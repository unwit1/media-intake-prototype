import datetime as dt
import io
import json
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from media_intake.core import connect, list_items
from media_intake.media_catalogs import (english_release_date, parse_lnori_catalog,
                                         ranobedb_series, search_lnori,
                                         search_mangaupdates, search_novelarchive,
                                         search_ranobedb)
from media_intake.watched import add_watch, process_due_watches


EPUB = io.BytesIO()
with zipfile.ZipFile(EPUB, 'w') as archive:
    archive.writestr('mimetype', 'application/epub+zip')
EPUB_BYTES = EPUB.getvalue()


class ReleaseFileHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/new-volume.epub':
            body, mime = EPUB_BYTES, 'application/epub+zip'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class MetadataCatalogTest(unittest.TestCase):
    def test_novelarchive_search_returns_only_discovery_metadata(self):
        payload = {'novels': [{
            'id': 'novel-1', 'title': 'Worm', 'author': 'Wildbow',
            'description': 'A web serial.', 'total_chapters': '313',
            'release_status': 'completed', 'has_image_chapters': False,
        }]}
        with patch('media_intake.media_catalogs.http_get',
                   return_value=(json.dumps(payload).encode(), 'https://novelarchive.cc/api/novels?search=Worm')):
            result = search_novelarchive('Worm')
        self.assertEqual(result[0]['title'], 'Worm')
        self.assertTrue(result[0]['discovery_only'])
        self.assertEqual(result[0]['format'], 'link')
        self.assertEqual(result[0]['url'], 'https://novelarchive.cc/novel?id=novel-1')
        self.assertNotIn('download_url', result[0])

    def test_lnori_catalog_parser_matches_card_and_ignores_other_hosts(self):
        page = '''<article class="card" data-id="123" data-t="Sample Light Novel"
          data-a="Writer Name" data-d="2022" data-v="4" data-tags="fantasy,adventure">
          <a href="/series/123/sample-light-novel"><img src="https://cdn.lnori.com/cover.webp"></a>
          </article>
          <article class="card" data-id="124" data-t="Other Title" data-a="Other Writer">
          <a href="https://example.net/series/124/other">Other</a></article>'''
        results = parse_lnori_catalog(page, 'Sample Light Novel', 10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['author'], 'Writer Name')
        self.assertEqual(results[0]['url'], 'https://lnori.com/series/123/sample-light-novel')
        self.assertEqual(results[0]['metadata']['volumes'], '4')
        self.assertTrue(results[0]['discovery_only'])

    def test_lnori_live_lookup_parses_catalog_page_fixture(self):
        page = '<article class="card" data-id="6" data-t="Example" data-a="Writer" data-d="2020" data-v="2"><a href="/series/6/example">Example</a></article>'
        with patch('media_intake.media_catalogs.http_get',
                   return_value=(page.encode(), 'https://lnori.com/library')):
            result = search_lnori('Example')
        self.assertEqual(result[0]['metadata']['catalog_id'], '6')

    def test_ranobedb_search_and_detail_normalize_series_metadata(self):
        with patch('media_intake.media_catalogs._ranobedb_get', side_effect=[
                {'series': [{'id': 42, 'title': 'Sample Series', 'olang': 'ja', 'c_num_books': 5}]},
                {'series': {'id': 42, 'title': 'Sample Series', 'books': []}},
        ]):
            results = search_ranobedb('Sample Series')
            series = ranobedb_series('42')
        self.assertEqual(results[0]['metadata']['catalog_id'], '42')
        self.assertTrue(results[0]['discovery_only'])
        self.assertEqual(series['id'], 42)

    def test_mangaupdates_lookup_returns_english_license_and_publishers(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def read(self, _limit):
                return json.dumps(self.payload).encode()

        search = {'results': [{'record': {'series_id': 55, 'title': 'Sample Manga',
                                           'url': 'https://www.mangaupdates.com/series/55/sample'}}]}
        detail = {'series_id': 55, 'title': 'Sample Manga',
                  'url': 'https://www.mangaupdates.com/series/55/sample', 'licensed': True,
                  'publishers': [{'publisher_name': 'Example Books', 'lang_name': 'English'}]}
        with patch('media_intake.media_catalogs.urllib.request.urlopen',
                   side_effect=[Response(search), Response(detail)]):
            results = search_mangaupdates('Sample Manga')
        self.assertIs(results[0]['metadata']['english_licensed'], True)
        self.assertEqual(results[0]['metadata']['english_publishers'], ['Example Books'])

    def test_ranobedb_english_release_date_validation(self):
        self.assertEqual(english_release_date(20261005), '2026-10-05')
        self.assertIsNone(english_release_date(20261345))
        self.assertIsNone(english_release_date(None))


class WatchedLightNovelTest(unittest.TestCase):
    def test_english_volume_waits_until_release_then_searches_and_downloads(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), ReleaseFileHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        today = dt.date.today()
        payload = {'id': 42, 'title': 'Sample Series', 'books': [
            {'id': 1, 'title': 'Sample Series 1', 'c_release_dates': {'en': int((today - dt.timedelta(days=10)).strftime('%Y%m%d'))}},
            {'id': 2, 'title': 'Sample Series 2', 'c_release_dates': {'en': int((today + dt.timedelta(days=2)).strftime('%Y%m%d'))}},
            {'id': 3, 'title': 'Sample Series 3', 'c_release_dates': {'ja': int(today.strftime('%Y%m%d'))}},
        ]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                library = root / 'library'
                library.mkdir()
                with closing(connect(root / 'data')) as db:
                    parent = add_watch(db, 'lightnovel', 'Sample Series', 'Writer',
                                       'https://ranobedb.org/series/42', metadata={
                                           'sources': {}, 'lightnovel': {'ranobedb_id': '42'}})
                    with patch('media_intake.watched.ranobedb_series', return_value=payload):
                        self.assertEqual(process_due_watches(db, str(library), force=True), 1)
                    children = db.execute('SELECT * FROM watched_items WHERE parent_id=? ORDER BY id', (parent,)).fetchall()
                    self.assertEqual(len(children), 1)
                    self.assertEqual(children[0]['title'], 'Sample Series 2')
                    self.assertEqual(children[0]['state'], 'upcoming')
                    status = json.loads(db.execute('SELECT metadata FROM watched_items WHERE id=?', (parent,)).fetchone()['metadata'])['lightnovel']
                    self.assertEqual(status['english_release_status'], 'upcoming')

                    due = today - dt.timedelta(days=1)
                    db.execute("UPDATE watched_items SET release_date=?,state='watching',next_check_at=NULL WHERE id=?",
                               (due.isoformat(), children[0]['id']))
                    db.commit()
                    candidate = {'title': 'Sample Series 2', 'author': 'Writer',
                                 'url': base + '/new-volume.epub', 'source': base,
                                 'format': '.epub'}
                    with patch('media_intake.watched.ranobedb_series', return_value=payload), \
                         patch('media_intake.watched._search_candidates', return_value=([candidate], [])) as search:
                        process_due_watches(db, str(library), force=True)
                    self.assertTrue(search.called)
                    saved = db.execute('SELECT * FROM watched_items WHERE id=?', (children[0]['id'],)).fetchone()
                    self.assertEqual(saved['state'], 'downloaded')
                    self.assertTrue(Path(saved['file_path']).is_file())
                    with zipfile.ZipFile(saved['file_path']) as archive:
                        self.assertIn('mimetype', archive.namelist())
                    self.assertEqual(len(list_items(db)), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
