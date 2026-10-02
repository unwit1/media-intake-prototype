"""Shortlink detection, headless resolution, file-host hand-off and the browser helper's Python side."""
import base64
import io
import json
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from media_intake import file_hosts, shortlink_browser, shortlinks
from media_intake.webcomics.http import Response, SourceUnavailable


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url, max_bytes=None, conditional=None):
        self.requested.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        final, body = page
        return Response(body.encode(), final, {}, 200)


class DetectionTests(unittest.TestCase):
    def test_userscript_rules_identify_shorteners_only(self):
        for url in ('https://ouo.io/abc', 'https://linkvertise.com/1/x', 'https://shrinkme.io/x',
                    'https://clk.sh/abc', 'https://maloma3arbi.blogspot.com/p'):
            self.assertTrue(shortlinks.is_shortlink(url), url)
        for url in ('https://pixeldrain.com/u/x', 'https://www.mediafire.com/file/x', 'https://youtube.com/watch',
                    'https://www.google.com/search?q=x', 'https://github.com/a/b', 'https://getcomics.org/x',
                    'https://example.com/a.epub', 'ftp://ouo.io/x'):
            self.assertFalse(shortlinks.is_shortlink(url), url)

    def test_source_is_pinned_with_matching_checksums(self):
        import hashlib
        info = shortlinks.source_info()
        self.assertEqual(len(info['commit']), 40)
        for name, digest in info['sha256'].items():
            self.assertEqual(hashlib.sha256((shortlinks.DATA / name).read_bytes()).hexdigest(), digest, name)


class ResolutionTests(unittest.TestCase):
    def test_destination_from_parameters(self):
        target = 'https://pixeldrain.com/u/abc123'
        encoded = base64.b64encode(target.encode()).decode().rstrip('=')
        self.assertEqual(shortlinks.destination_from_url(f'https://ouo.io/go?url={target}'), target)
        self.assertEqual(shortlinks.destination_from_url(f'https://ouo.io/go?safe={encoded}'), target)
        self.assertEqual(shortlinks.destination_from_url(f'https://ouo.io/go?id={encoded[::-1]}'), target)
        self.assertEqual(shortlinks.destination_from_url(f'https://clk.sh/st?api=1&url={target}'), target)
        self.assertIsNone(shortlinks.destination_from_url('https://ouo.io/go?id=12345'))

    def test_destination_from_html(self):
        page = '<meta http-equiv="refresh" content="0; url=https://www.mediafire.com/file/x/a.epub">'
        self.assertEqual(shortlinks.destination_from_html(page, 'https://ouo.io/a'),
                         'https://www.mediafire.com/file/x/a.epub')
        script = '<script>setTimeout(()=>{window.location.href = "https://pixeldrain.com/u/zz";}, 5000)</script>'
        self.assertEqual(shortlinks.destination_from_html(script, 'https://ouo.io/a'), 'https://pixeldrain.com/u/zz')
        self.assertIsNone(shortlinks.destination_from_html('<a href="/x">Continue</a>', 'https://ouo.io/a'))

    def test_resolve_follows_chain_and_requires_browser_for_interactive_gates(self):
        client = FakeClient({
            'https://ouo.io/a': ('https://ouo.io/a', '<script>location.replace("https://shrinkme.io/b")</script>'),
            'https://shrinkme.io/b': ('https://pixeldrain.com/u/final', ''),
            'https://ouo.io/timer': ('https://ouo.io/timer', '<button id="go">Get link</button>'),
            'https://ouo.io/blocked': SourceUnavailable(403, 'https://ouo.io/blocked'),
        })
        self.assertEqual(shortlinks.resolve_shortlink('https://ouo.io/a', client=client), 'https://pixeldrain.com/u/final')
        with self.assertRaisesRegex(shortlinks.ShortlinkNeedsBrowser, 'page interaction'):
            shortlinks.resolve_shortlink('https://ouo.io/timer', client=client)
        with self.assertRaisesRegex(shortlinks.ShortlinkNeedsBrowser, 'robots'):
            shortlinks.resolve_shortlink('https://ouo.io/blocked', client=client)

    def test_hosted_file_resolution_follows_shortlinks(self):
        with patch('media_intake.shortlinks.resolve_shortlink', return_value='https://files.example/Book%201.epub'):
            hosted = file_hosts.resolve_hosted_file('https://ouo.io/a', {'.epub'})
        self.assertEqual([(f.url, f.format, f.host, f.filename, f.shortlink) for f in hosted],
                         [('https://files.example/Book%201.epub', '.epub', 'direct', 'Book 1.epub', 'https://ouo.io/a')])
        with patch('media_intake.shortlinks.resolve_shortlink',
                   side_effect=shortlinks.ShortlinkNeedsBrowser('Shortlink ouo.io needs page interaction')):
            with self.assertRaisesRegex(file_hosts.ManualDownloadRequired, 'Resolve shortlink'):
                file_hosts.resolve_hosted_file('https://ouo.io/a', {'.epub'})
        with patch('media_intake.shortlinks.resolve_shortlink', return_value='https://files.example/page.html'):
            self.assertEqual(file_hosts.resolve_hosted_file('https://ouo.io/a', {'.epub'}), [])
        self.assertIsNone(file_hosts.resolve_hosted_file('https://example.com/page', {'.epub'}))


class Reply(io.BytesIO):
    status, reason = 200, 'OK'
    headers = {'Content-Type': 'text/plain'}

    def geturl(self):
        return 'https://api.example/final'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BrowserHelperTests(unittest.TestCase):
    def test_payload_bundles_shim_config_and_script_with_captcha_automation_locked(self):
        payload = shortlink_browser.userscript_payload()
        self.assertIn('const locked = {Audio: false, hCaptcha: false};', payload)
        self.assertIn('window.MonkeyConfig = MonkeyConfig;', payload)
        self.assertIn('Bypass All Shortlinks', payload)
        self.assertLess(payload.index('GM_xmlhttpRequest = details'), payload.index('new MonkeyConfig('))
        self.assertIn(json.dumps(shortlinks.source_info()['userscript_version']), payload)

    def test_page_actions(self):
        start = 'https://ouo.io/a'
        self.assertEqual(shortlink_browser.page_action(start, start), 'inject')
        self.assertEqual(shortlink_browser.page_action('https://pixeldrain.com/u/x', start), 'capture')
        self.assertEqual(shortlink_browser.page_action('https://cdn.example/f/book.epub', start), 'capture')
        self.assertEqual(shortlink_browser.page_action('https://blog.example/post', start), 'offer')
        self.assertIsNone(shortlink_browser.page_action('about:blank', start))

    def test_capture_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            actions = shortlink_browser.ShortlinkActions('https://ouo.io/a', folder)
            self.assertEqual(actions.capture('https://pixeldrain.com/u/x'), 'Sent to Media Intake')
            self.assertEqual(actions.capture('https://pixeldrain.com/u/x'), 'Already sent to Media Intake')
            with self.assertRaises(ValueError):
                actions.capture('file:///C:/secret.txt')
            seen = set()
            records = shortlink_browser.read_captures(folder, seen)
            self.assertEqual([(r['source_url'], r['final_url']) for r in records],
                             [('https://ouo.io/a', 'https://pixeldrain.com/u/x')])
            self.assertEqual(shortlink_browser.read_captures(folder, seen), [])

    def test_gm_xhr_strips_credentials_and_rejects_non_web_targets(self):
        sent = []
        def opener(request, timeout):
            sent.append(request)
            return Reply(b'{"ok": true}')
        actions = shortlink_browser.ShortlinkActions('https://ouo.io/a', tempfile.gettempdir(), opener=opener)
        reply = actions.gm_xhr({'url': 'https://api.example/x', 'method': 'post', 'data': 'a=1',
                                'headers': {'Cookie': 'sid=1', 'Authorization': 'x', 'X-Requested-With': 'XMLHttpRequest'}})
        self.assertEqual((reply['status'], reply['responseText'], reply['finalUrl']),
                         (200, '{"ok": true}', 'https://api.example/final'))
        request = sent[0]
        self.assertEqual((request.get_method(), request.data), ('POST', b'a=1'))
        self.assertIsNone(request.get_header('Cookie'))
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(request.get_header('X-requested-with'), 'XMLHttpRequest')
        for bad in ({'url': 'file:///C:/x'}, {'url': 'https://u:p@h.example/'}, {'url': 'https://h.example', 'method': 'PUT'}):
            with self.assertRaises(ValueError):
                actions.gm_xhr(bad)


if __name__ == '__main__':
    unittest.main()
