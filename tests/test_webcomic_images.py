"""Image validation, ComicInfo content/order and disk budget for webcomic archives."""
import errno
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from unittest.mock import patch

from media_intake.core import connect
from media_intake.webcomics.downloads import COMIC_INFO_ORDER, check_disk_budget, comic_info, record_disk_usage
from media_intake.webcomics.images import CorruptImage, inspect_image
from media_intake.webcomics.models import WebcomicAsset, WebcomicEntry


def png(width=4, height=5):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    rows = b''.join(b'\x00' + b'\x10\x20\x30' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def jpeg(width=640, height=480):
    sof = b'\xff\xc0' + struct.pack('>HBHHB', 11, 8, height, width, 1) + b'\x01\x11\x00'
    return b'\xff\xd8\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00' + b'\x00' * 9 + sof + \
        b'\xff\xda' + struct.pack('>H', 8) + b'\x01\x01\x00\x00\x3f\x00' + b'\x12\x34' * 50 + b'\xff\xd9'


def gif(width=120, height=60):
    return b'GIF89a' + struct.pack('<HH', width, height) + b'\x00\x00\x00' + b'\x2c' + b'\x00' * 20 + b'\x3b'


def webp(kind, width=300, height=200):
    if kind == 'VP8X':
        payload = b'VP8X' + struct.pack('<I', 10) + b'\x00\x00\x00\x00' + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')
    elif kind == 'VP8L':
        bits = (width - 1) | ((height - 1) << 14)
        payload = b'VP8L' + struct.pack('<I', 5) + b'\x2f' + struct.pack('<I', bits) + b'\x00' * 6
    else:
        payload = b'VP8 ' + struct.pack('<I', 10) + b'\x00' * 3 + b'\x9d\x01\x2a' + struct.pack('<HH', width, height) + b'\x00' * 4
    body = b'WEBP' + payload
    return b'RIFF' + struct.pack('<I', len(body)) + body


class ImageTests(unittest.TestCase):
    def test_dimensions_for_each_format(self):
        self.assertEqual(inspect_image(png()), ('.png', 4, 5))
        self.assertEqual(inspect_image(jpeg()), ('.jpg', 640, 480))
        self.assertEqual(inspect_image(gif(), 'image/gif'), ('.gif', 120, 60))
        for kind in ('VP8X', 'VP8L', 'VP8 '):
            self.assertEqual(inspect_image(webp(kind)), ('.webp', 300, 200), kind)

    def test_truncated_corrupt_and_mislabelled_images_are_rejected(self):
        for name, data in (('png cut', png()[:-20]), ('jpeg cut', jpeg()[:-60]), ('gif cut', gif()[:-1] + b'\x00' * 20),
                           ('webp cut', webp('VP8X')[:-6]), ('html', b'<html>404</html>'),
                           ('png header only', bytes.fromhex('89504e470d0a1a0a') + b'fixture'),
                           ('jpeg no frame', b'\xff\xd8\xff\xda\x00\x08' + b'\x00' * 30 + b'\xff\xd9')):
            with self.subTest(name), self.assertRaises(CorruptImage):
                inspect_image(data)
        with self.assertRaisesRegex(CorruptImage, 'text/html'):
            inspect_image(png(), 'text/html')
        self.assertEqual(inspect_image(png(), 'application/octet-stream')[0], '.png')


class ComicInfoTests(unittest.TestCase):
    def test_comic_info_round_trips_credits_number_web_arc_and_pages(self):
        series = {'title': 'Fixture Comic', 'language': 'en', 'tags': ['Fantasy', 'Adventure'],
                  'creators': [{'name': 'Ada Writer', 'role': 'author'}, {'name': 'Bo Artist', 'role': 'artist'},
                               {'name': 'Cy Colors', 'role': 'colorist'}]}
        entry = WebcomicEntry('42', 'https://comic.example/p/42', 'Page 42 & More', ordinal=42.0, number='42',
                              published_at='2024-03-07T10:00:00Z')
        assets = [WebcomicAsset('https://comic.example/a.png', 1, width=800, height=1200),
                  WebcomicAsset('https://comic.example/b.png', 2, width=800, height=1100)]
        xml = comic_info(series, entry, {'title': 'Chapter 3: The Climb'}, assets)
        root = ET.fromstring(xml)
        values = {child.tag: child.text for child in root if child.tag != 'Pages'}
        self.assertEqual(values['Title'], 'Page 42 & More')
        self.assertEqual((values['Series'], values['Number'], values['Web']), ('Fixture Comic', '42', 'https://comic.example/p/42'))
        self.assertEqual((values['Writer'], values['Penciller'], values['Colorist']), ('Ada Writer', 'Bo Artist', 'Cy Colors'))
        self.assertEqual((values['Year'], values['Month'], values['Day']), ('2024', '3', '7'))
        self.assertEqual((values['StoryArc'], values['PageCount'], values['LanguageISO']), ('Chapter 3: The Climb', '2', 'en'))
        self.assertEqual(values['Genre'], 'Fantasy, Adventure')
        order = [child.tag for child in root if child.tag != 'Pages']
        self.assertEqual(order, [tag for tag in COMIC_INFO_ORDER if tag in order])  # schema sequence
        pages = root.find('Pages').findall('Page')
        self.assertEqual([(p.get('Image'), p.get('ImageWidth'), p.get('ImageHeight')) for p in pages],
                         [('0', '800', '1200'), ('1', '800', '1100')])
        self.assertEqual(pages[0].get('Type'), 'FrontCover')


class DiskBudgetTests(unittest.TestCase):
    def test_budget_and_nearly_full_disk(self):
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                check_disk_budget(db, root)  # no budget: fine
                db.execute("INSERT INTO gui_settings(key,value) VALUES('webcomics_disk_budget_mb','1')")
                record_disk_usage(db, 600_000)
                check_disk_budget(db, root)
                record_disk_usage(db, 600_000)
                with self.assertRaisesRegex(ValueError, 'disk budget reached'):
                    check_disk_budget(db, root)
                db.execute("UPDATE gui_settings SET value='0' WHERE key='webcomics_disk_budget_mb'")
                with patch('media_intake.webcomics.downloads.shutil.disk_usage', return_value=__import__('types').SimpleNamespace(total=1, used=1, free=1024)):
                    with self.assertRaisesRegex(ValueError, 'nearly full'):
                        check_disk_budget(db, root)
            finally:
                db.close()

    def test_disk_full_during_archiving_is_reported_and_cleaned_up(self):
        import pathlib
        try:
            from tests.test_webcomics import BASE, PNG, FakeClient, fixture
        except ImportError:
            from test_webcomics import BASE, PNG, FakeClient, fixture
        from media_intake.webcomics.downloads import download_entry
        from media_intake.webcomics.http import Response
        from media_intake.webcomics.watcher import add_series, queue_pending
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                client = FakeClient({BASE + '/comic': fixture('static.html')})
                watch_id = add_series(db, BASE + '/comic', client=client, archive_existing=True)
                source = dict(db.execute('SELECT * FROM webcomic_sources WHERE watch_id=?', (watch_id,)).fetchone())
                row = db.execute('SELECT * FROM items WHERE id=?', (queue_pending(db, source, 1)[0],)).fetchone()
                client.pages[BASE + '/page/1'] = b'<div id="comic"><img src="/image.png"></div>'
                client.pages[BASE + '/image.png'] = Response(PNG, BASE + '/image.png', {'content-type': 'image/png'})
                full = OSError(errno.ENOSPC, 'No space left on device')
                with patch('zipfile.ZipFile.writestr', side_effect=full):
                    with self.assertRaisesRegex(ValueError, 'disk is full'):
                        download_entry(db, row, root, 1_000_000, client=client)
                leftovers = list((pathlib.Path(root) / 'downloads').glob('.webcomic-*'))
                self.assertEqual(leftovers, [])
                # A corrupt image is rejected and recorded without marking the source broken.
                client.pages[BASE + '/image.png'] = Response(PNG[:-12], BASE + '/image.png', {'content-type': 'image/png'})
                with self.assertRaisesRegex(ValueError, 'Image 1 rejected'):
                    download_entry(db, row, root, 1_000_000, client=client)
                health = db.execute('SELECT health FROM webcomic_sources WHERE id=?', (source['id'],)).fetchone()[0]
                self.assertEqual(health, 'OK')
                rejected = db.execute("SELECT COUNT(*) FROM webcomic_events WHERE event_type='ASSET_REJECTED'").fetchone()[0]
                self.assertEqual(rejected, 1)
            finally:
                db.close()

if __name__ == '__main__':
    unittest.main()
