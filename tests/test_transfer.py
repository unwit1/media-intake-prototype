"""Segmented/resumable transfer engine and its queue integration, against a local range server."""
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from media_intake import core, transfer
from media_intake.core import connect, download, enqueue

PAYLOAD = bytes(range(256)) * (48 * 1024)  # 12 MiB, enough for several segments


class RangeServer:
    def __init__(self, payload=PAYLOAD, ranges=True, etag='"v1"', content_type='application/octet-stream'):
        self.payload, self.ranges, self.etag, self.content_type = payload, ranges, etag, content_type
        self.requests, self.fail_next, self.delay = [], [], 0.0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(dict(self.headers))
                if owner.fail_next:
                    self.send_error(owner.fail_next.pop(0))
                    return
                data, status, extra = owner.payload, 200, {}
                header = self.headers.get('Range')
                if_range = self.headers.get('If-Range')
                if owner.ranges and header and (not if_range or if_range == owner.etag):
                    start, end = re.fullmatch(r'bytes=(\d+)-(\d*)', header).groups()
                    start, end = int(start), int(end or len(data) - 1)
                    data, status = data[start:end + 1], 206
                    extra['Content-Range'] = f'bytes {start}-{end}/{len(owner.payload)}'
                self.send_response(status)
                self.send_header('Content-Type', owner.content_type)
                self.send_header('Content-Length', str(len(data)))
                if owner.etag:
                    self.send_header('ETag', owner.etag)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                for offset in range(0, len(data), 64 * 1024):
                    if owner.delay:
                        time.sleep(owner.delay)
                    try:
                        self.wfile.write(data[offset:offset + 64 * 1024])
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        return

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f'http://127.0.0.1:{self.server.server_port}/file.bin'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class EngineTests(unittest.TestCase):
    def test_segmented_download_matches_bytes(self):
        with RangeServer() as server, tempfile.TemporaryDirectory() as folder:
            reports = []
            result = transfer.fetch(server.url, Path(folder) / 'a.part', max_bytes=1 << 30, connections=3,
                                    progress=lambda done, total, message: reports.append((done, total, message)))
            self.assertEqual(Path(result.path).read_bytes(), PAYLOAD)
            self.assertEqual((result.size, result.connections, result.resumed), (len(PAYLOAD), 3, False))
            ranges = [r['Range'] for r in server.requests if r.get('Range') != 'bytes=0-0']
            self.assertEqual(len(ranges), 3)
            self.assertTrue(all(r.get('If-Range') == '"v1"' for r in server.requests[1:]))
            self.assertFalse((Path(folder) / 'a.part.json').exists())
            self.assertTrue(any('3 connections' in message for _d, _t, message in reports))

    def test_pause_then_resume_from_checkpoint(self):
        with RangeServer() as server, tempfile.TemporaryDirectory() as folder:
            server.delay = 0.04
            part = Path(folder) / 'a.part'
            control = transfer.Control()
            def progress(done, total, message):
                if done > len(PAYLOAD) // 3:
                    control.pause.set()
            with self.assertRaises(transfer.TransferPaused):
                transfer.fetch(server.url, part, max_bytes=1 << 30, connections=4, control=control, progress=progress)
            saved, total = transfer.partial_progress(part)
            self.assertGreater(saved, 0)
            self.assertLess(saved, len(PAYLOAD))
            self.assertEqual(total, len(PAYLOAD))
            server.delay, server.requests[:] = 0, []
            result = transfer.fetch(server.url, part, max_bytes=1 << 30, connections=4)
            self.assertTrue(result.resumed)
            self.assertEqual(part.read_bytes(), PAYLOAD)
            fetched = sum(int(r['Range'].split('-')[1]) - int(r['Range'][6:].split('-')[0]) + 1
                          for r in server.requests if r['Range'] != 'bytes=0-0' and r['Range'].split('-')[1])
            self.assertLessEqual(fetched, len(PAYLOAD) - saved + 4)

    def test_changed_file_restarts_instead_of_mixing_versions(self):
        with RangeServer() as server, tempfile.TemporaryDirectory() as folder:
            server.delay = 0.01
            part = Path(folder) / 'a.part'
            control = transfer.Control()
            with self.assertRaises(transfer.TransferPaused):
                transfer.fetch(server.url, part, max_bytes=1 << 30, control=control,
                               progress=lambda done, *_: done > 1_000_000 and control.pause.set())
            server.delay, server.etag = 0, '"v2"'
            server.payload = PAYLOAD[::-1]
            result = transfer.fetch(server.url, part, max_bytes=1 << 30)
            self.assertFalse(result.resumed)
            self.assertEqual(part.read_bytes(), PAYLOAD[::-1])

    def test_retries_transient_errors_and_single_stream_without_ranges(self):
        with RangeServer(ranges=False) as server, tempfile.TemporaryDirectory() as folder:
            server.fail_next = [503, 502]
            with patch('media_intake.transfer.time.sleep'):
                result = transfer.fetch(server.url, Path(folder) / 'a.part', max_bytes=1 << 30, retries=3)
            self.assertEqual((result.connections, Path(result.path).read_bytes() == PAYLOAD), (1, True))
        with RangeServer() as server, tempfile.TemporaryDirectory() as folder:
            server.fail_next = [503, 503, 503]
            with patch('media_intake.transfer.time.sleep'), self.assertRaisesRegex(transfer.TransferError, 'retries'):
                transfer.fetch(server.url, Path(folder) / 'a.part', max_bytes=1 << 30, retries=2)

    def test_rejects_html_oversize_and_cancel_discards(self):
        with RangeServer(content_type='text/html') as server, tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(transfer.TransferError, 'HTML page'):
                transfer.fetch(server.url, Path(folder) / 'a.part', max_bytes=1 << 30)
        with RangeServer() as server, tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(transfer.TransferError, 'above'):
                transfer.fetch(server.url, Path(folder) / 'a.part', max_bytes=1000)
            server.delay = 0.01
            control = transfer.Control()
            part = Path(folder) / 'b.part'
            with self.assertRaises(transfer.TransferCancelled):
                transfer.fetch(server.url, part, max_bytes=1 << 30, control=control,
                               progress=lambda done, *_: done > 500_000 and control.cancel.set())
            self.assertFalse(part.exists())
            self.assertFalse(Path(str(part) + '.json').exists())

    def test_rate_limiter_caps_throughput(self):
        limiter = transfer.RateLimiter(2_000_000)
        start = time.monotonic()
        for _ in range(12):
            limiter.consume(500_000)
        self.assertGreaterEqual(time.monotonic() - start, 1.5)
        limiter.set_rate(0)
        start = time.monotonic()
        limiter.consume(10 ** 9)
        self.assertLess(time.monotonic() - start, 0.1)


class QueueIntegrationTests(unittest.TestCase):
    def queue(self, db, url, **extra):
        return enqueue(db, {'title': 'Fixture', 'author': 'A', 'url': url, 'format': '.zip', 'source': url, **extra})

    def test_download_pause_resume_and_cancel_states(self):
        with RangeServer() as server, tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            server.delay = 0.05  # several seconds, past the first checkpoint
            queued = self.queue(db, server.url)
            part = Path(root) / 'downloads' / f".partial-{queued['id']}.part"
            def pause_later():
                # Pause once some bytes are checkpointed, however long start-up takes.
                deadline = time.monotonic() + 30
                while transfer.partial_progress(part)[0] < 1_000_000 and time.monotonic() < deadline:
                    time.sleep(0.01)
                transfer.request_pause(('item', queued['id']))
            threading.Thread(target=pause_later, daemon=True).start()
            with patch('media_intake.core.check_download'), self.assertRaises(transfer.TransferPaused):
                download(db, queued['id'], root)
            row = db.execute('SELECT * FROM items WHERE id=?', (queued['id'],)).fetchone()
            self.assertEqual(row['state'], 'paused')
            self.assertGreater(row['progress_bytes'], 0)
            server.delay = 0
            with patch('media_intake.core.check_download'):
                result = download(db, queued['id'], root)
            self.assertEqual(Path(result['file_path']).read_bytes(), PAYLOAD)
            self.assertEqual(result['sha256'], hashlib.sha256(PAYLOAD).hexdigest())
            self.assertFalse(list((Path(root) / 'downloads').glob('.partial-*')))

            server.delay = 0.05
            second = self.queue(db, server.url + '?2')
            second_part = Path(root) / 'downloads' / f".partial-{second['id']}.part"
            def cancel_later():
                deadline = time.monotonic() + 30
                while transfer.partial_progress(second_part)[0] < 1_000_000 and time.monotonic() < deadline:
                    time.sleep(0.01)
                transfer.request_cancel(('item', second['id']))
            threading.Thread(target=cancel_later, daemon=True).start()
            with self.assertRaises(transfer.TransferCancelled):
                download(db, second['id'], root)
            self.assertEqual(db.execute('SELECT state FROM items WHERE id=?', (second['id'],)).fetchone()[0], 'cancelled')
            self.assertFalse(list((Path(root) / 'downloads').glob(f".partial-{second['id']}*")))

    def test_server_filename_corrects_a_guessed_format(self):
        with RangeServer() as server, tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            queued = self.queue(db, server.url, format='.epub', format_guessed=True)
            result = download(db, queued['id'], root)  # file.bin: '.bin' is a supported package format
            self.assertTrue(result['file_path'].endswith('.bin'))
            metadata = json.loads(db.execute('SELECT metadata FROM items WHERE id=?', (queued['id'],)).fetchone()[0])
            self.assertEqual((metadata['format'], metadata['format_guessed']), ('.bin', False))
            fixed = self.queue(db, server.url + '?fixed', format='.epub')
            with self.assertRaisesRegex(ValueError, 'Invalid .epub'):
                download(db, fixed['id'], root)  # without a guess, the declared type is enforced

    def test_settings_apply_connections_and_speed_limit(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            self.assertEqual(core.download_settings(db), {'connections': 4, 'speed_limit_kbps': 0, 'retries': 4,
                                                          'parallel': 6, 'autostart': 0,
                                                          'seed_ratio_pct': 100, 'seed_minutes': 1440, 'upload_limit_kbps': 0})
            db.executemany('INSERT INTO gui_settings(key,value) VALUES(?,?)',
                           [('download_connections', '40'), ('download_speed_limit_kbps', '512'),
                            ('download_retries', 'x'), ('download_parallel', '0'), ('download_autostart', '5')])
            self.assertEqual(core.download_settings(db), {'connections': 16, 'speed_limit_kbps': 512, 'retries': 4,
                                                          'parallel': 1, 'autostart': 1,
                                                          'seed_ratio_pct': 100, 'seed_minutes': 1440, 'upload_limit_kbps': 0})

    def test_the_same_item_cannot_download_twice_at_once(self):
        with RangeServer() as server, tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            queued = self.queue(db, server.url)
            control = transfer.claim_control(('item', queued['id']))
            self.assertIsNotNone(control)
            self.assertIsNone(transfer.claim_control(('item', queued['id'])))
            try:
                with self.assertRaisesRegex(ValueError, 'already downloading'):
                    download(db, queued['id'], root)
                self.assertEqual(db.execute('SELECT state FROM items WHERE id=?', (queued['id'],)).fetchone()[0], 'queued')
            finally:
                transfer.release_control(('item', queued['id']))

    def test_shortlinks_resolve_automatically_or_wait_for_helper(self):
        from media_intake.file_hosts import HostedFile, ManualDownloadRequired
        with RangeServer() as server, tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            queued = self.queue(db, 'https://ouo.io/abc')
            hosted = [HostedFile(server.url, '.zip', 'direct', 'file.zip', shortlink='https://ouo.io/abc')]
            with patch('media_intake.file_hosts.resolve_hosted_file', return_value=hosted), \
                 patch('media_intake.core.check_download'):
                result = download(db, queued['id'], root)
            self.assertEqual(Path(result['file_path']).read_bytes(), PAYLOAD)
            metadata = json.loads(db.execute('SELECT metadata FROM items WHERE id=?', (queued['id'],)).fetchone()[0])
            self.assertEqual((metadata['resolved_url'], metadata['shortlink_url']), (server.url, 'https://ouo.io/abc'))

            pending = self.queue(db, 'https://ouo.io/needs-click')
            with patch('media_intake.file_hosts.resolve_hosted_file',
                       side_effect=ManualDownloadRequired('Shortlink ouo.io needs page interaction')):
                with self.assertRaises(core.ShortlinkPending):
                    download(db, pending['id'], root)
            self.assertEqual(db.execute('SELECT state FROM items WHERE id=?', (pending['id'],)).fetchone()[0],
                             'needs_shortlink')
            core.resolve_shortlink_item(db, pending['id'], server.url)
            with patch('media_intake.core.check_download'):
                result = download(db, pending['id'], root)
            self.assertEqual(Path(result['file_path']).stat().st_size, len(PAYLOAD))


if __name__ == '__main__':
    unittest.main()
