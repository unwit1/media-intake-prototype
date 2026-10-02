"""Native torrent downloads through a fake libtorrent, plus queue integration."""
from contextlib import closing
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from media_intake import torrents, transfer
from media_intake.core import connect, download, enqueue, validate_item

MAGNET = 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Fixture'


class FakeStatus:
    def __init__(self, handle):
        self.handle = handle
        self.state = types.SimpleNamespace(name=handle.state)
        self.total_wanted_done = handle.done
        self.total_wanted = handle.total
        self.download_rate = 2_000_000 if handle.state == 'downloading' else 0
        self.num_peers = 7
        self.name = 'Fixture Pack'
        self.is_finished = handle.state == 'finished'
        self.errc = None


class FakeHandle:
    def __init__(self, session, params, steps):
        self.session, self.params, self.total = session, params, 3_000_000
        self.done = params.resume_done
        self.state = 'downloading_metadata'
        self.steps = steps
        self.paused = False

    def status(self):
        if not self.paused:
            if self.state == 'downloading_metadata':
                self.state = 'downloading'
            elif self.state == 'downloading':
                self.done = min(self.total, self.done + self.steps)
                if self.done >= self.total:
                    self.state = 'finished'
                    folder = Path(self.params.save_path) / 'Fixture Pack'
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / 'a.bin').write_bytes(b'x' * 10)
        return FakeStatus(self)

    def pause(self):
        self.paused = True

    def save_resume_data(self, flags=0):
        self.session.alerts.append(type('save_resume_data_alert', (), {'params': self.done})())

    def torrent_file(self):
        return types.SimpleNamespace(name=lambda: 'Fixture Pack')


class FakeParams:
    def __init__(self, source):
        self.source, self.save_path, self.ti, self.resume_done = source, None, None, 0


def fake_lt(steps=1_000_000):
    lt = types.SimpleNamespace()
    lt.removed, lt.sessions = [], []

    class Session:
        def __init__(self, settings):
            self.settings, self.alerts, self.applied = settings, [], []
            lt.sessions.append(self)

        def apply_settings(self, values):
            self.applied.append(values)

        def add_torrent(self, params):
            self.handle = FakeHandle(self, params, steps)
            return self.handle

        def pop_alerts(self):
            alerts, self.alerts = self.alerts, []
            return alerts

        def remove_torrent(self, handle, flags=0):
            lt.removed.append(flags)

    lt.session = Session
    lt.alert = types.SimpleNamespace(category_t=types.SimpleNamespace(
        status_notification=1, storage_notification=2, error_notification=4))
    lt.options_t = types.SimpleNamespace(delete_files=99)
    lt.save_resume_flags_t = types.SimpleNamespace(save_info_dict=1)
    lt.parse_magnet_uri = lambda uri: FakeParams(uri)
    lt.add_torrent_params = lambda: FakeParams('file')
    lt.bdecode = lambda data: data
    lt.torrent_info = lambda decoded: ('info', decoded)
    lt.write_resume_data_buf = lambda done: str(done).encode()

    def read_resume(data):
        params = FakeParams('resume')
        params.resume_done = int(data)
        return params
    lt.read_resume_data = read_resume
    return lt


class TorrentEngineTests(unittest.TestCase):
    def setUp(self):
        torrents.reset_session()
        self.addCleanup(torrents.reset_session)

    def test_magnet_downloads_with_progress_and_rate_limit(self):
        lt = fake_lt()
        reports = []
        transfer.LIMITER.set_rate(2048)
        self.addCleanup(transfer.LIMITER.set_rate, 0)
        with tempfile.TemporaryDirectory() as folder:
            path, total = torrents.download_torrent(
                {'url': MAGNET}, folder, 5, control=transfer.Control(), poll=0,
                progress=lambda *args: reports.append(args), fetch=None, lt=lt)
            self.assertEqual((path, total), (Path(folder) / 'Fixture Pack', 3_000_000))
            self.assertTrue((path / 'a.bin').exists())
        self.assertEqual(lt.sessions[0].handle.params.source, MAGNET)
        self.assertIn({'download_rate_limit': 2048}, lt.sessions[0].applied)
        self.assertTrue(any('7 peers' in message and 'MB/s' in message for _d, _t, message in reports))
        self.assertEqual(lt.removed, [0])  # stopped after completion, files kept

    def test_pause_saves_fast_resume_and_restart_continues(self):
        lt = fake_lt(steps=500_000)
        with tempfile.TemporaryDirectory() as folder:
            control = transfer.Control()
            def progress(done, total, message):
                if done >= 1_000_000:
                    control.pause.set()
            with self.assertRaises(transfer.TransferPaused):
                torrents.download_torrent({'url': MAGNET}, folder, 9, control=control, poll=0,
                                          progress=progress, fetch=None, lt=lt)
            self.assertTrue(torrents.torrent_partial_exists(folder, 9))
            torrents.reset_session()
            reports = []
            torrents.download_torrent({'url': MAGNET}, folder, 9, control=transfer.Control(), poll=0,
                                      progress=lambda *a: reports.append(a), fetch=None, lt=lt)
            self.assertEqual(lt.sessions[-1].handle.params.source, 'resume')
            self.assertGreaterEqual(reports[0][0], 1_000_000)
            self.assertTrue(any('resumed' in r[2] for r in reports))
            self.assertFalse(torrents.torrent_partial_exists(folder, 9))

    def test_cancel_deletes_and_torrent_file_links_are_fetched(self):
        lt = fake_lt(steps=100_000)
        with tempfile.TemporaryDirectory() as folder:
            control = transfer.Control()
            with self.assertRaises(transfer.TransferCancelled):
                torrents.download_torrent({'url': MAGNET}, folder, 3, control=control, poll=0,
                                          progress=lambda done, *_: done and control.cancel.set(), fetch=None, lt=lt)
            self.assertEqual(lt.removed[-1], 99)
            fetched = []
            torrents.reset_session()
            torrents.download_torrent({'url': 'http://127.0.0.1:9117/dl/x/?jackett_apikey=k&file=f', 'format': '.torrent'},
                                      folder, 4, control=transfer.Control(), poll=0, progress=lambda *a: None,
                                      fetch=lambda url: fetched.append(url) or b'd4:infod4:name1:xee', lt=lt)
            self.assertEqual(lt.sessions[-1].handle.params.ti, ('info', b'd4:infod4:name1:xee'))
            with self.assertRaisesRegex(transfer.TransferError, 'not return a .torrent'):
                torrents.download_torrent({'url': 'https://x.example/a.torrent'}, folder, 6, control=transfer.Control(),
                                          poll=0, progress=lambda *a: None, fetch=lambda url: b'<html>', lt=lt)

    def test_missing_libtorrent_explains_options(self):
        with patch('media_intake.torrents.load_libtorrent', return_value=None):
            with self.assertRaisesRegex(transfer.TransferError, 'requirements-torrent.txt'):
                torrents.download_torrent({'url': MAGNET}, tempfile.gettempdir(), 1, control=transfer.Control(),
                                          progress=None, fetch=None)


class TorrentQueueTests(unittest.TestCase):
    def setUp(self):
        torrents.reset_session()
        self.addCleanup(torrents.reset_session)

    def test_magnets_validate_and_download_through_the_queue(self):
        item = validate_item({'title': 'Fixture', 'url': MAGNET, 'format': 'torrent', 'source': 'Jackett'})
        self.assertEqual(item['format'], '.torrent')
        with self.assertRaises(ValueError):
            validate_item({'title': 'x', 'url': 'magnet:?dn=no-hash', 'format': '.torrent', 'source': 's'})
        lt = fake_lt()
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db, \
                patch('media_intake.torrents.load_libtorrent', return_value=lt), \
                patch('media_intake.torrents.time.sleep'):
            queued = enqueue(db, {'title': 'Fixture', 'url': MAGNET, 'format': 'torrent', 'source': 'Jackett'})
            result = download(db, queued['id'], root)
            self.assertEqual(result['state'], 'downloaded')
            self.assertEqual(Path(result['file_path']).name, 'Fixture Pack')
            self.assertIsNone(result['sha256'])


if __name__ == '__main__':
    unittest.main()
