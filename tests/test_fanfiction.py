import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from media_intake.core import connect, list_items
from media_intake.watched import add_watch, process_due_watches


STORY_URL = 'https://archiveofourown.org/works/12345678'


def write_epub(path, chapters):
    path = Path(path)
    manifest = ''.join(
        f'<item id="c{index}" href="chapter-{index:04d}.xhtml" media-type="application/xhtml+xml"/>'
        for index, _ in enumerate(chapters, 1))
    spine = ''.join(f'<itemref idref="c{index}"/>' for index, _ in enumerate(chapters, 1))
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml',
                         '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                         'media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('OEBPS/content.opf',
                         '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
                         'version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         '<dc:title>Test Story</dc:title><dc:creator>Test Author</dc:creator>'
                         '</metadata><manifest>' + manifest + '</manifest><spine>' + spine + '</spine></package>')
        for index, text in enumerate(chapters, 1):
            archive.writestr(f'OEBPS/chapter-{index:04d}.xhtml',
                             f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{text}</p></body></html>')


def fake_fanficfare(args, working_folder, profile):
    if '--update-epub' in args:
        epub = Path(args[args.index('--update-epub') + 1])
        with zipfile.ZipFile(epub) as archive:
            current = [archive.read(name).decode()
                       for name in sorted(archive.namelist())
                       if name.startswith('OEBPS/chapter-') and name.endswith('.xhtml')]
        if len(current) == 1:
            write_epub(epub, ['Chapter one', 'Chapter two'])
    else:
        output = next(option.split('=', 1)[1] for option in args
                      if option.startswith('output_filename='))
        write_epub(output, ['Chapter one'])
    return 'FanFicFare finished'


class FanfictionWatchTest(unittest.TestCase):
    def test_watch_creates_and_updates_one_epub_and_keeps_old_copy_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / 'library'
            library.mkdir()
            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(
                    db, 'fanfiction', 'Placeholder', 'Unknown', STORY_URL,
                    metadata={'sources': {}, 'fanfiction': {'story_url': STORY_URL}})
                with patch('media_intake.fanfiction._run_fanficfare', side_effect=fake_fanficfare):
                    self.assertEqual(process_due_watches(db, str(library), force=True), 1)
                    watched = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                    original_path = Path(watched['file_path'])
                    self.assertEqual(watched['title'], 'Test Story')
                    self.assertEqual(watched['author'], 'Test Author')
                    with zipfile.ZipFile(original_path) as archive:
                        self.assertIn(b'Chapter one', archive.read('OEBPS/chapter-0001.xhtml'))

                    self.assertEqual(process_due_watches(db, str(library), force=True), 1)
                    updated = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                    self.assertEqual(Path(updated['file_path']), original_path)
                    with zipfile.ZipFile(original_path) as archive:
                        chapters = [name for name in archive.namelist()
                                    if name.startswith('OEBPS/chapter-') and name.endswith('.xhtml')]
                        self.assertEqual(len(chapters), 2)
                        self.assertIn(b'Chapter two', archive.read('OEBPS/chapter-0002.xhtml'))

                    before_failure = original_path.read_bytes()
                    with patch('media_intake.fanfiction._run_fanficfare', side_effect=RuntimeError('temporary source error')):
                        with patch('media_intake.watched.logger.exception'):
                            self.assertEqual(process_due_watches(db, str(library), force=True), 1)
                    after_failure = original_path.read_bytes()
                    self.assertEqual(after_failure, before_failure)
                    updated = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                    self.assertIn('temporary source error', updated['last_error'])
                    queue = list_items(db)
                    self.assertEqual(len(queue), 1)
                    self.assertEqual(queue[0]['file_path'], str(original_path.resolve()))


if __name__ == '__main__':
    unittest.main()
