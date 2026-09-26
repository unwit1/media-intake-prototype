import io
import datetime as dt
import json
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from media_intake.comic_sources import (parse_getcomics_results,
                                        parse_public_domain_release,
                                        search_getcomics, search_public_domain_comic_mirror,
                                        resolve_getcomics_post)
from media_intake.core import connect, download, enqueue, list_items
from media_intake.watched import add_watch, process_due_watches


CBZ = io.BytesIO()
with zipfile.ZipFile(CBZ, 'w') as archive:
    archive.writestr('001.png', b'comic page')
CBZ_BYTES = CBZ.getvalue()


class PublicDomainMirrorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/?s='):
            body = b'<article><h2><a href="/comic/sample-1/">Sample Comic #1</a></h2></article>'
            mime = 'text/html'
        elif self.path == '/comic/sample-1/':
            body = b'''<article><h1>Sample Comic #1</h1><p>Rights: Public Domain.</p>
              <a href="/files/sample-1.cbz">Download CBZ</a>
              <a href="https://files.example.net/copyrighted.cbz">External link</a></article>'''
            mime = 'text/html'
        elif self.path == '/files/sample-1.cbz':
            body, mime = CBZ_BYTES, 'application/zip'
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


class ComicDiscoveryTest(unittest.TestCase):
    def test_getcomics_search_parser_returns_only_post_references(self):
        html = '''<nav><h2><a href="https://getcomics.org/category/marvel/">Marvel</a></h2></nav>
          <article><h2><a href="https://getcomics.org/example-comic/">Example Comic #1</a></h2>
          <a href="https://mega.nz/file/hidden">Download</a></article>'''
        rows = parse_getcomics_results(html, 'https://getcomics.org/?s=Example', limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Example Comic #1')
        self.assertEqual(rows[0]['url'], 'https://getcomics.org/example-comic/')
        self.assertTrue(rows[0]['discovery_only'])
        self.assertNotIn('mega.nz', rows[0]['url'])

    def test_getcomics_result_page_is_discovery_only_and_domain_checked(self):
        content = '<h2><a href="https://getcomics.info/title/one/">Title One</a></h2>'
        with self.assertRaisesRegex(ValueError, 'official site'):
            parse_getcomics_results(content, 'https://example.org/?s=one')

    def test_getcomics_search_reads_paged_post_headings(self):
        pages = [
            b'<article><h1 class="post-title"><a href="https://getcomics.org/one/">One</a></h1></article>',
            b'<article><h1 class="post-title"><a href="https://getcomics.org/two/">Two</a></h1></article>',
            b'<main>No results</main>',
        ]
        finals = [f'https://getcomics.org/page/{number}/?s=Sample' for number in range(1, 4)]
        with patch('media_intake.comic_sources.http_get', side_effect=list(zip(pages, finals))) as fetch:
            results = search_getcomics('Sample', limit=5)
        self.assertEqual([item['title'] for item in results], ['One', 'Two'])
        self.assertEqual(fetch.call_count, 3)

    def test_getcomics_post_resolves_main_server_redirect_after_local_yes(self):
        item = {'title': 'Sample #1', 'url': 'https://getcomics.org/sample/'}
        page = b'<a href="https://getcomics.org/download?id=44">Main Server</a>'
        target = 'https://cdn.example.net/files/sample.cbz'
        with patch('media_intake.comic_sources.http_get', return_value=(page, item['url'])), \
             patch('media_intake.comic_sources._head_download_target',
                   return_value=(target, 'application/zip', '')) as head:
            results = resolve_getcomics_post(item, {'approved_for_download': True,
                                                     'rights_work_key': 'url:https://example.org/sample'})
        self.assertEqual(results[0]['url'], target)
        self.assertEqual(results[0]['format'], '.cbz')
        self.assertEqual(results[0]['source_key'], 'getcomics.org')
        self.assertEqual(results[0]['rights_work_key'], 'url:https://example.org/sample')
        head.assert_called_once()

    def test_getcomics_post_does_not_open_links_without_yes(self):
        with patch('media_intake.comic_sources.http_get') as fetch:
            self.assertEqual(resolve_getcomics_post(
                {'url': 'https://getcomics.org/sample/'},
                {'approved_for_download': False}), [])
        fetch.assert_not_called()

    def test_public_domain_release_parser_requires_marker_and_same_host_file(self):
        content = '''<h1>Public Comic</h1><p>Public Domain</p>
          <a href="/files/book.cbz">CBZ</a>
          <a href="https://another.example/book.cbz">External</a>'''
        rows = parse_public_domain_release(
            content, 'https://mirror.example/comic/1', 'https://mirror.example')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['format'], '.cbz')
        self.assertEqual(rows[0]['rights'], 'Public Domain marker on mirror page')
        no_marker = parse_public_domain_release(
            '<h1>Private Comic</h1><a href="/files/book.cbz">CBZ</a>',
            'https://mirror.example/comic/2', 'https://mirror.example')
        self.assertEqual(no_marker, [])
        local_no = parse_public_domain_release(
            '<h1>Marked Comic</h1><p>Public Domain</p><a href="/files/book.cbz">CBZ</a>',
            'https://mirror.example/comic/3', 'https://mirror.example',
            manual_rights={'approved_for_download': False})
        self.assertEqual(local_no, [])

    def test_comic_mirror_does_not_search_when_local_approval_is_no(self):
        with patch('media_intake.comic_sources.http_get') as fetch:
            rows = search_public_domain_comic_mirror(
                'Sample', 'https://mirror.example', manual_rights={'approved_for_download': False})
        self.assertEqual(rows, [])
        fetch.assert_not_called()

    def test_mirror_parser_downloads_marked_public_domain_cbz_into_library(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), PublicDomainMirrorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        try:
            results = search_public_domain_comic_mirror('Sample Comic', base)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['title'], 'Sample Comic #1')
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                library = root / 'library'
                library.mkdir()
                with closing(connect(root / 'data')) as db:
                    queued = enqueue(db, results[0])
                    saved = download(db, queued['id'], root, destination=library)
                    self.assertEqual(saved['state'], 'downloaded')
                    self.assertEqual(Path(saved['file_path']).parent, library)
                    self.assertEqual(len(list_items(db)), 1)
                    with zipfile.ZipFile(saved['file_path']) as archive:
                        self.assertEqual(archive.read('001.png'), b'comic page')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_public_domain_mirror_refuses_getcomics_host(self):
        with self.assertRaisesRegex(ValueError, 'separate public-domain mirror'):
            search_public_domain_comic_mirror('Example', 'https://getcomics.info')

    def test_comicvine_issue_watch_waits_for_store_date_then_downloads(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), PublicDomainMirrorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        release_date = (dt.date.today() + dt.timedelta(days=2)).isoformat()
        issues = [{'comicvine_id': '9001', 'number': '1', 'title': 'Example',
                   'release_date': release_date, 'source_url': 'https://comicvine.gamespot.com/issue/9001/'}]
        source_options = {'openlibrary': False, 'libgen': False, 'annas': False,
                          'opds_feeds': [], 'catalogs': [], 'irc_configs': [],
                          'comic_mirror_base': base, 'comicvine_api_key': 'fixture-secret'}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                library = root / 'library'
                library.mkdir()
                with closing(connect(root / 'data')) as db:
                    parent_id = add_watch(db, 'comic', 'Sample Comic', 'Unknown',
                                          'https://comicvine.gamespot.com/volume/123/',
                                          metadata={'sources': source_options, 'comic': {
                                              'comicvine_id': '123', 'known_chapters': []}})
                    with patch('media_intake.watched.os.environ', {'COMICVINE_API_KEY': 'fixture'}), \
                         patch('media_intake.watched.list_comicvine_issues', return_value=issues):
                        process_due_watches(db, str(library), force=True,
                                            source_options=source_options)
                        child = db.execute('SELECT * FROM watched_items WHERE parent_id=?', (parent_id,)).fetchone()
                        self.assertIsNotNone(child)
                        self.assertEqual(child['state'], 'upcoming')
                        child_metadata = json.loads(child['metadata'])
                        self.assertNotIn('comicvine_api_key', child_metadata['sources'])
                        self.assertEqual(list_items(db), [])

                        db.execute("UPDATE watched_items SET release_date=?,state='watching',next_check_at=NULL WHERE id=?",
                                   (dt.date.today().isoformat(), child['id']))
                        db.commit()
                        process_due_watches(db, str(library), force=True,
                                            source_options=source_options)
                        saved = db.execute('SELECT * FROM watched_items WHERE id=?', (child['id'],)).fetchone()
                        self.assertEqual(saved['state'], 'downloaded')
                        self.assertTrue(Path(saved['file_path']).is_file())
                        self.assertEqual(json.loads(saved['metadata'])['comic_issue']['issue_number'], '1')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
