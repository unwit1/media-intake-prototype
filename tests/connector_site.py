"""A local stand-in for the connector websites: routes requests to fixture replies.

Connectors are pointed at http://127.0.0.1:<port> (allowed for local addresses), so
tests exercise the real HTTP code: headers, gzip, cookies, redirects, ranges,
dropped connections and error statuses.
"""
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time
import urllib.parse

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'connectors'


def fixture(name):
    return (FIXTURES / name).read_bytes()


class Reply:
    def __init__(self, body=b'', status=200, headers=None, content_type='text/html; charset=utf-8',
                 ranges=False, drop_after=None, delay=0.0, gzip_body=False):
        self.body = body.encode('utf-8') if isinstance(body, str) else body
        self.status, self.headers, self.content_type = status, dict(headers or {}), content_type
        self.ranges, self.drop_after, self.delay, self.gzip_body = ranges, drop_after, delay, gzip_body


class FakeSite:
    """routes: list of (method, path regex, reply or callable(request) -> Reply)."""

    def __init__(self):
        self.routes, self.requests = [], []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def _handle(self, method):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                parts = urllib.parse.urlsplit(self.path)
                request = {'method': method, 'path': parts.path, 'query': urllib.parse.parse_qs(parts.query),
                           'raw_query': parts.query, 'headers': dict(self.headers), 'body': body}
                owner.requests.append(request)
                reply = None
                for route_method, pattern, target in owner.routes:
                    if route_method in (method, '*') and re.fullmatch(pattern, parts.path):
                        reply = target(request) if callable(target) else target
                        if reply is not None:
                            break
                if reply is None:
                    reply = Reply(b'not found', 404)
                self._send(reply)

            def _send(self, reply):
                data, status, extra = reply.body, reply.status, dict(reply.headers)
                header = self.headers.get('Range')
                if reply.ranges:
                    extra['Accept-Ranges'] = 'bytes'
                    extra.setdefault('ETag', '"fixture"')
                    if header:
                        start, end = re.fullmatch(r'bytes=(\d+)-(\d*)', header).groups()
                        start, end = int(start), int(end or len(data) - 1)
                        extra['Content-Range'] = f'bytes {start}-{end}/{len(reply.body)}'
                        data, status = data[start:end + 1], 206
                if reply.gzip_body:
                    data = gzip.compress(data)
                    extra['Content-Encoding'] = 'gzip'
                self.send_response(status)
                self.send_header('Content-Type', reply.content_type)
                self.send_header('Content-Length', str(len(data)))
                for key, value in extra.items():
                    for item in (value if isinstance(value, list) else [value]):
                        self.send_header(key, item)
                self.end_headers()
                if self.command == 'HEAD':
                    return
                sent = 0
                for offset in range(0, len(data), 32 * 1024):
                    chunk = data[offset:offset + 32 * 1024]
                    if reply.drop_after is not None and sent + len(chunk) > reply.drop_after:
                        try:
                            self.wfile.write(chunk[:max(0, reply.drop_after - sent)])
                            self.wfile.flush()
                        finally:
                            self.close_connection = True
                            self.connection.shutdown(2)
                        return
                    if reply.delay:
                        time.sleep(reply.delay)
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        return
                    sent += len(chunk)

            def do_GET(self):
                self._handle('GET')

            def do_POST(self):
                self._handle('POST')

            def do_HEAD(self):
                self._handle('HEAD')

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def route(self, method, pattern, target):
        self.routes.insert(0, (method, pattern, target))
        return self

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def hits(self, path_pattern):
        return [request for request in self.requests if re.fullmatch(path_pattern, request['path'])]
