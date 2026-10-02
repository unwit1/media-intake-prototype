"""Jackett Torznab search and external download-manager handoff, without live services."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse

from media_intake import external_downloaders as ext
from media_intake import jackett
from media_intake.rights_evidence import ensure_rights_evidence, load_rights_evidence, save_rights_evidence

TORZNAB = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:torznab="http://torznab.com/schemas/2015/feed">
<channel><title>AggregateSearch</title>
<item>
  <title>Fixture Linux ISO 1.0</title>
  <guid>https://tracker.example/details/1</guid>
  <jackettindexer id="exampletracker">Example Tracker</jackettindexer>
  <comments>https://tracker.example/details/1</comments>
  <pubDate>Wed, 01 Oct 2026 10:00:00 +0000</pubDate>
  <size>1048576</size>
  <link>http://127.0.0.1:9117/dl/exampletracker/?jackett_apikey=SECRET&amp;path=abc&amp;file=Fixture</link>
  <enclosure url="http://127.0.0.1:9117/dl/exampletracker/?jackett_apikey=SECRET&amp;path=abc&amp;file=Fixture" length="1048576" type="application/x-bittorrent" />
  <torznab:attr name="category" value="4000" />
  <torznab:attr name="seeders" value="12" />
  <torznab:attr name="peers" value="15" />
  <torznab:attr name="infohash" value="0123456789abcdef0123456789abcdef01234567" />
</item>
<item>
  <title>Fixture Public Domain Audiobook</title>
  <guid>https://other.example/t/2</guid>
  <jackettindexer id="other">Other</jackettindexer>
  <link>magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef&amp;dn=Fixture</link>
  <torznab:attr name="seeders" value="40" />
  <torznab:attr name="size" value="2048" />
</item>
<item><title>No link</title></item>
</channel></rss>'''


class JackettTests(unittest.TestCase):
    def test_search_builds_torznab_request_and_parses_results(self):
        requested = []
        def fetch(url, timeout):
            requested.append(url)
            return TORZNAB
        results = jackett.search_jackett('fixture iso', 'http://127.0.0.1:9117/', 'SECRET',
                                         category='Software', fetch=fetch)
        parts = urllib.parse.urlsplit(requested[0])
        self.assertEqual(parts.path, '/api/v2.0/indexers/all/results/torznab/api')
        self.assertEqual(urllib.parse.parse_qs(parts.query),
                         {'apikey': ['SECRET'], 't': ['search'], 'q': ['fixture iso'], 'cat': ['4000']})
        self.assertEqual([row['title'] for row in results],
                         ['Fixture Public Domain Audiobook', 'Fixture Linux ISO 1.0'])  # by seeders
        magnet, torrent = results
        self.assertTrue(magnet['handoff_url'].startswith('magnet:?xt=urn:btih:89ab'))
        self.assertEqual((magnet['seeders'], magnet['size'], magnet['indexer']), (40, 2048, 'Other'))
        self.assertIn('jackett_apikey=SECRET', torrent['handoff_url'])
        self.assertEqual((torrent['size'], torrent['peers'], torrent['category_ids']), (1048576, 15, [4000]))
        self.assertEqual(torrent['provider'], 'Jackett · Example Tracker')
        self.assertEqual(torrent['source'], 'https://tracker.example/details/1')
        self.assertTrue(all(row['source_key'] == 'jackett' and row['external_download'] for row in results))

    def test_error_responses_and_redaction(self):
        with self.assertRaisesRegex(jackett.JackettError, 'Invalid API Key'):
            jackett.search_jackett('x', 'http://127.0.0.1:9117', 'bad', fetch=lambda url, timeout:
                                   b'<error code="100" description="Invalid API Key" />')
        with self.assertRaisesRegex(jackett.JackettError, 'API key'):
            jackett.torznab_url('http://127.0.0.1:9117', '', 'x')
        with self.assertRaises(jackett.JackettError):
            jackett.torznab_url('ftp://host', 'k', 'x')
        self.assertEqual(jackett.redact('http://h/dl/x/?jackett_apikey=SECRET&file=a'),
                         'http://h/dl/x/?jackett_apikey=***&file=a')

    def test_connection_reports_search_modes(self):
        caps = b'<caps><searching><search available="yes"/><tv-search available="yes"/><movie-search available="no"/></searching></caps>'
        self.assertEqual(jackett.test_connection('http://127.0.0.1:9117', 'k', fetch=lambda url, timeout: caps),
                         ['search', 'tv-search'])


class FakeReply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.launched = []

    def opener(self, reply):
        def open_request(request, timeout):
            self.calls.append(request)
            return FakeReply(json.dumps(reply).encode())
        return open_request

    def popen(self, arguments, **kwargs):
        self.launched.append(arguments)

    def test_gopeed_creates_task_with_token_and_options(self):
        status = ext.hand_off({'title': 'Book', 'url': 'https://files.example/book.epub',
                               'source_filename': 'book.epub'},
                              {'downloader': 'gopeed', 'gopeed_url': 'http://127.0.0.1:9999',
                               'gopeed_token': 'tok', 'directory': 'D:/Downloads'},
                              opener=self.opener({'code': 0, 'msg': '', 'data': 'task-1'}))
        self.assertEqual(status, 'Sent to Gopeed (task task-1)')
        request = self.calls[0]
        self.assertEqual((request.get_method(), request.full_url), ('POST', 'http://127.0.0.1:9999/api/v1/tasks'))
        self.assertEqual(request.get_header('X-api-token'), 'tok')
        body = json.loads(request.data)
        self.assertEqual(body['req']['url'], 'https://files.example/book.epub')
        self.assertEqual(body['opts'], {'path': 'D:/Downloads', 'name': 'book.epub'})

    def test_gopeed_accepts_magnets_and_reports_api_errors(self):
        magnet = 'magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef'
        ext.hand_off({'handoff_url': magnet, 'source_filename': 'ignored'}, {'downloader': 'gopeed'},
                     opener=self.opener({'code': 0, 'data': 'bt'}))
        self.assertNotIn('name', json.loads(self.calls[0].data)['opts'])
        with self.assertRaisesRegex(ext.HandoffError, 'token'):
            ext.hand_off({'handoff_url': magnet}, {'downloader': 'gopeed'},
                         opener=self.opener({'code': 1001, 'msg': 'unauthorized'}))
        with self.assertRaisesRegex(ext.HandoffError, 'info hash'):
            ext.hand_off({'handoff_url': 'magnet:?dn=x'}, {'downloader': 'gopeed'})

    def test_xdm_command_line_and_torrent_refusal(self):
        status = ext.hand_off({'url': 'https://files.example/a.cbz', 'source_filename': 'a.cbz'},
                              {'downloader': 'xdm', 'xdm_path': r'"C:\Program Files\XDM\xdm-app.exe"',
                               'directory': r'D:\Comics'}, popen=self.popen)
        self.assertEqual(status, 'Sent to Xtreme Download Manager')
        self.assertEqual(self.launched[0], [r'C:\Program Files\XDM\xdm-app.exe', '--url',
                                            'https://files.example/a.cbz', '-o', str(Path(r'D:\Comics') / 'a.cbz')])
        with self.assertRaisesRegex(ext.HandoffError, 'Gopeed'):
            ext.hand_off({'handoff_url': 'http://127.0.0.1:9117/dl/x/?jackett_apikey=k&file=f'},
                         {'downloader': 'xdm', 'xdm_path': 'xdm-app.exe'}, popen=self.popen)

    def test_custom_command_substitutes_arguments_without_a_shell(self):
        ext.hand_off({'url': 'https://files.example/a b.epub'},
                     {'downloader': 'command', 'command': r'"C:\Tools\File Centipede\filecxx.exe" --add "{url}" --dir {dir}',
                      'directory': r'D:\Books'}, popen=self.popen)
        self.assertEqual(self.launched[0], [r'C:\Tools\File Centipede\filecxx.exe', '--add',
                                            'https://files.example/a b.epub', '--dir', r'D:\Books'])
        with self.assertRaisesRegex(ext.HandoffError, r'\{url\}'):
            ext.command_arguments('tool.exe --add', 'https://x.example/a')
        with self.assertRaisesRegex(ext.HandoffError, 'HTTP'):
            ext.hand_off({'url': 'file:///C:/secret'}, {'downloader': 'command', 'command': 'x {url}'},
                         popen=self.popen)

    def test_rights_evidence_gates_jackett_handoff(self):
        with tempfile.TemporaryDirectory() as root:
            path = ensure_rights_evidence(Path(root) / 'rights-evidence.json')
            document = load_rights_evidence(path)
            self.assertIs(document['sources']['jackett']['approved_for_download'], False)
            item = {'handoff_url': 'magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef',
                    'source_key': 'jackett'}
            with self.assertRaisesRegex(ext.HandoffError, 'jackett.approved_for_download'):
                ext.hand_off(item, {'downloader': 'gopeed'}, rights_path=path,
                             opener=self.opener({'code': 0, 'data': 't'}))
            self.assertEqual(self.calls, [])
            document['sources']['jackett']['approved_for_download'] = True
            save_rights_evidence(path, document)
            self.assertEqual(ext.hand_off(item, {'downloader': 'gopeed'}, rights_path=path,
                                          opener=self.opener({'code': 0, 'data': 't'})), 'Sent to Gopeed (task t)')


if __name__ == '__main__':
    unittest.main()
