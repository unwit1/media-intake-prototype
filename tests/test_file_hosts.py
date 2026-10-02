"""Public-host fixtures, real local transfers, GetComics load/fallback checks."""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import zipfile

from media_intake.core import connect, download, enqueue
from media_intake.comic_sources import parse_getcomics_results, search_getcomics, resolve_getcomics_post
from media_intake.file_hosts import ManualDownloadRequired, resolve_hosted_file
from media_intake import download_connectors as connectors
from media_intake.webcomics.http import SourceUnavailable

DATA = io.BytesIO()
with zipfile.ZipFile(DATA, 'w') as archive:
    archive.writestr('001.png', b'original fixture bytes')
CBZ = DATA.getvalue()
SHA256 = hashlib.sha256(CBZ).hexdigest()
POST = {'title': 'Sample Comic #1', 'author': 'Unknown', 'url': 'https://getcomics.org/sample-comic/'}


class FileHostTests(unittest.TestCase):
    def test_comic_mirrors_download_best_once_and_retry_failed_issue_host(self):
        from media_intake.watched import add_watch, _process_comic_watch, _process_comic_issue_watch
        from media_intake.rights_evidence import ensure_rights_evidence, load_rights_evidence, save_rights_evidence
        candidates = [dict(POST, source_key='getcomics.org', format='.cbz',
            url=f'https://files.example/{host}.cbz', source=POST['url'], release_identity=POST['url'])
            for host in ('failed', 'good', 'alternate')]
        def transfer(_db, item_id, *_args, **_kwargs):
            row = _db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            if '/failed.' in row['url']:
                raise OSError('mirror offline')
            return {'file_path': str(item_id) + '.cbz'}
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            path = ensure_rights_evidence(Path(tmp) / 'rights-evidence.json')
            document = load_rights_evidence(path)
            document['sources']['getcomics.org']['approved_for_download'] = True
            save_rights_evidence(path, document)
            options = {'rights_evidence_path': str(path)}
            watch_id = add_watch(db, 'comic', 'Sample Comic', metadata={'sources': options, 'comic': {}})
            watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
            with patch('media_intake.watched._search_candidates', return_value=(candidates, [])), \
                 patch('media_intake.watched.download', side_effect=transfer) as fetch:
                self.assertEqual(_process_comic_watch(db, watch, tmp), 1)
                self.assertEqual(fetch.call_count, 2)
                watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
                self.assertEqual(_process_comic_watch(db, watch, tmp), 0)
                self.assertEqual(fetch.call_count, 2)
            issue_id = add_watch(db, 'comic_issue', 'Sample Comic #1', metadata={'sources': options, 'comic_issue': {'issue_number': '1'}})
            issue = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (issue_id,)).fetchone())
            with patch('media_intake.watched._search_candidates', return_value=(candidates, [])), \
                 patch('media_intake.watched.download', side_effect=transfer) as fetch:
                self.assertEqual(_process_comic_issue_watch(db, issue, tmp), 1)
                self.assertEqual(fetch.call_count, 2)

    def test_getcomics_unknown_creator_does_not_reject_a_known_creator_watch(self):
        from media_intake.watched import _search_candidates_sequential
        watch = {'kind': 'comic', 'title': 'Sample Comic', 'author': 'Actual Writer', 'metadata': '{}'}
        posts = parse_getcomics_results('<h2><a href="/sample-comic/">Sample Comic #1</a></h2>', 'https://getcomics.org/page/1/')
        with patch('media_intake.watched.search_getcomics', return_value=posts) as search, \
             patch('media_intake.comic_sources.http_get', return_value=(b'<a href="https://files.example/sample.cbz">Download</a>', POST['url'])):
            files, errors = _search_candidates_sequential(watch, {'_resolved_approvals': {'getcomics.org': {'approved_for_download': True}}})
        self.assertEqual(len(files), 1)
        self.assertEqual(errors, [])
        search.assert_called_once_with('Sample Comic', limit=20)

    def test_all_static_registered_connectors_resolve_and_download_category_files(self):
        epub = io.BytesIO()
        with zipfile.ZipFile(epub, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
        bodies = {'.epub': epub.getvalue(), '.zip': CBZ,
                  '.mkv': bytes.fromhex('1a45dfa3') + b'local video fixture'}
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = bodies[Path(self.path).suffix]
                self.send_response(200); self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        import urllib.request
        original = urllib.request.urlopen
        cases = [('meawfy.com', 'Books'), ('softarchive.download', 'Books')]
        cases.extend((key, 'Anime') for key, *_rest in connectors.ANIME_DOWNLOAD_ADAPTERS)
        cases.extend((key, 'Books' if 'Books' in categories else 'Games')
                     for key, _name, categories in connectors.GENERAL_DOWNLOAD_ADAPTERS if key != 'xdcc.eu')
        try:
            with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
                for key, category in cases:
                    with self.subTest(connector=key):
                        ext = '.epub' if category == 'Books' else '.mkv' if category == 'Anime' else '.zip'
                        profile = connectors._GENERAL_DOWNLOAD_SITES[key]
                        page = profile['base_url'].rstrip('/') + '/fixture-release/'
                        item = {'title': 'Fixture release ' + key, 'author': 'Fixture creator', 'url': page}
                        direct = f'https://files.example.test/{key}{ext}'
                        with patch('media_intake.download_connectors.http_get', return_value=(f'<a href="{direct}">Download</a>'.encode(), page)):
                            files = connectors.resolve_site_downloads(item, key,
                                allowed_formats=connectors.formats_for_category(category))
                        self.assertEqual(len(files), 1)
                        queued = enqueue(db, files[0])
                        local = f'http://127.0.0.1:{server.server_address[1]}/fixture{ext}'
                        with patch('media_intake.core.open_url', side_effect=lambda *_a, **_k: original(local)):
                            result = download(db, queued['id'], tmp)
                        self.assertEqual(result['state'], 'downloaded')
                        self.assertEqual(Path(result['file_path']).read_bytes(), bodies[ext])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_pixeldrain_public_api_and_checksum(self):
        body = json.dumps({'success': True, 'name': 'Sample Comic.cbz', 'size': len(CBZ), 'hash_sha256': SHA256}).encode()
        with patch('media_intake.file_hosts.public_get', return_value=(body, 'https://pixeldrain.com/api/file/abcd1234/info')) as fetch:
            files = resolve_hosted_file('https://pixeldrain.com/u/abcd1234', {'.cbz', '.pdf'})
        self.assertEqual(files[0].url, 'https://pixeldrain.com/api/file/abcd1234?download')
        self.assertEqual(files[0].sha256, SHA256)
        self.assertEqual(fetch.call_count, 1)

    def test_pixeldrain_restrictions_are_not_bypassed(self):
        for restriction in ('file_rate_limited_captcha_required', 'transfer_limit_exceeded', 'unavailable_for_legal_reasons'):
            with self.subTest(restriction=restriction), patch('media_intake.file_hosts.public_get', return_value=(
                    json.dumps({'name': 'sample.cbz', 'availability': restriction}).encode(), 'https://pixeldrain.com/api/file/abcd1234/info')):
                with self.assertRaises(ManualDownloadRequired):
                    resolve_hosted_file('https://pixeldrain.com/u/abcd1234', {'.cbz'})

    def test_mediafire_reads_public_button_and_rejects_ad_links(self):
        share = 'https://www.mediafire.com/file/abc123/Sample.cbz/file'
        target = 'https://download42.mediafire.com/token/abc123/Sample.cbz'
        with patch('media_intake.file_hosts.public_get', return_value=(
                f'<a href="https://ads.example/evil.cbz">Download</a><a id="downloadButton" href="{target}">Download</a>'.encode(), share)):
            files = resolve_hosted_file(share, {'.cbz'})
        self.assertEqual([file.url for file in files], [target])
        with patch('media_intake.file_hosts.public_get', return_value=(b'<a id="downloadButton" href="https://evil.example/file.cbz">Download</a>', share)):
            with self.assertRaises(ValueError):
                resolve_hosted_file(share, {'.cbz'})

    def test_mediafire_challenge_is_manual_and_dropbox_preserves_share_key(self):
        share = 'https://www.mediafire.com/file/abc123/Sample.cbz/file'
        with patch('media_intake.file_hosts.public_get', return_value=(b'<h1>Verify you are human</h1>', share)):
            with self.assertRaises(ManualDownloadRequired):
                resolve_hosted_file(share, {'.cbz'})
        files = resolve_hosted_file('https://www.dropbox.com/scl/fi/token/Sample.cbz?rlkey=publickey&dl=0', {'.cbz'})
        self.assertIn('rlkey=publickey&dl=1', files[0].url)

    def test_protected_hosts_and_deceptive_domains_are_not_automatic(self):
        for url in ('https://mega.nz/file/token#key', 'https://we.tl/transfer', 'https://gofile.io/d/token'):
            with self.subTest(url=url), self.assertRaises(ManualDownloadRequired):
                resolve_hosted_file(url, {'.cbz'})
        self.assertIsNone(resolve_hosted_file('https://pixeldrain.com.evil.example/u/abcd1234', {'.cbz'}))
        with self.assertRaises(ValueError):
            resolve_hosted_file('https://user:secret@pixeldrain.com/u/abcd1234', {'.cbz'})

    def test_getcomics_resolves_all_working_hosts_despite_one_broken_mirror(self):
        html = (b'<a href="https://pixeldrain.com/u/abcd1234">Pixeldrain</a>'
                b'<a href="https://www.mediafire.com/file/abc123/Sample.cbz/file">Mediafire</a>'
                b'<a href="https://mega.nz/file/token#key">MEGA</a>'
                b'<a href="https://getcomics.org/download?id=1">Main Server</a>')
        def fetch(url, **_kwargs):
            if 'pixeldrain.com' in url:
                return json.dumps({'name': 'Sample.cbz', 'size': len(CBZ), 'hash_sha256': SHA256}).encode(), url
            return b'<a id="downloadButton" href="https://download42.mediafire.com/token/Sample.cbz">Download</a>', url
        post = dict(POST)
        with patch('media_intake.comic_sources.http_get', return_value=(html, POST['url'])), \
             patch('media_intake.file_hosts.public_get', side_effect=fetch), \
             patch('media_intake.comic_sources._head_download_target', side_effect=OSError('main server down')):
            items = resolve_getcomics_post(post, {'approved_for_download': True})
        self.assertEqual({item['file_host'] for item in items}, {'pixeldrain', 'mediafire'})
        self.assertTrue(any('MEGA' in warning for warning in post['connector_warnings']))
        self.assertEqual(items[0]['expected_sha256'], SHA256)

    def test_getcomics_duplicate_pagination_and_missing_last_page(self):
        body = b'<h2><a href="/sample-comic/">Sample Comic</a></h2>'
        with patch('media_intake.comic_sources.http_get', side_effect=[
                (body, 'https://getcomics.org/page/1/'), (body, 'https://getcomics.org/page/2/')]) as fetch:
            self.assertEqual(len(search_getcomics('Sample Comic', 20)), 1)
            self.assertEqual(fetch.call_count, 2)
        with patch('media_intake.comic_sources.http_get', side_effect=[
                (body, 'https://getcomics.org/page/1/'), SourceUnavailable(404, 'https://getcomics.org/page/2/')]):
            self.assertEqual(len(search_getcomics('Sample Comic', 20)), 1)

    def test_getcomics_wrapped_file_host_redirect_uses_host_adapter(self):
        post = dict(POST)
        with patch('media_intake.comic_sources.http_get', return_value=(
                b'<a href="https://getcomics.org/dls/encoded-token">PIXELDRAIN</a>', POST['url'])), \
             patch('media_intake.comic_sources._head_download_target', return_value=(
                 'https://pixeldrain.com/u/abcd1234', 'text/html', '')), \
             patch('media_intake.file_hosts.public_get', return_value=(
                 json.dumps({'name': 'Sample.cbz', 'hash_sha256': SHA256}).encode(),
                 'https://pixeldrain.com/api/file/abcd1234/info')):
            files = resolve_getcomics_post(post, {'approved_for_download': True})
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['file_host'], 'pixeldrain')

    def test_head_stops_at_share_redirect_before_fetching_viewer(self):
        from email.message import Message
        from media_intake.webcomics.http import HttpClient
        client = HttpClient(interval=0, respect_robots=False)
        headers = Message(); headers['Location'] = 'https://pixeldrain.com/u/abcd1234'
        error = urllib.error.HTTPError(POST['url'], 302, 'Found', headers, None)
        with patch.object(client, '_request', side_effect=error) as request:
            response = client.head(POST['url'], stop_at=lambda url: 'pixeldrain.com' in url)
        self.assertEqual(response.url, 'https://pixeldrain.com/u/abcd1234')
        self.assertEqual(request.call_count, 1)

    def test_getcomics_local_load_is_bounded_and_deduplicated(self):
        body = ''.join(f'<h2><a href="/comic-{i % 100}/">Comic {i % 100}</a></h2>' for i in range(1000))
        start = time.monotonic()
        for _ in range(100):
            items = parse_getcomics_results(body, 'https://getcomics.org/page/1/', limit=50)
            self.assertEqual(len(items), 50)
            self.assertEqual(len({item['url'] for item in items}), 50)
        self.assertLess(time.monotonic() - start, 15)

    def test_getcomics_each_public_host_downloads_validated_original_bytes(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(len(CBZ))); self.end_headers(); self.wfile.write(CBZ)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        import urllib.request
        original = urllib.request.urlopen
        local_url = f'http://127.0.0.1:{server.server_address[1]}/fixture.cbz'
        try:
            with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
                for host, url in [('pixeldrain', 'https://pixeldrain.com/u/abcd1234'),
                                  ('mediafire', 'https://www.mediafire.com/file/abc123/Sample.cbz/file'),
                                  ('dropbox', 'https://www.dropbox.com/s/token/Sample.cbz')]:
                    with self.subTest(host=host):
                        def fetch(target, **_kwargs):
                            if 'pixeldrain.com' in target:
                                return json.dumps({'name': 'Sample.cbz', 'size': len(CBZ), 'hash_sha256': SHA256}).encode(), target
                            return b'<a id="downloadButton" href="https://download42.mediafire.com/token/Sample.cbz">Download</a>', target
                        with patch('media_intake.comic_sources.http_get', return_value=(f'<a href="{url}">Download</a>'.encode(), POST['url'])), \
                             patch('media_intake.file_hosts.public_get', side_effect=fetch):
                            items = resolve_getcomics_post(dict(POST), {'approved_for_download': True})
                        self.assertEqual(len(items), 1)
                        queued = enqueue(db, items[0])
                        with patch('media_intake.core.open_url', side_effect=lambda *_a, **_k: original(local_url)):
                            result = download(db, queued['id'], tmp)
                        self.assertEqual(Path(result['file_path']).read_bytes(), CBZ)
                        self.assertEqual(result['sha256'], SHA256)
                        bad = dict(items[0], url=items[0]['url'] + '&invalid=1', expected_sha256='0' * 64)
                        queued = enqueue(db, bad)
                        with patch('media_intake.core.open_url', side_effect=lambda *_a, **_k: original(local_url)):
                            with self.assertRaisesRegex(ValueError, 'checksum'):
                                download(db, queued['id'], tmp)
                        self.assertFalse(list((Path(tmp) / 'downloads').glob('.partial-*')))
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
