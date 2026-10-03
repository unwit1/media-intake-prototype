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
from media_intake.download_connectors import ANIME_DOWNLOAD_ADAPTERS
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
            '/internet-archive.epub': (EPUB_BYTES, 'application/epub+zip'),
            '/softarchive.zip': (CBZ_BYTES, 'application/zip'),
            '/meawfy.epub': (EPUB_BYTES, 'application/epub+zip'),
            '/foss-tool.zip': (CBZ_BYTES, 'application/zip'),
            '/anime.mkv': (b'fixture anime video bytes', 'video/x-matroska'),
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

    def test_book_watch_continues_after_browser_only_mediafire_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', 'mediafiretrend.com')
            self._approve(rights_path, 'lnori.com')
            options = self._source_options(rights_path)
            watch = {'id': 1, 'kind': 'book', 'title': 'Example Book', 'author': 'Writer',
                     'metadata': json.dumps({'sources': options})}
            file_hit = {'title': 'Example Book', 'author': 'Writer', 'format': '.epub',
                        'url': 'https://files.example.test/example.epub',
                        'source': 'https://lnori.com/series/1/example',
                        'provider': 'LNORI', 'source_key': 'lnori.com'}
            with patch('media_intake.watched.search_registered_download_site', return_value=[{
                    'title': 'Example Book', 'provider': 'MediaFireTrend',
                    'source_key': 'mediafiretrend.com', 'format': 'link',
                    'discovery_only': True}]) as browser_search, \
                 patch('media_intake.watched.search_lnori', return_value=[{
                     'title': 'Example Book', 'author': 'Writer', 'format': 'link',
                     'url': 'https://lnori.com/series/1/example', 'discovery_only': True}]), \
                 patch('media_intake.watched.resolve_catalog_file_links',
                       return_value=[file_hit]):
                progress = []
                candidates, errors = _search_candidates(
                    watch, progress_callback=progress.append)

        self.assertEqual([item['source_key'] for item in candidates], ['lnori.com'])
        self.assertFalse(any('app search is unavailable' in error for error in errors))
        self.assertEqual(browser_search.call_count, 1)
        self.assertIn('LNORI', progress)
        self.assertIn('MediaFireTrend', progress)

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

    def test_internet_archive_watch_searches_and_downloads_an_open_epub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', 'internet_archive')
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            candidate = {
                'title': 'Example Archive Book', 'author': 'Example Writer',
                'url': 'https://archive.org/download/example-book/example.epub',
                'format': '.epub', 'source': 'https://archive.org/details/example-book',
                'provider': 'Internet Archive', 'source_key': 'internet_archive',
            }
            search_json = json.dumps({'response': {'docs': [{
                'identifier': 'example-book', 'title': candidate['title'],
                'creator': candidate['author'], 'mediatype': 'texts',
            }]}}).encode()
            metadata_json = json.dumps({
                'metadata': {'title': candidate['title'], 'creator': candidate['author']},
                'files': [{'name': 'example.epub', 'format': 'EPUB', 'size': '1024'}],
            }).encode()

            def archive_response(url, **_kwargs):
                if '/advancedsearch.php?' in url:
                    return search_json, url
                if '/metadata/example-book' in url:
                    return metadata_json, url
                raise AssertionError(f'Unexpected Internet Archive request: {url}')

            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'book', 'Example Archive Book', 'Example Writer',
                                     metadata={'sources': options})
                with patch('media_intake.download_connectors.http_get',
                           side_effect=archive_response) as search, \
                     patch('urllib.request.urlopen', side_effect=self._route_test_file_host({
                         'archive.org': '/internet-archive.epub'})):
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                self.assertEqual(search.call_count, 2)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watch['state'], 'downloaded', watch['last_error'])
                self.assertTrue(Path(watch['file_path']).is_file())
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertIn('mimetype', archive.namelist())
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_softarchive_game_watch_resolves_and_downloads_an_approved_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', 'softarchive.download')
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            search_page = (b'<article><h2><a href="/full/example-game">'
                           b'Example Game</a></h2></article>')
            detail_page = (b'<main><a href="https://files.softarchive-download.test/'
                           b'softarchive.zip">Download ZIP</a></main>')

            def softarchive_response(url, **_kwargs):
                if '/full?s=' in url:
                    return search_page, url
                if url == 'https://softarchive.download/full/example-game':
                    return detail_page, url
                raise AssertionError(f'Unexpected SoftArchive request: {url}')

            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'game', 'Example Game', metadata={
                    'sources': options, 'game_subtype': 'base_game'})
                with patch('media_intake.download_connectors.http_get',
                           side_effect=softarchive_response) as search, \
                     patch('urllib.request.urlopen', side_effect=self._route_test_file_host({
                         'files.softarchive-download.test': '/softarchive.zip'})):
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                self.assertEqual(search.call_count, 2)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watch['state'], 'downloaded', watch['last_error'])
                self.assertTrue(Path(watch['file_path']).is_file())
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertIn('001.png', archive.namelist())
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_oldgamesdownload_watch_resolves_and_downloads_an_approved_game_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json',
                                        'oldgamesdownload.com')
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            search_page = (b'<article><a href="/game/example-game/">'
                           b'Example Game</a></article>')
            detail_page = (b'<main><a href="https://files.oldgames.test/'
                           b'example-game.zip">Download ZIP</a></main>')

            def oldgames_response(url, **_kwargs):
                parts = urllib.parse.urlsplit(url)
                if parts.query == 's=Example+Game':
                    return search_page, url
                if url == 'https://oldgamesdownload.com/game/example-game/':
                    return detail_page, url
                raise AssertionError(f'Unexpected OldGamesDownload request: {url}')

            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'game', 'Example Game', metadata={
                    'sources': options, 'game_subtype': 'base_game'})
                with patch('media_intake.download_connectors.http_get',
                           side_effect=oldgames_response) as search, \
                     patch('urllib.request.urlopen', side_effect=self._route_test_file_host({
                         'files.oldgames.test': '/softarchive.zip'})):
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                self.assertEqual(search.call_count, 2)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?',
                                    (watch_id,)).fetchone()
                self.assertEqual(watch['state'], 'downloaded', watch['last_error'])
                self.assertTrue(Path(watch['file_path']).is_file())
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertIn('001.png', archive.namelist())
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_registered_game_sites_download_approved_fixture_archives(self):
        cases = (
            ('mediafiretrend.com', 'https://mediafiretrend.com/example-game'),
            ('warezomen.com', 'https://warezomen.com/example-game'),
            ('myabandonware.com', 'https://www.myabandonware.com/game/example-game/'),
            ('oldgamesdownload.com', 'https://oldgamesdownload.com/game/example-game/'),
            ('abandonwaregames.net', 'https://abandonwaregames.net/game/example-game/'),
            ('ankergames.net', 'https://ankergames.net/example-game'),
        )
        for source, detail_url in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rights_path = self._approve(root / 'rights-evidence.json', source)
                options = self._source_options(rights_path)
                library = root / 'library'
                library.mkdir()
                search_page = (f'<article><a href="{detail_url}">Example Game</a></article>'
                               .encode())
                detail_page = (b'<main><a href="https://files.fixture.test/'
                               b'approved-game.zip">Download ZIP</a></main>')

                def site_response(url, **_kwargs):
                    if url == detail_url:
                        return detail_page, url
                    if urllib.parse.urlsplit(url).hostname not in {
                            source, 'www.' + source}:
                        raise AssertionError(f'Unexpected {source} request: {url}')
                    return search_page, url

                def post_search(url, fields, **_kwargs):
                    self.assertEqual(source, 'myabandonware.com')
                    self.assertEqual(fields, {'searchValue': 'Example Game'})
                    return search_page, url

                with closing(connect(root / 'data')) as db:
                    watch_id = add_watch(db, 'game', 'Example Game', metadata={
                        'sources': options, 'game_subtype': 'base_game'})
                    with patch('media_intake.download_connectors.http_get',
                               side_effect=site_response) as get, \
                         patch('media_intake.download_connectors._post_site_form',
                               side_effect=post_search) as post, \
                         patch('urllib.request.urlopen',
                               side_effect=self._route_test_file_host({
                                   'files.fixture.test': '/softarchive.zip'})):
                        self.assertEqual(process_due_watches(
                            db, str(library), force=True, source_options=options), 1)
                    if source == 'myabandonware.com':
                        post.assert_called_once()
                        self.assertEqual(get.call_count, 1)
                    else:
                        post.assert_not_called()
                        self.assertEqual(get.call_count, 2)
                    watch = db.execute('SELECT * FROM watched_items WHERE id=?',
                                        (watch_id,)).fetchone()
                    self.assertEqual(watch['state'], 'downloaded', watch['last_error'])
                    self.assertTrue(Path(watch['file_path']).is_file())
                    with zipfile.ZipFile(watch['file_path']) as archive:
                        self.assertIn('001.png', archive.namelist())
                    self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_meawfy_book_watch_searches_and_downloads_an_approved_epub(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', 'meawfy.com')
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            search_page = (b'<article><h2><a href="/example-book">'
                           b'Example Meawfy Book</a></h2></article>')
            detail_page = (b'<main><a href="https://files.meawfy.test/'
                           b'meawfy.epub">Download EPUB</a></main>')

            def meawfy_response(url, **_kwargs):
                if '?s=' in url:
                    return search_page, url
                if url == 'https://meawfy.com/example-book':
                    return detail_page, url
                raise AssertionError(f'Unexpected Meawfy request: {url}')

            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'book', 'Example Meawfy Book', 'Example Writer',
                                     metadata={'sources': options})
                with patch('media_intake.download_connectors.http_get',
                           side_effect=meawfy_response) as search, \
                     patch('urllib.request.urlopen', side_effect=self._route_test_file_host({
                         'files.meawfy.test': '/meawfy.epub'})):
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                self.assertEqual(search.call_count, 2)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watch['state'], 'downloaded', watch['last_error'])
                self.assertTrue(Path(watch['file_path']).is_file())
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertIn('mimetype', archive.namelist())
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_anime_site_watches_resolve_and_download_approved_video_files(self):
        for source_key, provider, _searcher, _resolver in ANIME_DOWNLOAD_ADAPTERS:
            with self.subTest(source=provider), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rights_path = self._approve(root / 'rights-evidence.json', source_key)
                options = self._source_options(rights_path)
                library = root / 'library'
                library.mkdir()
                listing = (b'<article><h2><a href="/anime/example-anime">'
                           b'Example Anime Series</a></h2></article>')
                detail = (b'<main><a href="https://cdn.anime-fixture.test/'
                          b'anime.mkv">Download video</a></main>')

                def anime_response(url, **_kwargs):
                    if urllib.parse.urlsplit(url).path == '/anime/example-anime':
                        return detail, url
                    return listing, url

                with closing(connect(root / 'data')) as db:
                    watch_id = add_watch(
                        db, 'video', 'Example Anime Series', metadata={
                            'entity_type': 'work', 'video_subtype': 'anime',
                            'sources': options})
                    with patch('media_intake.download_connectors.http_get',
                               side_effect=anime_response) as requests, \
                         patch('urllib.request.urlopen', side_effect=self._route_test_file_host({
                             'cdn.anime-fixture.test': '/anime.mkv'})):
                        self.assertEqual(process_due_watches(
                            db, str(library), force=True, source_options=options), 1)
                    self.assertEqual(requests.call_count, 2)
                    watch = db.execute('SELECT * FROM watched_items WHERE id=?',
                                        (watch_id,)).fetchone()
                    self.assertEqual(watch['state'], 'downloaded', watch['last_error'])
                    self.assertTrue(Path(watch['file_path']).is_file())
                    self.assertEqual(Path(watch['file_path']).read_bytes(),
                                     b'fixture anime video bytes')
                    self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_software_watch_downloads_an_approved_foss_release_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rights_path = self._approve(root / 'rights-evidence.json', 'github_foss')
            options = self._source_options(rights_path)
            library = root / 'library'
            library.mkdir()
            release_url = 'https://github.com/example/tool/releases/download/v1/tool.zip'
            candidate = {
                'title': 'Example FOSS Tool v1', 'author': 'example',
                'url': release_url, 'format': '.zip',
                'source': 'https://github.com/example/tool/releases/tag/v1',
                'source_url': 'https://github.com/example/tool',
                'provider': 'GitHub (FOSS)', 'source_key': 'github_foss',
                'kind': 'software', 'software_project': 'example/tool',
                'software_version': 'v1', 'software_license': 'MIT',
            }
            with closing(connect(root / 'data')) as db:
                watch_id = add_watch(db, 'software', 'Example FOSS Tool', metadata={
                    'sources': options, 'software': {'known_download_urls': []}})
                with patch('media_intake.watched.search_github_foss',
                           return_value=[candidate]) as search_github, \
                     patch('media_intake.watched.search_sourceforge_foss') as search_sourceforge, \
                     patch('urllib.request.urlopen', side_effect=self._route_test_file_host({
                         'github.com': '/foss-tool.zip'})):
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                    self.assertEqual(process_due_watches(
                        db, str(library), force=True, source_options=options), 1)
                self.assertEqual(search_github.call_count, 2)
                search_sourceforge.assert_not_called()
                watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                metadata = json.loads(watch['metadata'])
                self.assertEqual(watch['state'], 'watching', watch['last_error'])
                self.assertEqual(metadata['software']['known_download_urls'], [release_url])
                self.assertTrue(Path(watch['file_path']).is_file())
                with zipfile.ZipFile(watch['file_path']) as archive:
                    self.assertIn('001.png', archive.namelist())
                self.assertEqual(sum(row['state'] == 'downloaded' for row in list_items(db)), 1)

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
                     patch('media_intake.watched._search_candidates',
                           return_value=([], [])), \
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
