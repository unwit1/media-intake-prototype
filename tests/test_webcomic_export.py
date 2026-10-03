"""Exports keep original bytes and manifest order; CBZ/EPUB open as a reader would; OPDS lists them."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from media_intake.core import connect
from media_intake.webcomics.downloads import download_entry
from media_intake.webcomics.export import export_source
from media_intake.webcomics.http import Response
from media_intake.webcomics.watcher import add_series, queue_pending

try:
    from tests.test_webcomics import BASE, FakeClient, _png, fixture
except ImportError:
    from test_webcomics import BASE, FakeClient, _png, fixture


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        client = FakeClient({BASE + '/comic': fixture('static.html')})
        watch_id = add_series(self.db, BASE + '/comic', client=client, archive_existing=True)
        source = dict(self.db.execute('SELECT * FROM webcomic_sources WHERE watch_id=?', (watch_id,)).fetchone())
        self.source_id = source['id']
        self.images = {}
        for row in self.db.execute('SELECT url FROM webcomic_entries ORDER BY ordinal').fetchall():
            number = len(self.images) + 1
            art = [_png(2 + number, 3), _png(3, 2 + number)]
            client.pages[row['url']] = f'<div id="comic"><img src="/img/{number}a.png"><img src="/img/{number}b.png"></div>'.encode()
            for suffix, data in zip('ab', art):
                client.pages[f'{BASE}/img/{number}{suffix}.png'] = Response(data, f'{BASE}/img/{number}{suffix}.png', {'content-type': 'image/png'})
            self.images[row['url']] = art
        for item_id in queue_pending(self.db, source, 100):
            download_entry(self.db, self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone(),
                           self.temp.name, 5_000_000, client=client)
        self.expected = [data for url in self.images for data in self.images[url]]

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_series_cbz_keeps_bytes_order_and_comicinfo(self):
        out = Path(self.temp.name) / 'export'
        (path,) = export_source(self.db, self.source_id, out, 'series_cbz')
        with zipfile.ZipFile(path) as archive:
            images = sorted(name for name in archive.namelist() if name.endswith('.png'))
            self.assertEqual([archive.read(name) for name in images], self.expected)
            info = ET.fromstring(archive.read('ComicInfo.xml'))
            self.assertEqual(info.find('PageCount').text, str(len(self.expected)))
        catalog = json.loads((out / 'catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(catalog['publications'][0]['links'][0]['type'], 'application/vnd.comicbook+zip')

    def test_chapter_cbz_and_folders_and_epub(self):
        out = Path(self.temp.name) / 'export'
        chapters = export_source(self.db, self.source_id, out, 'chapter_cbz')
        pages = []
        for path in chapters:
            with zipfile.ZipFile(path) as archive:
                pages += [archive.read(name) for name in sorted(archive.namelist()) if name.endswith('.png')]
        self.assertEqual(pages, self.expected)
        folders = export_source(self.db, self.source_id, out, 'folders')
        self.assertEqual([hashlib.sha256(p.read_bytes()).hexdigest() for f in folders for p in sorted(f.glob('*.png'))],
                         [hashlib.sha256(data).hexdigest() for data in self.expected])
        (epub,) = export_source(self.db, self.source_id, out, 'epub')
        with zipfile.ZipFile(epub) as archive:
            self.assertEqual(archive.namelist()[0], 'mimetype')
            self.assertEqual(archive.read('mimetype'), b'application/epub+zip')
            opf = ET.fromstring(archive.read('OEBPS/content.opf'))
            spine = opf.find('{http://www.idpf.org/2007/opf}spine')
            self.assertEqual(len(spine), len(self.expected))
            self.assertEqual(archive.read('OEBPS/images/0001.png'), self.expected[0])
        listed = {pub['links'][0]['href'] for pub in json.loads((out / 'catalog.json').read_text(encoding='utf-8'))['publications']}
        self.assertTrue(any(href.endswith('.epub') for href in listed))

    def test_tampered_archive_is_refused(self):
        path = Path(self.db.execute('SELECT metadata FROM webcomic_assets LIMIT 1').fetchone()[0] and
                    json.loads(self.db.execute('SELECT metadata FROM webcomic_assets LIMIT 1').fetchone()[0])['local_path'])
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members['0001.png'] = b'changed'
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            export_source(self.db, self.source_id, Path(self.temp.name) / 'export', 'series_cbz')


if __name__ == '__main__':
    unittest.main()
