import json
import io
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from media_intake.core import (connect, download, enqueue, file_name, http_get,
                               import_calibre, list_items, queue_watch_search,
                               search_opds, update_watch_search)
from unittest.mock import patch


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/feed':
            body = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x1</id><title>Example Book</title><author><name>Jane Doe</name></author><link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/book.epub"/></entry></feed>'''
            mime = 'application/atom+xml'
        else:
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('mimetype', 'application/epub+zip')
            body = output.getvalue() if self.path == '/book.epub' else b'broken epub'
            mime = 'application/epub+zip'
        self.send_response(200); self.send_header('Content-Type', mime); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args): pass


class PrototypeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def test_opds_queue_download_and_dedupe(self):
        base = f'http://127.0.0.1:{self.server.server_address[1]}'
        items = search_opds(base + '/feed', 'example book')
        self.assertEqual(len(items), 1)
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                first = enqueue(db, items[0]); self.assertEqual(first['id'], enqueue(db, items[0])['id'])
                result = download(db, first['id'], tmp)
                self.assertEqual(result['state'], 'downloaded')
                self.assertEqual(Path(result['file_path']).name, 'Example Book - Jane Doe.epub')
                self.assertEqual(len(list_items(db)), 1)
                self.assertEqual(download(db, first['id'], tmp)['file_path'], result['file_path'])
                with patch('media_intake.core.subprocess.run') as run:
                    imported = import_calibre(db, first['id'], '/tmp/Calibre Library')
                self.assertEqual(imported['state'], 'imported')
                self.assertEqual(run.call_args.args[0][0:4], ['calibredb', 'add', '--with-library', '/tmp/Calibre Library'])

    def test_watch_search_queue_shows_active_download_connector(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                queue_watch_search(db, 7, 'Example Book', 'Writer')
                update_watch_search(db, 7, progress_message='Searching LNORI…')
                row = list_items(db)[0]

        self.assertEqual(row['state'], 'searching')
        self.assertEqual(row['progress_message'], 'Searching LNORI…')

    def test_successful_book_download_runs_optional_kindle_delivery_hook(self):
        base = f'http://127.0.0.1:{self.server.server_address[1]}'
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            item = enqueue(db, {
                'title': 'Kindle Hook Book', 'author': 'Jane Doe',
                'url': base + '/book.epub', 'format': '.epub', 'source': base,
                'kind': 'book', 'category': 'Books',
            })
            with patch('media_intake.kindle.maybe_auto_send_to_kindle',
                       return_value={'id': item['id'], 'state': 'downloaded'}) as deliver:
                result = download(db, item['id'], tmp)

            self.assertEqual(result['state'], 'downloaded')
            deliver.assert_called_once()
            self.assertEqual(deliver.call_args.args[1], item['id'])
            self.assertEqual(Path(deliver.call_args.args[2]), Path(tmp))

    def test_invalid_file_is_failed_and_retriable(self):
        base = f'http://127.0.0.1:{self.server.server_address[1]}'
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                item = enqueue(db, {'title': 'Bad file', 'url': base + '/bad.epub', 'format': '.epub', 'source': base})
                with self.assertRaisesRegex(ValueError, 'Invalid .epub archive'):
                    download(db, item['id'], tmp)
                self.assertEqual(list_items(db)[0]['state'], 'failed')
                self.assertEqual(list((Path(tmp) / 'downloads').iterdir()), [])

    def test_http_get_closes_error_response_before_reraising(self):
        body = io.BytesIO(b'provider error')
        error = HTTPError('https://example.invalid/catalog', 403, 'Forbidden', {}, body)
        with patch('media_intake.core.urllib.request.urlopen', side_effect=error):
            with self.assertRaises(HTTPError) as raised:
                http_get('https://example.invalid/catalog')

        self.assertIs(raised.exception, error)
        self.assertTrue(body.closed)

    def test_name_sanitization(self):
        self.assertEqual(file_name({'title': '../Bad: title', 'author': 'A/B', 'format': '.pdf', 'series': 'Series', 'series_index': 2}), 'Series 2 - Bad title - A B.pdf')


if __name__ == '__main__': unittest.main()
