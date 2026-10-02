"""HLS stream saving against a local playlist/segment server (no playback, no live services)."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from media_intake import streams, transfer
from media_intake.core import connect, download, enqueue

SEGMENTS = [bytes([n]) * 4000 for n in range(12)]


class StreamServer:
    def __init__(self):
        self.routes, self.requests, self.delay = {}, [], 0.0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split('?')[0]
                owner.requests.append(path)
                route = owner.routes.get(path)
                if route is None:
                    self.send_error(404)
                    return
                content_type, body = route() if callable(route) else route
                status = 200
                header = self.headers.get('Range')
                if header:
                    start, end = map(int, re.fullmatch(r'bytes=(\d+)-(\d+)', header).groups())
                    body, status = body[start:end + 1], 206
                if owner.delay and path.endswith('.ts'):
                    time.sleep(owner.delay)
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def media(self, path, segments, endlist=True, extra=''):
        lines = ['#EXTM3U', '#EXT-X-TARGETDURATION:4', '#EXT-X-MEDIA-SEQUENCE:0', extra]
        for index, _segment in enumerate(segments):
            lines += ['#EXTINF:4.0,', f'seg{index}.ts']
        if endlist:
            lines.append('#EXT-X-ENDLIST')
        folder = path.rsplit('/', 1)[0]
        self.routes[path] = ('application/vnd.apple.mpegurl', '\n'.join(l for l in lines if l).encode())
        for index, segment in enumerate(segments):
            self.routes[f'{folder}/seg{index}.ts'] = ('video/mp2t', segment)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


MASTER = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
high/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
mid/index.m3u8
'''


class ParserTests(unittest.TestCase):
    def test_master_media_and_encryption(self):
        master = streams.parse_playlist(MASTER, 'https://cdn.example/v/master.m3u8')
        self.assertEqual(streams.choose_variant(master['variants'])['uri'], 'https://cdn.example/v/high/index.m3u8')
        self.assertEqual(streams.choose_variant(master['variants'], max_height=720)['height'], 720)
        media = streams.parse_playlist('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-MAP:URI="init.mp4"\n'
                                       '#EXTINF:2,\n#EXT-X-BYTERANGE:100@0\nall.m4s\n#EXTINF:2,\n'
                                       '#EXT-X-BYTERANGE:50\nall.m4s\n#EXT-X-ENDLIST', 'https://c.example/a/p.m3u8')
        self.assertEqual([(s['sequence'], s['range']) for s in media['segments']], [(7, (0, 100)), (8, (100, 50))])
        self.assertEqual(media['init']['uri'], 'https://c.example/a/init.mp4')
        self.assertTrue(media['endlist'])
        for key in ('#EXT-X-KEY:METHOD=AES-128,URI="k"', '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="com.apple.streamingkeydelivery"'):
            with self.assertRaisesRegex(streams.StreamError, 'encrypted'):
                streams.parse_playlist(f'#EXTM3U\n{key}\n#EXTINF:2,\na.ts\n', 'https://c.example/p.m3u8')
        streams.parse_playlist('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\na.ts\n', 'https://c.example/p.m3u8')
        with self.assertRaisesRegex(streams.StreamError, 'Not an HLS'):
            streams.parse_playlist('<html>', 'https://c.example/p.m3u8')


class DownloadTests(unittest.TestCase):
    def test_vod_master_downloads_best_variant_and_joins_in_order(self):
        with StreamServer() as server, tempfile.TemporaryDirectory() as folder:
            server.routes['/v/master.m3u8'] = ('application/vnd.apple.mpegurl', MASTER.encode())
            server.media('/v/high/index.m3u8', SEGMENTS)
            server.media('/v/low/index.m3u8', [b'low'])
            reports = []
            path, ext = streams.download_hls(server.base + '/v/master.m3u8', folder, connections=4, ffmpeg='',
                                             progress=lambda *a: reports.append(a))
            self.assertEqual((ext, path.read_bytes()), ('.ts', b''.join(SEGMENTS)))
            self.assertNotIn('/v/low/index.m3u8', server.requests)
            self.assertIn('12/12 segments', reports[-1][2])

    def test_pause_keeps_segments_and_resume_fetches_only_the_rest(self):
        with StreamServer() as server, tempfile.TemporaryDirectory() as folder:
            server.media('/s/index.m3u8', SEGMENTS)
            server.delay = 0.05
            control = transfer.Control()
            with self.assertRaises(transfer.TransferPaused):
                streams.download_hls(server.base + '/s/index.m3u8', folder, connections=2, ffmpeg='', control=control,
                                     progress=lambda done, *_: done >= 12_000 and control.pause.set())
            saved = len(list(Path(folder, 'video').glob('*.seg')))
            self.assertGreaterEqual(saved, 3)
            self.assertLess(saved, 12)
            server.delay, server.requests[:] = 0, []
            path, _ext = streams.download_hls(server.base + '/s/index.m3u8', folder, ffmpeg='')
            self.assertEqual(path.read_bytes(), b''.join(SEGMENTS))
            self.assertEqual(len([r for r in server.requests if r.endswith('.ts')]), 12 - saved)

    def test_fmp4_byteranges_become_mp4(self):
        with StreamServer() as server, tempfile.TemporaryDirectory() as folder:
            blob = b'A' * 100 + b'B' * 50
            server.routes['/f/p.m3u8'] = ('application/vnd.apple.mpegurl', (
                '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2,\n#EXT-X-BYTERANGE:100@0\nall.m4s\n'
                '#EXTINF:2,\n#EXT-X-BYTERANGE:50\nall.m4s\n#EXT-X-ENDLIST').encode())
            server.routes['/f/init.mp4'] = ('video/mp4', b'INIT')
            server.routes['/f/all.m4s'] = ('video/iso.segment', blob)
            path, ext = streams.download_hls(server.base + '/f/p.m3u8', folder, ffmpeg='')
            self.assertEqual((ext, path.read_bytes()), ('.mp4', b'INIT' + blob))

    def test_live_playlist_records_until_it_ends(self):
        with StreamServer() as server, tempfile.TemporaryDirectory() as folder:
            calls = []
            def growing():
                # Two initial reads (variant check, track), then three more segments per refresh.
                calls.append(1)
                count = min(12, 3 * max(1, len(calls) - 1))
                server.media('/l/index.m3u8', SEGMENTS[:count], endlist=count == 12)
                route = server.routes['/l/index.m3u8']
                server.routes['/l/index.m3u8'] = growing
                return route
            server.media('/l/index.m3u8', SEGMENTS, endlist=True)  # registers the segment routes
            server.routes['/l/index.m3u8'] = growing
            reports = []
            path, ext = streams.download_hls(server.base + '/l/index.m3u8', folder, ffmpeg='', poll=0,
                                             progress=lambda *a: reports.append(a))
            self.assertEqual(path.read_bytes(), b''.join(SEGMENTS))
            self.assertTrue(any('Recording live stream' in r[2] for r in reports))

    def test_ffmpeg_remux_and_separate_audio(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            Path(args[-1]).write_bytes(b'MP4')
            return types.SimpleNamespace(returncode=0)
        with StreamServer() as server, tempfile.TemporaryDirectory() as folder:
            server.media('/r/index.m3u8', SEGMENTS[:2])
            path, ext = streams.download_hls(server.base + '/r/index.m3u8', folder, ffmpeg='ffmpeg.exe', run=fake_run)
            self.assertEqual((ext, path.name, path.read_bytes()), ('.mp4', 'stream.mp4', b'MP4'))
            self.assertEqual(calls[0][:6], ['ffmpeg.exe', '-y', '-loglevel', 'error', '-i', str(Path(folder) / 'joined-video.ts')])
            self.assertIn('copy', calls[0])
        with StreamServer() as server, tempfile.TemporaryDirectory() as folder:
            server.routes['/a/master.m3u8'] = ('application/vnd.apple.mpegurl', (
                '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",DEFAULT=YES,URI="audio/index.m3u8"\n'
                '#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720,AUDIO="aud"\nvideo/index.m3u8\n').encode())
            server.media('/a/video/index.m3u8', SEGMENTS[:2])
            server.media('/a/audio/index.m3u8', [b'au1', b'au2'])
            with self.assertRaisesRegex(streams.StreamError, 'install ffmpeg'):
                streams.download_hls(server.base + '/a/master.m3u8', folder, ffmpeg='')
            calls.clear()
            path, ext = streams.download_hls(server.base + '/a/master.m3u8', folder, ffmpeg='ffmpeg.exe', run=fake_run)
            self.assertEqual(ext, '.mp4')
            self.assertEqual(calls[0][calls[0].index('-map'):calls[0].index('-map') + 4], ['-map', '0:v:0', '-map', '1:a:0'])
            self.assertEqual((Path(folder) / 'joined-audio.ts').read_bytes(), b'au1au2')


class QueueTests(unittest.TestCase):
    def test_queue_saves_streams_by_extension_or_content_type(self):
        with StreamServer() as server, tempfile.TemporaryDirectory() as root, closing(connect(root)) as db, \
                patch('media_intake.streams.ffmpeg_path', return_value=None):
            server.media('/q/index.m3u8', SEGMENTS[:4])
            queued = enqueue(db, {'title': 'Lecture', 'author': 'A', 'url': server.base + '/q/index.m3u8',
                                  'format': '.mp4', 'source': 's'})
            result = download(db, queued['id'], root)
            self.assertTrue(result['file_path'].endswith('Lecture - A.ts'))
            self.assertEqual(Path(result['file_path']).read_bytes(), b''.join(SEGMENTS[:4]))
            self.assertFalse(list((Path(root) / 'downloads').glob('.partial-*')))
            # A link without .m3u8 is recognised from its playlist content type.
            server.routes['/play'] = server.routes['/q/index.m3u8']
            for index in range(4):
                server.routes[f'/seg{index}.ts'] = server.routes[f'/q/seg{index}.ts']
            other = enqueue(db, {'title': 'Talk', 'author': 'A', 'url': server.base + '/play',
                                 'format': '.mp4', 'source': 's'})
            result = download(db, other['id'], root)
            self.assertTrue(result['file_path'].endswith('Talk - A.ts'))
            metadata = json.loads(db.execute('SELECT metadata FROM items WHERE id=?', (other['id'],)).fetchone()[0])
            self.assertEqual((metadata['stream'], metadata['format']), ('hls', '.ts'))

    def test_encrypted_stream_fails_clearly_and_cancel_discards(self):
        with StreamServer() as server, tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            server.routes['/e/index.m3u8'] = ('application/vnd.apple.mpegurl',
                                             b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:4,\nseg0.ts\n#EXT-X-ENDLIST')
            queued = enqueue(db, {'title': 'Locked', 'author': 'A', 'url': server.base + '/e/index.m3u8',
                                  'format': '.mp4', 'source': 's'})
            with self.assertRaisesRegex(streams.StreamError, 'encrypted'):
                download(db, queued['id'], root)
            self.assertEqual(db.execute('SELECT state FROM items WHERE id=?', (queued['id'],)).fetchone()[0], 'failed')


if __name__ == '__main__':
    unittest.main()
