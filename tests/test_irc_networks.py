"""IRC network presets, nickname retries, early "no matches", search-all and the console session."""
import io
import socketserver
import threading
import time
import unittest
import zipfile

from media_intake.irc import (EBOOK_NETWORK_PRESETS, ensure_ebook_presets, parse_search_text, search_all_sources,
                              search_irc)
from media_intake.irc_session import IrcSession, format_line


class Transfer(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(self.server.payload)
        try:
            self.request.recv(4)
        except OSError:
            pass


def results_zip(lines):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('SearchBot_results.txt', '\n'.join(lines))
    return data.getvalue()


class ScriptedIRC(socketserver.BaseRequestHandler):
    """NICK/USER, optional 433 first, 001, JOIN -> 366, then the scripted reply to the first command."""

    def handle(self):
        self.request.settimeout(5)
        reader = self.request.makefile('rb')
        server = self.server
        nick = reader.readline().decode().split()[1]
        reader.readline()  # USER
        if server.nick_taken:
            self.request.sendall(f':server 433 * {nick} :Nickname is already in use\r\n'.encode())
            nick = reader.readline().decode().split()[1]
            server.retried_nick = nick
        self.request.sendall(f':server 001 {nick} :Welcome\r\n'.encode())
        reader.readline()  # JOIN
        self.request.sendall(f':server 366 {nick} #ebooks :End of /NAMES list\r\n'.encode())
        server.command = reader.readline().decode()
        reply = server.reply.replace('{nick}', nick).replace('{dcc}', str(server.dcc_port))
        self.request.sendall(reply.encode())
        try:
            while reader.readline():
                pass
        except OSError:
            pass


def irc_server(reply, nick_taken=False, payload=b''):
    dcc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Transfer)
    dcc.payload = payload
    irc = socketserver.ThreadingTCPServer(('127.0.0.1', 0), ScriptedIRC)
    irc.reply, irc.nick_taken, irc.dcc_port, irc.command, irc.retried_nick = reply, nick_taken, dcc.server_address[1], None, None
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (dcc, irc)]
    for thread in threads:
        thread.start()
    def stop():
        for s in (irc, dcc):
            s.shutdown()
            s.server_close()
    return irc, stop


LISTING = ['!DukeLupus Mary Shelley - Frankenstein (retail).epub ::INFO:: 412.8KB',
           '!Oatmeal Mary Shelley - Frankenstein.mobi ::INFO:: 300KB',
           'Search results for frankenstein, 2 matches']


class PresetTests(unittest.TestCase):
    def test_presets_are_added_once_and_fix_existing_entries(self):
        sources, applied, added = ensure_ebook_presets([], [], nick='MIReader0001')
        self.assertEqual(added, ['IRCHighway #ebooks', 'Undernet #bookz'])
        highway, undernet = sources
        self.assertEqual((highway['server'], highway['port'], highway['tls'], highway['verify_tls'], highway['channel']),
                         ('irc.irchighway.net', 6697, True, False, '#ebooks'))
        self.assertEqual((undernet['server'], undernet['port'], undernet['tls'], undernet['channel']),
                         ('irc.undernet.org', 6667, False, '#bookz'))
        self.assertEqual(highway['categories'], ['Books', 'Light novels'])
        # Removed by the user: not added again.
        again, applied, added = ensure_ebook_presets([undernet], applied)
        self.assertEqual((len(again), added), (1, []))
        # A manual IRCHighway entry gets the certificate setting it needs.
        manual = [{'server': 'IRC.irchighway.net', 'channel': '#EBOOKS', 'tls': True}]
        fixed, _applied, added = ensure_ebook_presets(manual, ['undernet-bookz'])
        self.assertEqual((added, fixed[0]['verify_tls'], fixed[0]['port']), ([], False, 6697))
        self.assertEqual({p['preset'] for p in EBOOK_NETWORK_PRESETS}, {'irchighway-ebooks', 'undernet-bookz'})

    def test_parse_text_results_keeps_size(self):
        entries = parse_search_text('\n'.join(LISTING), {'server': 'irc.example', 'channel': '#ebooks'})
        self.assertEqual([(e['bot'], e['title'], e['format'], e['size_text']) for e in entries],
                         [('DukeLupus', 'Frankenstein (retail)', '.epub', '412.8KB'),
                          ('Oatmeal', 'Frankenstein', '.mobi', '300KB')])
        self.assertEqual(entries[0]['irc']['request'], '!DukeLupus Mary Shelley - Frankenstein (retail).epub')


class SearchTests(unittest.TestCase):
    def config(self, port):
        return {'server': '127.0.0.1', 'port': port, 'tls': False, 'nick': 'Reader', 'channel': '#ebooks', 'timeout': 5}

    def test_no_matches_ends_the_search_at_once_after_a_nick_retry(self):
        irc, stop = irc_server(':Search!s@h NOTICE {nick} :Sorry, your search for "zzz" returned no matches.\r\n',
                               nick_taken=True)
        try:
            started = time.monotonic()
            self.assertEqual(search_irc('zzz', self.config(irc.server_address[1])), [])
            self.assertLess(time.monotonic() - started, 4)
            self.assertTrue(irc.retried_nick.startswith('Reader') and irc.retried_nick != 'Reader')
            self.assertIn('PRIVMSG #ebooks :@search zzz', irc.command)
        finally:
            stop()

    def test_search_all_sources_collects_every_server(self):
        offer = ':SearchOok!s@h PRIVMSG {nick} :\x01DCC SEND results.zip 2130706433 {dcc} %d\x01\r\n'
        payload = results_zip(LISTING)
        first, stop_first = irc_server(offer % len(payload), payload=payload)
        empty, stop_empty = irc_server(':Search!s@h NOTICE {nick} :Sorry, no matches found\r\n')
        try:
            options = {'irc_sources': [
                {'name': 'Highway', 'categories': ['Books'], **self.config(first.server_address[1])},
                {'name': 'Empty', 'categories': ['Books'], **self.config(empty.server_address[1])}]}
            results, errors = search_all_sources('frankenstein', options, allowed_formats={'.epub'})
            self.assertEqual(errors, [])
            self.assertEqual([(r['title'], r['irc_source_name']) for r in results], [('Frankenstein (retail)', 'Highway')])
        finally:
            stop_first()
            stop_empty()


class ConsoleTests(unittest.TestCase):
    def test_console_session_search_results_and_book_offers(self):
        payload = results_zip(LISTING)
        reply = (':SearchOok!s@h NOTICE {nick} :Your search for frankenstein returned 2 matches\r\n'
                 ':SearchOok!s@h PRIVMSG {nick} :\x01DCC SEND results.zip 2130706433 {dcc} %d\x01\r\n'
                 ':DukeLupus!d@h PRIVMSG {nick} :\x01DCC SEND "Frankenstein.epub" 2130706433 {dcc} %d\x01\r\n'
                 % (len(payload), len(payload)))
        irc, stop = irc_server(reply, payload=payload)
        events, done = [], threading.Event()
        def on_event(kind, payload_):
            events.append((kind, payload_))
            if kind == 'joined':
                session.send('@search frankenstein')
            if kind in ('results', 'offer') and {'results', 'offer'} <= {k for k, _ in events}:
                done.set()
        session = IrcSession({'server': '127.0.0.1', 'port': irc.server_address[1], 'tls': False,
                              'nick': 'Reader', 'channel': '#ebooks', 'timeout': 5}, on_event)
        try:
            session.start()
            self.assertTrue(done.wait(10), events)
            self.assertIn('PRIVMSG #ebooks :@search frankenstein', irc.command)
            results = next(p for k, p in events if k == 'results')
            self.assertEqual(len(results['entries']), 2)
            self.assertIn('-SearchOok- Your search for frankenstein returned 2 matches', [p for k, p in events if k == 'line'])
            offer = next(p for k, p in events if k == 'offer')
            self.assertEqual((offer['filename'], offer['sender']), ('Frankenstein.epub', 'DukeLupus'))
        finally:
            session.close()
            session.join(5)
            stop()

    def test_format_line(self):
        self.assertEqual(format_line(':Bot!b@h PRIVMSG #ebooks :\x0304hello\x03 world'), '<Bot> hello world')
        self.assertEqual(format_line(':Bot!b@h NOTICE me :done'), '-Bot- done')
        self.assertIsNone(format_line(':Bot!b@h PRIVMSG me :\x01VERSION\x01'))
        self.assertIsNone(format_line(':server 353 me = #ebooks :a b c'))


if __name__ == '__main__':
    unittest.main()
