"""An interactive IRC channel session for the GUI console window.

The user types commands (for example ``@search title author``) and sees the
channel; the session takes care of the protocol:

- registration with nickname-clash retries, PING/PONG and joining the channel;
- search-result lists offered over DCC (.zip, or .txt) are received and parsed
  into selectable entries, delivered as a ``results`` event;
- a book offered over DCC after the user requested it is reported as an
  ``offer`` event; the GUI decides whether to accept it (``accept_offer``).

Events go to ``on_event(kind, payload)`` from the session thread; the GUI must
marshal them to its own thread.
"""
import io
import queue
import re
import socket
import struct
import threading
import time
from pathlib import Path

from .irc import (_irc_ssl_context, parse_offer, parse_search_index, receive_dcc_file, send_line,
                  send_user_command)

INDEX_SUFFIXES = ('.zip', '.txt')
MAX_INDEX_BYTES = 5_000_000
_SPEAKER = re.compile(r'^:([^!\s]+)(?:![^\s]*)?\s+(PRIVMSG|NOTICE)\s+(\S+)\s+:(.*)$')


def format_line(line):
    """A readable chat line, or None for protocol noise."""
    match = _SPEAKER.match(line)
    if match:
        speaker, verb, target, text = match.groups()
        if text.startswith('\x01'):
            return None  # CTCP (DCC offers are reported separately)
        text = re.sub(r'\x03\d{0,2}(?:,\d{1,2})?|[\x02\x0f\x16\x1d\x1f]', '', text)  # strip mIRC colours
        return f'-{speaker}- {text}' if verb == 'NOTICE' else f'<{speaker}> {text}'
    numeric = re.match(r'^:\S+\s+(\d{3})\s+\S+\s+(.*)$', line)
    if numeric and numeric.group(1) in ('332', '372', '375', '376', '001', '002', '003', '366'):
        return '* ' + numeric.group(2).lstrip(':')
    return None


class IrcSession(threading.Thread):
    def __init__(self, config, on_event, connect=None):
        super().__init__(daemon=True, name=f"irc-{config.get('server')}")
        self.config = dict(config)
        self.on_event = on_event
        self.connect_fn = connect or socket.create_connection
        self.outgoing = queue.Queue()
        self.offers = {}
        self.stopped = threading.Event()
        self.nick = self.config.get('nick') or 'MediaIntake'
        self.sock = None

    # Called from the GUI thread.
    def send(self, text):
        self.outgoing.put(str(text))

    def close(self):
        self.stopped.set()
        self.outgoing.put(None)

    def accept_offer(self, offer_id, folder):
        offer = self.offers.pop(offer_id, None)
        if offer is None:
            return
        def receive():
            try:
                path = receive_dcc_file(offer, folder, progress=lambda done, total:
                                        self.on_event('progress', (offer_id, done, total)))
                self.on_event('saved', {'offer_id': offer_id, 'path': str(path), 'offer': offer})
            except Exception as exc:
                self.on_event('error', f"Transfer of {offer['filename']} failed: {exc}")
        threading.Thread(target=receive, daemon=True).start()

    def decline_offer(self, offer_id):
        self.offers.pop(offer_id, None)

    # Session thread.
    def _write(self, line):
        send_line(self.sock, line)

    def _send_user_text(self, text):
        text = text.strip()
        if not text:
            return
        if text.startswith('/'):
            send_user_command(self.sock, text, self.config)
        else:
            # Plain text, including @search and !Bot requests, goes to the channel.
            self._write(f"PRIVMSG {self.config['channel']} :{text}")
        self.on_event('line', f'<{self.nick}> {text}')

    def _receive_index(self, offer):
        def run():
            try:
                buffer = io.BytesIO()
                with self.connect_fn((offer['host'], offer['port']), timeout=30) as dcc:
                    dcc.settimeout(int(self.config.get('timeout', 90)))
                    while buffer.tell() < offer['size']:
                        chunk = dcc.recv(min(65536, offer['size'] - buffer.tell()))
                        if not chunk:
                            raise ConnectionError('Search results transfer ended early')
                        buffer.write(chunk)
                        dcc.sendall(struct.pack('!I', buffer.tell() & 0xffffffff))
                entries = parse_search_index(buffer.getvalue(), self.config, limit=2000)
                self.on_event('results', {'filename': offer['filename'], 'sender': offer['sender'],
                                          'entries': entries})
            except Exception as exc:
                self.on_event('error', f"Could not read search results {offer['filename']}: {exc}")
        threading.Thread(target=run, daemon=True).start()

    def run(self):
        config = self.config
        server, tls = config['server'], config.get('tls', True)
        port = int(config.get('port', 6697 if tls else 6667))
        try:
            self.on_event('status', f'Connecting to {server}:{port}{" (TLS)" if tls else ""}…')
            plain = self.connect_fn((server, port), timeout=25)
            plain.settimeout(1.0)
            self.sock = _irc_ssl_context(config).wrap_socket(plain, server_hostname=server) if tls else plain
            self._write('NICK ' + self.nick)
            self._write('USER ' + self.nick + ' 0 * :Media Intake')
            joined, attempts, buffer = False, 0, b''
            while not self.stopped.is_set():
                while True:
                    try:
                        text = self.outgoing.get_nowait()
                    except queue.Empty:
                        break
                    if text is None:
                        break
                    if not joined:
                        self.on_event('error', 'Not in the channel yet; wait for the join to finish')
                        continue
                    try:
                        self._send_user_text(text)
                    except ValueError as exc:
                        self.on_event('error', str(exc))
                try:
                    data = self.sock.recv(4096)
                except (socket.timeout, TimeoutError):
                    continue
                if not data:
                    raise ConnectionError('The server closed the connection')
                buffer += data
                *lines, buffer = buffer.split(b'\n')
                for raw in lines:
                    line = raw.decode('utf-8', 'replace').rstrip('\r')
                    if line.startswith('PING '):
                        self._write('PONG ' + line[5:])
                        continue
                    if re.search(r'\s433\s', line) and not joined and attempts < 3:
                        attempts += 1
                        self.nick = f"{(config.get('nick') or 'MediaIntake')[:12]}{int(time.time() * 1000) % 1000:03d}"
                        self._write('NICK ' + self.nick)
                        continue
                    if re.search(r'\s(?:403|432|464|471|473|474|475|477)\s', line):
                        raise ConnectionError('The server or channel refused the connection: ' + line[:250])
                    if re.search(r'\s001\s', line) and not joined:
                        self._write('JOIN ' + config['channel'])
                    if re.search(r'\s366\s', line) and not joined:
                        joined = True
                        self.on_event('joined', {'channel': config['channel'], 'nick': self.nick})
                    try:
                        offer = parse_offer(line, None, max_bytes=10 * 1024 ** 3)
                    except ValueError:
                        offer = None
                    if offer:
                        suffix = Path(offer['filename']).suffix.lower()
                        if suffix in INDEX_SUFFIXES and offer['size'] <= MAX_INDEX_BYTES:
                            self.on_event('status', f"Receiving search results {offer['filename']} from {offer['sender']}…")
                            self._receive_index(offer)
                        else:
                            offer_id = f"{offer['sender']}:{offer['filename']}:{time.time():.3f}"
                            self.offers[offer_id] = offer
                            self.on_event('offer', {'offer_id': offer_id, **offer})
                        continue
                    shown = format_line(line)
                    if shown:
                        self.on_event('line', shown)
        except Exception as exc:
            if not self.stopped.is_set():
                self.on_event('error', str(exc))
        finally:
            try:
                if self.sock is not None:
                    try:
                        self._write('QUIT :Media Intake')
                    except Exception:
                        pass
                    self.sock.close()
            finally:
                self.on_event('closed', None)
