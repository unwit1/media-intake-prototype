import datetime as dt
import io
import json
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from media_intake.comics import (list_comicvine_issues, list_mangadex_chapters,
                                 search_anilist, search_comicvine, search_mangadex,
                                 search_taddy)
from media_intake.core import connect, list_items
from media_intake.serial import discover_feed
from media_intake.watched import add_watch, process_due_watches


MANGA_ID = '550e8400-e29b-41d4-a716-446655440000'
CHAPTER_ID = '550e8400-e29b-41d4-a716-446655440001'
PNG = bytes.fromhex('89504e470d0a1a0a') + b'comic-page-test-data'


class FeedPages(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/feed':
            published = format_datetime(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
            body = (f'<rss version="2.0"><channel><title>Feed Comic</title>'
                    f'<item><title>Chapter 7</title><link>{self.server.base}/chapter/7</link>'
                    f'<pubDate>{published}</pubDate></item></channel></rss>').encode()
            mime = 'application/rss+xml'
        elif self.path == '/serial-feed':
            entries = ''.join(
                f'<item><title>{title}</title><link>{self.server.base}{path}</link>'
                f'<pubDate>{published}</pubDate></item>'
                for title, path, published in self.server.serial_items)
            body = (f'<rss version="2.0"><channel><title>Sample Web Serial</title>'
                    f'<managingEditor>Serial Author</managingEditor>{entries}</channel></rss>').encode()
            mime = 'application/rss+xml'
        elif self.path == '/sss-feed':
            body = json.dumps(self.server.sss_feed).encode()
            mime = 'application/json'
        elif self.path.startswith('/serial-chapter/'):
            title = self.path.rsplit('/', 1)[-1].replace('-', ' ')
            body, mime = f'<main><article><h1>{title}</h1><p>New words for {title}.</p></article></main>'.encode(), 'text/html'
        elif self.path == '/chapter/7':
            body, mime = b'<article><img src="/page.png"></article>', 'text/html'
        elif self.path == '/page.png':
            body, mime = PNG, 'image/png'
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


class ComicCatalogTest(unittest.TestCase):
    def test_comicvine_search_and_issue_lookup_use_api_key_and_release_dates(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _limit): return json.dumps(self.payload).encode()

        volume = {'id': 123, 'name': 'Public Comic', 'start_year': 1939,
                  'count_of_issues': 27, 'site_detail_url': 'https://comicvine.gamespot.com/public-comic/4050-123/'}
        issue = {'id': 456, 'name': 'Public Comic #27', 'issue_number': '27',
                 'store_date': '2026-10-01 00:00:00', 'cover_date': '2026-10-01 00:00:00',
                 'site_detail_url': 'https://comicvine.gamespot.com/public-comic-27/4000-456/'}
        with patch('media_intake.comics.urllib.request.urlopen',
                   side_effect=[Response({'error': 'OK', 'results': [volume]}),
                                Response({'error': 'OK', 'results': [issue]})]) as open_url:
            volumes = search_comicvine('Public Comic', 'fixture-key')
            issues = list_comicvine_issues('123', 'fixture-key')
        self.assertEqual(volumes[0]['comicvine_id'], '123')
        self.assertEqual(volumes[0]['format'], 'catalog')
        self.assertIn('fixture-key', open_url.call_args_list[0].args[0].full_url)
        self.assertEqual(issues[0]['number'], '27')
        self.assertEqual(issues[0]['release_date'], '2026-10-01 00:00:00')

    def test_mangadex_feed_keeps_latest_translation_and_group(self):
        payload = {'data': [
            {'id': 'newest-chapter-2', 'attributes': {'chapter': '2', 'volume': '1',
              'title': 'The Door', 'readableAt': '2026-09-20T12:00:00+00:00'},
             'relationships': [{'type': 'scanlation_group', 'id': 'group-new',
                               'attributes': {'name': 'Newest Group'}}]},
            {'id': 'chapter-1', 'attributes': {'chapter': '1', 'volume': '1',
              'title': '', 'readableAt': '2026-09-19T12:00:00+00:00'}, 'relationships': []},
            {'id': 'old-chapter-2', 'attributes': {'chapter': '2', 'volume': '1',
              'title': 'The Door', 'readableAt': '2026-09-18T12:00:00+00:00'},
             'relationships': [{'type': 'scanlation_group', 'id': 'group-old',
                               'attributes': {'name': 'Older Group'}}]},
        ]}
        with patch('media_intake.comics.mangadex_http_get',
                   return_value=(json.dumps(payload).encode(), 'https://api.mangadex.org')):
            chapters = list_mangadex_chapters(MANGA_ID)
        self.assertEqual([chapter['mangadex_id'] for chapter in chapters],
                         ['chapter-1', 'newest-chapter-2'])
        self.assertEqual(chapters[-1]['groups'], ['Newest Group'])

    def test_mangadex_catalog_search_normalizes_metadata(self):
        payload = {'data': [{'id': MANGA_ID, 'attributes': {
            'title': {'en': 'Example Manga'}, 'description': {'en': 'A story.'}},
            'relationships': [{'type': 'author', 'attributes': {'name': 'Writer'}}]}]}
        with patch('media_intake.comics.mangadex_http_get',
                   return_value=(json.dumps(payload).encode(), 'https://api.mangadex.org')):
            results = search_mangadex('Example Manga')
        self.assertEqual(results[0]['title'], 'Example Manga')
        self.assertEqual(results[0]['author'], 'Writer')
        self.assertEqual(results[0]['mangadex_id'], MANGA_ID)

    def test_taddy_search_normalizes_comic_metadata(self):
        series = {'uuid': MANGA_ID, 'name': 'Example Scroll', 'description': 'A webtoon.',
                  'status': 'ONGOING', 'sssUrl': 'https://taddy.org/feeds/sss/comicseries/' + MANGA_ID,
                  'seriesType': 'WEBTOON', 'totalIssuesCount': 12, 'datePublished': 1767225600,
                  'creators': [{'name': 'Creator'}]}
        payload = {'data': {'search': {'comicSeries': [series]}}}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _limit): return json.dumps(payload).encode()

        with patch('media_intake.comics.urllib.request.urlopen', return_value=Response()) as request:
            results = search_taddy('Example Scroll', '1234', 'test-api-key')
        sent = json.loads(request.call_args.args[0].data.decode())
        self.assertIn('filterForTypes: [COMICSERIES]', sent['query'])
        self.assertEqual(request.call_args.args[0].get_header('X-user-id'), '1234')
        self.assertEqual(request.call_args.args[0].get_header('X-api-key'), 'test-api-key')
        self.assertEqual(results[0]['kind'], 'webtoon')
        self.assertEqual(results[0]['author'], 'Creator')
        self.assertEqual(results[0]['sss_url'], series['sssUrl'])

    def test_anilist_search_associates_mangadex_id_and_metadata(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, _limit):
                return json.dumps({'data': {'Page': {'media': [{
                    'id': 42, 'siteUrl': 'https://anilist.co/manga/42/example',
                    'format': 'WEBTOON', 'status': 'RELEASING', 'chapters': 12, 'volumes': None,
                    'countryOfOrigin': 'KR', 'genres': ['Drama'],
                    'title': {'english': 'Example Webtoon', 'romaji': 'Example'},
                    'description': 'A story.', 'startDate': {'year': 2024, 'month': 3, 'day': 4},
                    'externalLinks': [{'site': 'MangaDex',
                                       'url': f'https://mangadex.org/title/{MANGA_ID}'}],
                    'staff': {'edges': [{'role': 'Story & Art',
                                         'node': {'name': {'full': 'Creator'}}}]},
                }]}}}).encode()

        with patch('media_intake.comics.urllib.request.urlopen', return_value=Response()):
            results = search_anilist('Example Webtoon')
        self.assertEqual(results[0]['kind'], 'webtoon')
        self.assertEqual(results[0]['author'], 'Creator')
        self.assertEqual(results[0]['mangadex_id'], MANGA_ID)
        self.assertEqual(results[0]['release_date'], '2024-03-04')
        self.assertEqual(results[0]['chapters'], 12)


class WatchedComicDownloadTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), FeedPages)
        self.server.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.server.serial_items = []
        self.server.sss_feed = {}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _make_old_watch(self, db, kind, title, source_url, metadata):
        watch_id = add_watch(db, kind, title, 'Artist', source_url, metadata=metadata)
        created_at = (dt.datetime.now() - dt.timedelta(days=3)).isoformat(sep=' ')
        db.execute('UPDATE watched_items SET created_at=? WHERE id=?', (created_at, watch_id))
        db.commit()
        return watch_id

    def test_mangadex_watch_downloads_cbz_with_attribution_to_library(self):
        chapter = {
            'title': 'Chapter 8 - New Page', 'number': '8', 'volume': '1',
            'published': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat(),
            'url': f'https://mangadex.org/chapter/{CHAPTER_ID}',
            'mangadex_id': CHAPTER_ID, 'groups': ['Test Scanlation Group'],
        }

        def fake_mangadex_get(url, max_bytes=10_000_000):
            if '/at-home/server/' in url:
                body = json.dumps({'baseUrl': 'https://uploads.mangadex.org', 'chapter': {
                    'hash': 'hash', 'data': ['001.png']}}).encode()
            else:
                body = PNG
            return body, url

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / 'library'
            library.mkdir()
            with closing(connect(root / 'data')) as db:
                watch_id = self._make_old_watch(
                    db, 'manga', 'Example Manga', f'https://mangadex.org/title/{MANGA_ID}',
                    {'comic': {'mangadex_id': MANGA_ID, 'known_chapters': []}, 'sources': {}})
                with patch('media_intake.watched.list_mangadex_chapters', return_value=[chapter]), \
                     patch('media_intake.watched._search_candidates', return_value=([], [])), \
                     patch('media_intake.comics.mangadex_http_get', side_effect=fake_mangadex_get):
                    self.assertEqual(process_due_watches(db, str(library), force=True), 1)

                watched = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watched['state'], 'watching')
                self.assertIn(CHAPTER_ID, json.loads(watched['metadata'])['comic']['known_chapters'])
                self.assertEqual(Path(watched['file_path']).parent, library)
                queued = list_items(db)
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]['state'], 'downloaded')
                with zipfile.ZipFile(watched['file_path']) as archive:
                    self.assertIn('0001-0001.png', archive.namelist())
                    self.assertEqual(archive.read('0001-0001.png'), PNG)
                    info = archive.read('ComicInfo.xml').decode()
                    self.assertIn(f'https://mangadex.org/chapter/{CHAPTER_ID}', info)
                    self.assertIn('Test Scanlation Group', info)

    def test_rss_webtoon_watch_downloads_new_chapter_to_library(self):
        feed_url = self.server.base + '/feed'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / 'library'
            library.mkdir()
            with closing(connect(root / 'data')) as db:
                watch_id = self._make_old_watch(
                    db, 'webtoon', 'Feed Comic', feed_url,
                    {'comic': {'feed_url': feed_url, 'selector': 'article', 'known_chapters': []},
                     'sources': {}})
                with patch('media_intake.watched._search_candidates', return_value=([], [])):
                    self.assertEqual(process_due_watches(db, str(library), force=True), 1)
                watched = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watched['state'], 'watching')
                self.assertTrue(Path(watched['file_path']).is_file())
                self.assertEqual(Path(watched['file_path']).parent, library)
                with zipfile.ZipFile(watched['file_path']) as archive:
                    self.assertEqual(archive.namelist(), ['0001-0001.png', 'ComicInfo.xml'])
                    self.assertEqual(archive.read('0001-0001.png'), PNG)
                    self.assertIn(b'Feed Comic', archive.read('ComicInfo.xml'))
                self.assertEqual(list_items(db)[0]['state'], 'downloaded')

    def test_sss_watch_indexes_and_downloads_public_issue_images_as_cbz(self):
        feed_url = self.server.base + '/sss-feed'
        self.server.sss_feed = {
            '@context': 'https://3s-docs.org', '@type': 'ComicSeries',
            'identifier': 'series-1', 'name': 'SSS Comic', 'description': 'A feed comic',
            'feedOwnerName': 'Comic Author', 'copyrightNotice': 'Copyright the creator',
            'issues': [{
                '@type': 'ComicIssue', 'identifier': 'issue-9', 'name': 'Chapter 9',
                'datePublished': (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(),
                'stories': [{'storyImage': {'base_url': self.server.base + '/', 'story': 'page.png'}}],
            }],
        }
        parsed = discover_feed(feed_url, 'webcomic')
        self.assertEqual(parsed['title'], 'SSS Comic')
        self.assertEqual(parsed['author'], 'Comic Author')
        self.assertEqual(parsed['chapters'][0]['images'], [self.server.base + '/page.png'])
        self.assertEqual(parsed['sss_metadata']['copyrightNotice'], 'Copyright the creator')

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / 'library'
            library.mkdir()
            with closing(connect(root / 'data')) as db:
                watch_id = self._make_old_watch(
                    db, 'webcomic', 'SSS Comic', feed_url,
                    {'comic': {'feed_url': feed_url, 'selector': 'article', 'known_chapters': [],
                               'catalog_metadata': parsed['sss_metadata']}, 'sources': {}})
                with patch('media_intake.watched._search_candidates', return_value=([], [])):
                    self.assertEqual(process_due_watches(db, str(library), force=True), 1)
                watched = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watched['state'], 'watching')
                self.assertEqual(Path(watched['file_path']).parent, library)
                self.assertTrue(any('issue-9' in key for key in
                                    json.loads(watched['metadata'])['comic']['known_chapters']))
                queued = list_items(db)
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]['state'], 'downloaded')
                with zipfile.ZipFile(watched['file_path']) as archive:
                    self.assertEqual(archive.read('0001-0001.png'), PNG)
                    self.assertIn('Copyright the creator', archive.read('ComicInfo.xml').decode())

    def test_web_serial_watch_indexes_and_downloads_new_epub_chapter(self):
        feed_url = self.server.base + '/serial-feed'
        old_date = format_datetime(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4))
        new_date = format_datetime(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
        self.server.serial_items = [('Chapter 1', '/serial-chapter/chapter-1', old_date)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / 'library'
            library.mkdir()
            with closing(connect(root / 'data')) as db:
                watch_id = self._make_old_watch(
                    db, 'webfiction', 'Sample Web Serial', feed_url,
                    {'serial': {'feed_url': feed_url, 'selector': 'article',
                                'known_chapters': [], 'known_files': []}, 'sources': {}})
                with patch('media_intake.watched._search_candidates', return_value=([], [])):
                    self.assertEqual(process_due_watches(db, str(library), force=True), 1)
                    self.assertEqual(list_items(db), [])
                    self.server.serial_items.append(('Chapter 2', '/serial-chapter/chapter-2', new_date))
                    self.assertEqual(process_due_watches(db, str(library), force=True), 1)

                watched = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(watched['state'], 'watching')
                self.assertEqual(Path(watched['file_path']).parent, library)
                serial_state = json.loads(watched['metadata'])['serial']
                self.assertEqual(len(serial_state['known_chapters']), 2)
                queued = list_items(db)
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]['state'], 'downloaded')
                self.assertEqual(Path(queued[0]['file_path']).suffix, '.epub')
                with zipfile.ZipFile(queued[0]['file_path']) as archive:
                    self.assertIn(b'New words for chapter 2.',
                                  archive.read('OEBPS/chapter-0001.xhtml'))


if __name__ == '__main__':
    unittest.main()
