import json
import socketserver
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from media_intake.core import connect, download
from media_intake.serial import discover_feed, enqueue_serial, validate_manifest


PNG = bytes.fromhex('89504e470d0a1a0a') + b'test-image-content'


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/feed':
            data = b'<rss version="2.0"><channel><title>Sample Serial</title><item><title>Chapter Two</title><link>http://localhost/chapter-2</link></item><item><title>Chapter One</title><link>http://localhost/chapter-1</link></item></channel></rss>'
            data = data.replace(b'http://localhost', self.server.base.encode())
            mime = 'application/rss+xml'
        elif self.path == '/chapter-1':
            data, mime = b'<html><nav><p>Ignore menu</p></nav><article><p>First paragraph.</p><script>ignore me</script><p>Second paragraph.</p></article></html>', 'text/html'
        elif self.path == '/chapter-2':
            data, mime = b'<main><p>The next chapter.</p></main>', 'text/html'
        elif self.path == '/images':
            data, mime = b'<div class="viewer"><img data-src="/one.png"><img src="/two.png"></div>', 'text/html'
        elif self.path in ('/one.png', '/two.png'):
            data, mime = PNG, 'image/png'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_): pass


class SerialTest(unittest.TestCase):
    def setUp(self):
        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Pages)
        self.server.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_rss_to_epub(self):
        manifest = discover_feed(self.server.base + '/feed', 'fanfiction')
        self.assertEqual([x['title'] for x in manifest['chapters']], ['Chapter One', 'Chapter Two'])
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                row = enqueue_serial(db, manifest)
                result = download(db, row['id'], tmp)
                with zipfile.ZipFile(result['file_path']) as archive:
                    self.assertIn(b'First paragraph.', archive.read('OEBPS/chapter-0001.xhtml'))
                    self.assertNotIn(b'Ignore menu', archive.read('OEBPS/chapter-0001.xhtml'))
                    self.assertIn(b'The next chapter.', archive.read('OEBPS/chapter-0002.xhtml'))
                self.assertEqual(result['state'], 'downloaded')

    def test_webtoon_images_to_cbz(self):
        manifest = {'title': 'Drawn Story', 'author': 'Artist', 'kind': 'webtoon',
                    'selector': '.viewer', 'chapters': [{'title': '1', 'url': self.server.base + '/images'}]}
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                row = enqueue_serial(db, manifest)
                result = download(db, row['id'], tmp)
                with zipfile.ZipFile(result['file_path']) as archive:
                    self.assertEqual(archive.namelist(), ['0001-0001.png', '0001-0002.png', 'ComicInfo.xml'])
                    self.assertEqual(archive.read('0001-0001.png'), PNG)
                    self.assertIn(b'Drawn Story', archive.read('ComicInfo.xml'))

    def test_bad_manifest(self):
        with self.assertRaises(ValueError):
            validate_manifest({'title': 'Oops', 'kind': 'manga', 'chapters': [{'url': 'file:///etc/passwd'}]})


if __name__ == '__main__': unittest.main()
