import io
import json
import socketserver
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from media_intake.core import connect, download
from media_intake.irc import (configured_search_sources, enqueue_irc, irc_source_priority_label,
                              parse_offer, search_configured_sources, send_user_command,
                              parse_search_index, search_irc, discover_irc_channels,
                              _irc_ssl_context, receive_offer)
from media_intake.sources import (parse_site_results, resolve_detail, search_forum_threads,
                                  search_openlibrary)


def epub_bytes():
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('book.txt', 'A test transfer')
    return data.getvalue()


class Transfer(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(self.server.payload)
        self.server.ack = self.request.recv(4)


class IRC(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        f = self.request.makefile('rb')
        for _ in range(2): f.readline()
        self.request.sendall(b':server 001 Test :Welcome\r\n')
        self.server.lines.append(f.readline().decode())
        self.request.sendall(b':server 366 Test #books :End of names\r\n')
        self.server.lines.append(f.readline().decode())
        offer = f':BookBot!bot@localhost PRIVMSG Test :\x01DCC SEND "book.epub" 2130706433 {self.server.dcc_port} {len(self.server.payload)}\x01\r\n'
        self.request.sendall(offer.encode())
        try: f.readline()
        except TimeoutError: pass


class SearchIRC(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        f = self.request.makefile('rb')
        for _ in range(2): f.readline()
        self.request.sendall(b':server 001 Test :Welcome\r\n')
        f.readline()
        self.request.sendall(b':server 366 Test #books :End of names\r\n')
        self.server.command = f.readline().decode()
        self.request.sendall(f':IndexBot!bot@localhost PRIVMSG Test :\x01DCC SEND "results.zip" 2130706433 {self.server.dcc_port} {len(self.server.payload)}\x01\r\n'.encode())
        try: f.readline()
        except TimeoutError: pass


class ListIRC(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        reader = self.request.makefile('rb')
        reader.readline()  # NICK
        reader.readline()  # USER
        self.request.sendall(b':server 001 Test :Welcome\r\n')
        self.server.command = reader.readline().decode()
        self.request.sendall(b'PING :channel-list-check\r\n')
        self.server.pong = reader.readline().decode()
        self.request.sendall(
            b':server 322 Test #ebooks 42 :Public books channel\r\n'
            b':server 322 Test #Bookz 8 :Public domain archive\r\n'
            b':server 323 Test :End of /LIST\r\n')


class RejectChannelIRC(socketserver.BaseRequestHandler):
    def handle(self):
        reader = self.request.makefile('rb')
        reader.readline()
        reader.readline()
        self.request.sendall(b':server 001 Test :Welcome\r\n')
        self.server.join_command = reader.readline().decode()
        self.request.sendall(b':server 403 Test #missing :No such channel\r\n')


class SourceAndIRCTest(unittest.TestCase):
    def test_irc_tls_verification_is_enabled_by_default_and_can_be_explicitly_disabled(self):
        self.assertTrue(_irc_ssl_context({}).check_hostname)
        unverified = _irc_ssl_context({'verify_tls': False})
        self.assertFalse(unverified.check_hostname)

    def test_irc_channel_discovery_sends_list_and_parses_checkbox_entries(self):
        irc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), ListIRC)
        irc.command, irc.pong = None, None
        thread = threading.Thread(target=irc.serve_forever, daemon=True)
        thread.start()
        try:
            channels = discover_irc_channels({
                'server': '127.0.0.1', 'port': irc.server_address[1],
                'tls': False, 'nick': 'Test', 'timeout': 5,
            })
            self.assertEqual(irc.command, 'LIST\r\n')
            self.assertEqual(irc.pong, 'PONG :channel-list-check\r\n')
            self.assertEqual(channels, [
                {'channel': '#Bookz', 'users': 8, 'topic': 'Public domain archive'},
                {'channel': '#ebooks', 'users': 42, 'topic': 'Public books channel'},
            ])
        finally:
            irc.shutdown()
            irc.server_close()
            thread.join()

    def test_irc_channel_discovery_can_limit_the_server_list(self):
        irc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), ListIRC)
        irc.command, irc.pong = None, None
        thread = threading.Thread(target=irc.serve_forever, daemon=True)
        thread.start()
        try:
            channels = discover_irc_channels({
                'server': '127.0.0.1', 'port': irc.server_address[1],
                'tls': False, 'nick': 'Test', 'channel_mask': '#Bookz,#ebooks',
                'timeout': 5,
            })
            self.assertEqual(irc.command, 'LIST #Bookz,#ebooks\r\n')
            self.assertEqual([row['channel'] for row in channels], ['#Bookz', '#ebooks'])
        finally:
            irc.shutdown()
            irc.server_close()
            thread.join()

    def test_irc_channel_discovery_rejects_unsafe_list_masks(self):
        with patch('media_intake.irc.socket.create_connection') as create_connection:
            with self.assertRaisesRegex(ValueError, 'channel listing filter'):
                discover_irc_channels({
                    'server': '127.0.0.1', 'port': 6667, 'tls': False,
                    'nick': 'Test', 'channel_mask': '#books\r\nQUIT',
                })
            create_connection.assert_not_called()

    def test_irc_channel_rejection_is_reported_without_waiting_for_search_timeout(self):
        irc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), RejectChannelIRC)
        irc.join_command = None
        thread = threading.Thread(target=irc.serve_forever, daemon=True)
        thread.start()
        try:
            config = {'server': '127.0.0.1', 'port': irc.server_address[1],
                      'tls': False, 'nick': 'Test', 'channel': '#missing', 'timeout': 5}
            with self.assertRaisesRegex(ConnectionError, 'No such channel'):
                with receive_offer(config):
                    self.fail('A rejected channel cannot provide an offer.')
            self.assertEqual(irc.join_command, 'JOIN #missing\r\n')
        finally:
            irc.shutdown()
            irc.server_close()
            thread.join()

    def test_irc_search_uses_saved_per_server_priority(self):
        options = {'irc_sources': [
            {'name': 'Primary', 'server': 'irc.primary.test', 'port': 6667,
             'tls': False, 'nick': 'Reader', 'channel': '#books', 'bot': 'IndexBot',
             'categories': ['Books']},
            {'name': 'Backup', 'server': 'irc.backup.test', 'port': 6667,
             'tls': False, 'nick': 'Reader', 'channel': '#ebooks', 'bot': 'IndexBot',
             'categories': ['Books']},
        ]}
        priority = [irc_source_priority_label('Backup', options['irc_sources'][1]),
                    irc_source_priority_label('Primary', options['irc_sources'][0])]
        ordered = configured_search_sources(options, 'Books', priority=priority)
        self.assertEqual([name for name, _config in ordered], ['Backup', 'Primary'])

        def fake_search(_query, config, limit=100):
            if config['server'] == 'irc.backup.test':
                return [{'title': 'Example', 'author': 'Writer', 'format': '.epub'}]
            return []

        progress = []
        with patch('media_intake.irc.search_irc', side_effect=fake_search):
            results, errors = search_configured_sources(
                'Example', options, 'Books', {'.epub'}, priority=priority,
                progress_callback=progress.append)
        self.assertEqual(errors, [])
        self.assertEqual(results[0]['irc_source_name'], 'Backup')
        self.assertEqual(results[0]['download_priority_label'], priority[0])
        self.assertEqual(progress, [priority[0]])

    def test_forum_search_exposes_thread_references_only(self):
        markup = '''
          <div class="topiclist">
            <a class="topictitle" href="/viewtopic.php?f=7&t=123&sid=session#p789" title="Sample Book">Sample Book</a>
            <div class="post-snippet">
              <a href="/viewtopic.php?t=456">mentioned thread</a>
              <a href="https://files.example.invalid/file/secret">external file link</a>
            </div>
            <a href="/viewtopic.php?t=789">unrelated sidebar thread</a>
            <a href="/viewforum.php?f=8">Books forum</a>
            <a href="https://elsewhere.example/threads/other.99/">Other forum</a>
          </div>
        '''
        with tempfile.TemporaryDirectory() as tmp:
            html = Path(tmp) / 'search.html'
            html.write_text(markup, encoding='utf-8')
            rows = search_forum_threads('sample book', html_path=html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Sample Book')
        self.assertEqual(rows[0]['url'], 'https://fora.snahp.eu/viewtopic.php?t=123')
        self.assertTrue(rows[0]['forum_thread'])
        self.assertEqual(rows[0]['format'], 'thread')

    def test_forum_403_points_to_browser_snapshot_fallback(self):
        with patch('media_intake.sources.urllib.request.build_opener') as build_opener:
            build_opener.return_value.open.side_effect = HTTPError(
                'https://fora.snahp.eu/search.php?keywords=test', 403, 'Forbidden', {}, None)
            with self.assertRaisesRegex(ValueError, 'normal browser and load a saved results page'):
                search_forum_threads('test')

    def test_site_page_and_detail_parsing(self):
        md5 = 'a' * 32
        anna = f'<a href="/md5/{md5}">Example Book</a><a href="/about">Help</a>'
        libgen = f'<a href="/book/index.php?md5={md5}">Another Book</a>'
        self.assertEqual(parse_site_results('annas', anna, 'https://annas-archive.pk/search?q=test')[0]['title'], 'Example Book')
        self.assertEqual(parse_site_results('libgen', libgen, 'https://libgen.rs/search.php?req=test')[0]['title'], 'Another Book')
        row = f'''<tr><td><a href="edition.php?id=12">The Way of Kings</a><br>9781429992800</td>
                  <td>Brandon Sanderson,</td><td>Tom Doherty Associates</td><td>2015</td>
                  <td>English</td><td>epub</td><td><a href="/ads.php?md5={md5}">1</a></td></tr>'''
        result = parse_site_results('libgen', row, 'https://libgen.gl/index.php?req=test')[0]
        self.assertEqual((result['title'], result['author'], result['format'], result['isbn']),
                         ('The Way of Kings', 'Brandon Sanderson', '.epub', '9781429992800'))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'detail.html'
            p.write_text('<a href="/files/book.epub">EPUB</a><a href="/slow_download/123">Mirror</a>')
            links = resolve_detail('annas', 'https://annas-archive.pk/md5/' + md5, 'Example Book', ext='.epub', html_path=p)
            self.assertEqual(len(links), 2)
            self.assertEqual(links[0]['url'], 'https://annas-archive.pk/files/book.epub')

    def test_openlibrary_file_resolution(self):
        def fake_get(url, max_bytes=10_000_000):
            if 'search.json' in url:
                return json.dumps({'docs': [{'title': 'Example', 'author_name': ['Writer'], 'ia': ['exampleid']}]}).encode(), url
            return json.dumps({'files': [{'name': 'exampleid.epub', 'private': 'false'}]}).encode(), url
        with patch('media_intake.sources.http_get', fake_get):
            items = search_openlibrary('Example', limit=1)
        self.assertEqual(items[0]['url'], 'https://archive.org/download/exampleid/exampleid.epub')

    def test_openlibrary_ignores_encrypted_loan_files(self):
        def fake_get(url, max_bytes=10_000_000):
            if 'search.json' in url:
                return json.dumps({'docs': [{'title': 'Example', 'ia': ['loanid']}]}).encode(), url
            return json.dumps({'metadata': {'access-restricted-item': 'true'},
                               'files': [{'name': 'loanid_encrypted.pdf', 'format': 'ACS Encrypted PDF'}]}).encode(), url
        with patch('media_intake.sources.http_get', fake_get):
            self.assertEqual(search_openlibrary('Example', limit=1), [])

    def test_irc_dcc_transfer(self):
        payload = epub_bytes()
        dcc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Transfer)
        dcc.payload, dcc.ack = payload, None
        irc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), IRC)
        irc.dcc_port, irc.payload, irc.lines = dcc.server_address[1], payload, []
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (dcc, irc)]
        for t in threads: t.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with closing(connect(tmp)) as db:
                    config = {'server': '127.0.0.1', 'port': irc.server_address[1], 'tls': False,
                              'nick': 'Test', 'channel': '#books', 'bot': 'BookBot', 'pack': 42,
                              'title': 'IRC Example', 'author': 'Writer', 'format': '.epub', 'timeout': 5}
                    row = enqueue_irc(db, config)
                    result = download(db, row['id'], tmp)
                    self.assertEqual(Path(result['file_path']).read_bytes(), payload)
                    self.assertEqual(result['state'], 'downloaded')
                    self.assertIn('PRIVMSG BookBot :xdcc send #42', ''.join(irc.lines))
                    self.assertEqual(int.from_bytes(dcc.ack, 'big'), len(payload))
        finally:
            for s in (irc, dcc): s.shutdown(); s.server_close()
            for t in threads: t.join()

    def test_irc_offer_from_other_bot_is_ignored(self):
        offer = ':NotTheBot!x@y PRIVMSG Me :\x01DCC SEND book.epub 2130706433 1234 45\x01'
        self.assertIsNone(parse_offer(offer, 'BookBot'))

    def test_custom_server_command(self):
        class Socket:
            def __init__(self): self.sent = []
            def sendall(self, data): self.sent.append(data)
        sock = Socket()
        send_user_command(sock, '/msg {channel} !get {pack}', {'channel': '#books', 'bot': 'BookBot', 'pack': 42})
        self.assertEqual(sock.sent, [b'PRIVMSG #books :!get 42\r\n'])

    def test_search_index_to_download(self):
        index = io.BytesIO()
        with zipfile.ZipFile(index, 'w') as z:
            z.writestr('listing.txt', '!BookBot Mary Shelley - Frankenstein.epub :: INFO :: 123 KB\n')
            z.writestr('../ignored.epub', b'unsafe archive entry')
        config = {'server': '127.0.0.1', 'tls': False, 'nick': 'Test', 'channel': '#books', 'timeout': 5}
        dcc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Transfer)
        dcc.payload, dcc.ack = index.getvalue(), None
        irc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), SearchIRC)
        irc.dcc_port, irc.payload, irc.command = dcc.server_address[1], dcc.payload, None
        config['port'] = irc.server_address[1]
        threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (dcc, irc)]
        for t in threads: t.start()
        try:
            result = search_irc('Frankenstein', config)
            self.assertIn('@search Frankenstein', irc.command)
            self.assertEqual((result[0]['title'], result[0]['author']), ('Frankenstein', 'Mary Shelley'))
            self.assertEqual(result[0]['irc']['request'], '!BookBot Mary Shelley - Frankenstein.epub')
            self.assertEqual(len(result), 1)
            with tempfile.TemporaryDirectory() as tmp:
                with closing(connect(tmp)) as db:
                    row = enqueue_irc(db, result[0]['irc'])
                    self.assertEqual(row['state'], 'queued')
        finally:
            for s in (irc, dcc): s.shutdown(); s.server_close()
            for t in threads: t.join()


if __name__ == '__main__': unittest.main()
