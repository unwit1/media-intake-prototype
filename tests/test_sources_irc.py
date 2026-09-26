import io
import json
import socketserver
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from media_intake.core import connect, download
from media_intake.irc import enqueue_irc, parse_offer, send_user_command, parse_search_index, search_irc
from media_intake.sources import parse_site_results, resolve_detail, search_openlibrary


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


class SourceAndIRCTest(unittest.TestCase):
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
                db = connect(tmp)
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
                db = connect(tmp)
                row = enqueue_irc(db, result[0]['irc'])
                self.assertEqual(row['state'], 'queued')
        finally:
            for s in (irc, dcc): s.shutdown(); s.server_close()
            for t in threads: t.join()


if __name__ == '__main__': unittest.main()
