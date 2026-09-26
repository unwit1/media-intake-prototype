import datetime as dt
import io
import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from media_intake.core import connect, list_items
from media_intake.rights_evidence import ensure_rights_evidence, load_rights_evidence, save_rights_evidence
from media_intake.watched import _search_candidates, add_watch, process_due_watches


EPUB = io.BytesIO()
with zipfile.ZipFile(EPUB, 'w') as archive:
    archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
EPUB_BYTES = EPUB.getvalue()
CBZ = io.BytesIO()
with zipfile.ZipFile(CBZ, 'w') as archive:
    archive.writestr('001.png', b'fixture comic page')
CBZ_BYTES = CBZ.getvalue()


class ApprovedFileHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        assets = {
            '/getcomics.cbz': (CBZ_BYTES, 'application/zip'),
            '/lnori.epub': (EPUB_BYTES, 'application/epub+zip'),
            '/novelarchive.epub': (EPUB_BYTES, 'application/epub+zip'),
        }
        body, content_type = assets.get(self.path, (b'', ''))
        if not body:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class ApprovedSourceDownloadTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ApprovedFileHandler)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _approve(self, path, source):
        rights_path = ensure_rights_evidence(path)
        document = load_rights_evidence(rights_path)
        document['sources'][source]['approved_for_download'] = True
        document['sources'][source]['evidence'] = [{'summary': 'Enabled in local fixture test'}]
        save_rights_evidence(rights_path, document)
        return rights_path

    def _source_options(self, rights_path):
        return {
            'rights_evidence_path': str(rights_path),
            'openlibrary': False, 'libgen': False, 'annas': False,
            'opds_feeds': [], 'catalogs': [], 'irc_configs': [],
        }

    def _route_test_file_host(self, host_map):
        original = urllib.request.urlopen

        def route(request, *args, **kwargs):
            url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
            parsed = urllib.request.urlsplit(url)
            local_path = host_map.get(parsed.hostname)
            if local_path:
                return original(self.base + local_path, *args, **kwargs)
            return original(request, *args, **kwargs)
        return route

    def test_watched_getcomics_release_resolves_main_server_and_downloads_cbz(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', 'getcomics.org')
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            post = {'provider': 'GetComics', 'title': 'Sample Comic #1',
                    'author': 'Unknown', 'url': 'https://getcomics.org/sample-comic/'}
            post_html = b'<article><a href="https://getcomics.org/download?id=41">Main Server</a></article>'
            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'comic', 'Sample Comic', metadata={
                    'sources': options, 'comic': {'known_chapters': []}})
                with patch('media_intake.watched.search_getcomics', return_value=[post]), \
                     patch('media_intake.comic_sources.http_get',
                           return_value=(post_html, post['url'])), \
                     patch('media_intake.comic_sources._head_download_target',
                           return_value=(self.base + '/getcomics.cbz', 'application/zip', '')), \
                     patch('urllib.request.urlopen', side_effect=self._route_test_file_host({} )):
                    # The resolved link is already the fixture server URL, so
                    # no host translation is needed for this download.
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertIsNotNone(watch['file_path'], watch['last_error'])
                self.assertTrue(Path(watch['file_path']).is_file())
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertEqual(archive.read('001.png'), b'fixture comic page')

    def test_getcomics_source_no_skips_release_page_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = ensure_rights_evidence(root / 'rights-evidence.json')
            options = self._source_options(rights_path)
            watch = {'id': 1, 'kind': 'comic', 'title': 'Sample Comic', 'author': 'Unknown',
                     'metadata': json.dumps({'sources': options})}
            with patch('media_intake.watched.search_getcomics') as search:
                candidates, _ = _search_candidates(watch)
            self.assertEqual(candidates, [])
            search.assert_not_called()

    def _download_catalog_file(self, source, file_path, item_url, page_html):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', source)
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            item = {'provider': 'LNORI' if source == 'lnori.com' else 'NovelArchive',
                    'title': f'{source} Sample Book', 'author': 'Writer', 'url': item_url,
                    'source': item_url, 'format': 'link', 'discovery_only': True}
            lookup = 'media_intake.watched.search_lnori' if source == 'lnori.com' else 'media_intake.watched.search_novelarchive'
            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'book', item['title'], 'Writer', item_url,
                                     metadata={'sources': options})
                with patch(lookup, return_value=[item]), \
                     patch('media_intake.media_catalogs.http_get',
                           return_value=(page_html.encode(), item_url)), \
                     patch('urllib.request.urlopen', side_effect=self._route_test_file_host({
                         f'files.{source.replace(".", "-")}.test': file_path,
                     })):
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watch['state'], 'downloaded')
                self.assertTrue(Path(watch['file_path']).is_file(), watch['last_error'])
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertIn('mimetype', archive.namelist())
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_watched_lnori_source_downloads_a_resolved_epub(self):
        page = '<main><a href="https://files.lnori-com.test/lnori.epub">Download EPUB</a></main>'
        self._download_catalog_file('lnori.com', '/lnori.epub',
                                    'https://lnori.com/series/10/sample', page)

    def test_watched_novelarchive_source_downloads_a_resolved_epub(self):
        page = '<main><a href="https://files.novelarchive-cc.test/novelarchive.epub">EPUB</a></main>'
        self._download_catalog_file('novelarchive.cc', '/novelarchive.epub',
                                    'https://novelarchive.cc/novel?id=10', page)

    def test_novelarchive_webfiction_watch_downloads_chapter_as_epub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', 'novelarchive.cc')
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            series_url = 'https://novelarchive.cc/novel?id=42'
            chapter = {'title': 'Chapter 1: Start',
                       'url': 'https://novelarchive.cc/chapter/42/1',
                       'published': (dt.datetime.now(dt.timezone.utc) -
                                     dt.timedelta(days=1)).isoformat()}
            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'webfiction', 'Sample Serial', 'Writer', series_url,
                                     metadata={'sources': options, 'serial': {
                                         'feed_url': series_url, 'known_chapters': [],
                                         'known_files': []}})
                yesterday = (dt.datetime.now() - dt.timedelta(days=2)).isoformat(sep=' ')
                db.execute('UPDATE watched_items SET created_at=? WHERE id=?', (yesterday, watch_id))
                db.commit()
                with patch('media_intake.watched.discover_novelarchive_chapters',
                           return_value={'chapters': [chapter], 'selector': 'main,article'}), \
                     patch('media_intake.media_catalogs.http_get',
                           return_value=(b'<main>No direct files on this page</main>', series_url)), \
                     patch('media_intake.serial.http_get',
                           return_value=(b'<main><h1>Chapter 1</h1><p>Chapter fixture text.</p></main>', chapter['url'])):
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertIsNotNone(watch['file_path'], watch['last_error'])
                self.assertTrue(Path(watch['file_path']).is_file())
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertIn('OEBPS/chapter-0001.xhtml', archive.namelist())
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')


if __name__ == '__main__':
    unittest.main()
