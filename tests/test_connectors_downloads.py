"""End-to-end downloads for the file-capable connectors, from search result to validated library file.

Everything runs against local stand-ins: fixture websites over HTTP, a fake
SABnzbd API, and for torrents a real libtorrent seeder plus a tiny HTTP tracker on
127.0.0.1. The payloads are deterministic test files, not copies of real works.
"""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from unittest.mock import patch

from connector_site import FakeSite, Reply, fixture
from media_intake import transfer
from media_intake.connectors import save_source_config, search_one, search_sources
from media_intake.connectors import usenet
from media_intake.core import connect, download, enqueue
from media_intake.rights_evidence import ensure_rights_evidence
from media_intake.watched import set_setting


def epub_bytes(title='Pride and Prejudice', padding=600_000):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '<container/>')
        archive.writestr('content.opf', f'<package><metadata><title>{title}</title></metadata></package>')
        # Stored, not deflated, so the file is large enough to interrupt part-way.
        archive.writestr('chapter.xhtml', ('It is a truth universally acknowledged. ' * (padding // 40)),
                         compress_type=zipfile.ZIP_STORED)
    return buffer.getvalue()


def zip_bytes(name='tetromino.nes', size=300_000):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(name, bytes(range(256)) * (size // 256))
    return buffer.getvalue()


PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n' + b'0' * 50_000


class DownloadCase(unittest.TestCase):
    approve = ()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rights = ensure_rights_evidence(self.root / 'rights-evidence.json')
        document = json.loads(self.rights.read_text(encoding='utf-8'))
        for key in self.approve:
            document['sources'][key]['approved_for_download'] = True
        self.rights.write_text(json.dumps(document), encoding='utf-8')
        self.db = connect(self.root)
        self.dpapi = patch('media_intake.kindle._dpapi_crypt', side_effect=RuntimeError('no DPAPI here'))
        self.dpapi.start()

    def tearDown(self):
        self.dpapi.stop()
        self.db.close()
        self.temp.cleanup()

    def options(self, configs):
        return {'rights_evidence_path': str(self.rights), 'source_configs': configs}

    def queue(self, row):
        return enqueue(self.db, dict(row, rights_evidence_path=str(self.rights)))['id']

    def item(self, item_id):
        return dict(self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())


class ZLibraryDownloadTest(DownloadCase):
    approve = ('z-library',)

    def site(self, file_reply, file_api=None):
        site = FakeSite()
        site.route('POST', '/eapi/user/login', Reply(fixture('zlib_login.json'), content_type='application/json'))
        site.route('POST', '/eapi/book/search', Reply(fixture('zlib_search.json'), content_type='application/json'))
        site.route('GET', r'/eapi/book/1342/a1b2c3/file', file_api or (lambda request: Reply(json.dumps({
            'success': 1, 'file': {'extension': 'epub', 'allowDownload': True,
                                   'downloadLink': f'{site.base}/dl/pride.epub'}}), content_type='application/json')))
        site.route('GET', '/dl/pride.epub', file_reply)
        return site

    def search(self, site):
        save_source_config(self.db, 'z-library', {'base': site.base, 'username': 'reader@example.invalid',
                                                  'password': 'pw'})
        from media_intake.connectors import load_all_configs
        found = search_sources('pride and prejudice', 'Books', self.options(load_all_configs(self.db)))
        self.assertEqual(found['errors'], [])
        return next(row for row in found['rows'] if row['format'] == '.epub')

    def test_search_queue_download_and_validate_an_epub(self):
        payload = epub_bytes()
        with self.site(Reply(payload, content_type='application/epub+zip', ranges=True)) as site:
            row = self.search(site)
            item_id = self.queue(row)
            self.assertEqual(self.queue(row), item_id)  # queueing the same result twice keeps one item
            download(self.db, item_id, self.root)
            item = self.item(item_id)
            self.assertEqual(item['state'], 'downloaded')
            self.assertEqual(Path(item['file_path']).read_bytes(), payload)
            self.assertEqual(item['sha256'], hashlib.sha256(payload).hexdigest())
            self.assertEqual(Path(item['file_path']).name, 'Pride and Prejudice - Jane Austen.epub')
            fetched = len(site.hits('/dl/pride.epub'))
            download(self.db, item_id, self.root)  # already downloaded: nothing fetched again
            self.assertEqual(len(site.hits('/dl/pride.epub')), fetched)
            health = self.db.execute("SELECT ok FROM source_events WHERE source_key='z-library' AND kind='download'")
            self.assertEqual({row[0] for row in health}, {1})

    def test_an_interrupted_transfer_resumes_with_a_fresh_link(self):
        payload = epub_bytes(padding=2_000_000)
        attempts = []

        def serve(request):
            if request['headers'].get('Range') == 'bytes=0-0':  # the engine's size/range probe
                return Reply(payload, content_type='application/epub+zip', ranges=True)
            attempts.append(request['headers'].get('Range'))
            if len(attempts) == 1:
                return Reply(payload, content_type='application/epub+zip', ranges=True, drop_after=700_000)
            return Reply(payload, content_type='application/epub+zip', ranges=True)
        set_setting(self.db, 'download_retries', 0)
        set_setting(self.db, 'download_connections', 1)
        with self.site(serve) as site:
            item_id = self.queue(self.search(site))
            with self.assertRaises(Exception):
                download(self.db, item_id, self.root)
            failed = self.item(item_id)
            self.assertEqual(failed['state'], 'failed')
            self.assertEqual(failed['progress_message'], 'Partial download kept; retry resumes')
            download(self.db, item_id, self.root)
            self.assertEqual(len(site.hits(r'/eapi/book/1342/a1b2c3/file')), 2)  # link asked for again
        self.assertTrue(attempts[-1] and attempts[-1].startswith('bytes=') and attempts[-1] != 'bytes=0-')
        self.assertEqual(Path(self.item(item_id)['file_path']).read_bytes(), payload)

    def test_an_html_page_instead_of_the_book_is_rejected(self):
        with self.site(Reply(b'<html><body>Please log in</body></html>' * 50, content_type='text/html')) as site:
            item_id = self.queue(self.search(site))
            with self.assertRaises(Exception):
                download(self.db, item_id, self.root)
        item = self.item(item_id)
        self.assertEqual(item['state'], 'failed')
        self.assertIn('HTML', item['error'])
        self.assertFalse(any(path.suffix == '.epub' for path in (self.root / 'downloads').glob('*')))

    def test_daily_limit_and_unsupported_formats_are_explained(self):
        for api, words in ((Reply(fixture('zlib_limit.json'), content_type='application/json'), 'daily download limit'),
                           (Reply(json.dumps({'success': 1, 'file': {'extension': 'djvu', 'downloadLink': 'x'}}),
                                  content_type='application/json'), 'cannot check or open')):
            with self.subTest(words=words), self.site(Reply(b''), file_api=api) as site:
                item_id = self.queue(self.search(site))
                with self.assertRaises(Exception):
                    download(self.db, item_id, self.root)
                item = self.item(item_id)
                self.assertEqual(item['state'], 'failed')
                self.assertIn(words, item['error'])
                self.assertTrue(item['error'].startswith('Z-Library: '))
                self.db.execute('DELETE FROM items')
                self.db.commit()

    def test_no_approval_means_no_download(self):
        document = json.loads(self.rights.read_text(encoding='utf-8'))
        document['sources']['z-library']['approved_for_download'] = False
        with self.site(Reply(epub_bytes(), content_type='application/epub+zip')) as site:
            # Searching skips an unapproved source, so queue a previously found row instead.
            stored = {'title': 'Pride and Prejudice', 'author': 'Jane Austen', 'format': '.epub',
                      'url': f'{site.base}/book/1342', 'source': 'Z-Library', 'source_key': 'z-library',
                      'resolve_connector': 'z-library', 'source_id': '1342', 'zlib_hash': 'a1b2c3'}
            self.rights.write_text(json.dumps(document), encoding='utf-8')
            item_id = self.queue(stored)
            with self.assertRaises(ValueError) as caught:
                download(self.db, item_id, self.root)
            self.assertEqual(site.requests, [])
        self.assertIn('Download not approved', str(caught.exception))


class VimmAndMagzDownloadTest(DownloadCase):
    approve = ('vimm', 'magzdb')

    def test_vimm_download_form_sends_the_referer_and_saves_the_archive(self):
        payload = zip_bytes()

        def serve(request):
            if not request['headers'].get('Referer', '').endswith('/vault/70001'):
                return Reply(b'Forbidden', 403)
            self.assertEqual(request['query']['mediaId'], ['12345'])
            return Reply(payload, content_type='application/zip', ranges=True,
                         headers={'Content-Disposition': 'attachment; filename="Homebrew Tetromino (USA).zip"'})
        with FakeSite() as site:
            site.route('GET', '/vault/', Reply(fixture('vimm_list.html')))
            site.route('GET', '/vault/70001', Reply(
                f'<html><h2 class="mainContent">Homebrew Tetromino</h2><form id="dl_form" action="{site.base}/dl/" '
                'method="POST"><input type="hidden" name="mediaId" value="12345"></form>'
                '<table><tr><td id="dl_size">293 KB</td></tr></table></html>'))
            site.route('GET', '/vault/70002', Reply(fixture('vimm_unavailable.html')))
            site.route('GET', '/dl/', serve)
            save_source_config(self.db, 'vimm', {'base': site.base})
            page = search_one('vimm', 'tetromino', 'Games', 1, {'vimm': {'base': site.base}})
            item_id = self.queue(page.rows[0])
            download(self.db, item_id, self.root)
            unavailable = self.queue(page.rows[1])
            with self.assertRaises(Exception):
                download(self.db, unavailable, self.root)
        item = self.item(item_id)
        self.assertEqual(item['state'], 'downloaded')
        self.assertEqual(Path(item['file_path']).read_bytes(), payload)
        self.assertTrue(zipfile.is_zipfile(item['file_path']))
        self.assertIn('downloads of this title are disabled', self.item(unavailable)['error'])

    def test_magzdb_issue_page_leads_to_a_validated_pdf(self):
        with FakeSite() as site:
            site.route('GET', '/makelist', Reply(fixture('magzdb_search.html')))
            site.route('GET', '/j/501', Reply(fixture('magzdb_journal.html')))
            site.route('GET', '/num/90001', Reply('<html><a href="/file/90001/psm-1872-01.pdf">Download</a></html>'))
            site.route('GET', '/file/90001/psm-1872-01.pdf', Reply(PDF, content_type='application/pdf', ranges=True))
            site.route('GET', '/num/90002', Reply('<html><p>No file for this issue.</p></html>'))
            save_source_config(self.db, 'magzdb', {'base': site.base})
            page = search_one('magzdb', 'popular science', 'Books', 1, {'magzdb': {'base': site.base}})
            item_id = self.queue(page.rows[0])
            download(self.db, item_id, self.root)
            missing = self.queue(page.rows[1])
            with self.assertRaises(Exception):
                download(self.db, missing, self.root)
        self.assertEqual(Path(self.item(item_id)['file_path']).read_bytes(), PDF)
        self.assertIn('no downloadable file', self.item(missing)['error'])


class FakeSabnzbd:
    """Just enough of SABnzbd's API: addfile, queue, history, pause/resume/delete."""

    def __init__(self, site, finish_after=2, fail=False):
        self.site, self.finish_after, self.fail = site, finish_after, fail
        self.jobs, self.calls, self.paused = {}, [], set()
        site.route('*', '/api', self.handle)

    def handle(self, request):
        query = {key: values[0] for key, values in request['query'].items()}
        if query.get('apikey') != 'SABKEY':
            return Reply(json.dumps({'status': False, 'error': 'API Key Incorrect'}), content_type='application/json')
        mode, name = query.get('mode'), query.get('name')
        self.calls.append((mode, name, query.get('value')))
        if mode == 'version':
            return self.json({'version': '4.3.0'})
        if mode == 'addfile':
            assert b'<nzb' in request['body']
            job = f'SABnzbd_nzo_{len(self.jobs) + 1}'
            self.jobs[job] = 0
            return self.json({'status': True, 'nzo_ids': [job]})
        if mode == 'queue' and name in ('pause', 'resume'):
            (self.paused.add if name == 'pause' else self.paused.discard)(query['value'])
            return self.json({'status': True})
        if mode == 'queue' and name == 'delete':
            self.jobs.pop(query['value'], None)
            return self.json({'status': True})
        if mode == 'queue':
            slots = []
            for job, polls in list(self.jobs.items()):
                if polls < self.finish_after or job in self.paused:
                    if job not in self.paused:
                        self.jobs[job] = polls + 1
                    slots.append({'nzo_id': job, 'status': 'Paused' if job in self.paused else 'Downloading',
                                  'mb': '100.0', 'mbleft': str(100 - 40 * polls), 'timeleft': '0:00:05'})
            return self.json({'queue': {'slots': slots}})
        if mode == 'history':
            slots = [{'nzo_id': job, 'status': 'Failed' if self.fail else 'Completed', 'bytes': 104857600,
                      'storage': '/srv/usenet/complete/Night of the Living Dead',
                      'fail_message': 'Repair failed, not enough repair blocks (2 short)'}
                     for job, polls in self.jobs.items() if polls >= self.finish_after]
            return self.json({'history': {'slots': slots}})
        return self.json({'status': False, 'error': 'unknown mode'})

    @staticmethod
    def json(value):
        return Reply(json.dumps(value), content_type='application/json')


class UsenetDownloadTest(DownloadCase):
    approve = ('nzbgeek',)

    def setUp(self):
        super().setUp()
        self.poll = patch.object(usenet, 'POLL_SECONDS', 0.01)
        self.poll.start()

    def tearDown(self):
        self.poll.stop()
        super().tearDown()

    def row(self, indexer, nzb_reply=None):
        search = fixture('newznab_search.xml').replace(b'https://indexer.example', indexer.base.encode())
        indexer.route('GET', '/api', Reply(search, content_type='application/rss+xml'))
        indexer.route('GET', r'/getnzb/.*', nzb_reply or Reply(fixture('sample.nzb'), content_type='application/x-nzb'))
        page = search_one('nzbgeek', 'night of the living dead', 'Videos', 1,
                          {'nzbgeek': {'base': indexer.base, 'api_key': 'KEY'}})
        return page.rows[0]

    def client(self, sab, **extra):
        save_source_config(self.db, 'usenet_client', dict(client='sabnzbd', url=sab.base, api_key='SABKEY', **extra))

    def test_nzb_goes_to_sabnzbd_and_the_queue_follows_it_to_completion(self):
        with FakeSite() as indexer, FakeSite() as sab_site:
            sab = FakeSabnzbd(sab_site)
            self.client(sab_site)
            item_id = self.queue(self.row(indexer))
            messages = []
            with patch('media_intake.core.update_download_progress',
                       side_effect=lambda db, item, done, total, message: messages.append((done, total, message))):
                download(self.db, item_id, self.root)
        item = self.item(item_id)
        self.assertEqual(item['state'], 'downloaded')
        self.assertEqual(item['file_path'], str(Path('/srv/usenet/complete/Night of the Living Dead')))
        self.assertIn(('addfile', None, None), sab.calls)
        self.assertTrue(any('SABnzbd: Downloading' in str(message) for _done, _total, message in messages))
        self.assertTrue(any(done and total and done < total for done, total, _message in messages))

    def test_pause_and_cancel_reach_the_newsreader(self):
        with FakeSite() as indexer, FakeSite() as sab_site:
            sab = FakeSabnzbd(sab_site, finish_after=1000)
            self.client(sab_site)
            item_id = self.queue(self.row(indexer))
            worker = self._background(item_id)
            self._wait(lambda: sab.jobs)
            transfer.request_pause(('item', item_id))
            worker.join(10)
            self.assertEqual(self._state(item_id), 'paused')
            job = next(iter(sab.jobs))
            self.assertIn(job, sab.paused)
            sab.finish_after = 0  # the job completes once resumed
            download(self.db, item_id, self.root)
            self.assertEqual(self._state(item_id), 'downloaded')
            self.assertIn(('queue', 'resume', job), sab.calls)
            self.assertEqual(sab.calls.count(('addfile', None, None)), 1)  # resumed, not sent twice

            second = self.queue(dict(self.row(indexer), url=self.row(indexer)['url'] + '&copy=2'))
            sab.finish_after = 1000
            worker = self._background(second)
            self._wait(lambda: len(sab.calls) > 0 and sab.calls.count(('addfile', None, None)) == 2)
            transfer.request_cancel(('item', second))
            worker.join(10)
            self.assertEqual(self._state(second), 'cancelled')
            self.assertTrue(any(call[:2] == ('queue', 'delete') for call in sab.calls))

    def test_failed_jobs_limits_and_missing_newsreader_are_actionable(self):
        with FakeSite() as indexer, FakeSite() as sab_site:
            FakeSabnzbd(sab_site, finish_after=0, fail=True)
            self.client(sab_site)
            item_id = self.queue(self.row(indexer))
            with self.assertRaises(Exception):
                download(self.db, item_id, self.root)
            self.assertIn('Repair failed', self.item(item_id)['error'])
            self.assertIn('choose another release', self.item(item_id)['error'])

        with FakeSite() as indexer, FakeSite() as sab_site:
            FakeSabnzbd(sab_site)
            self.client(sab_site)
            limited = self.queue(self.row(indexer, Reply(fixture('newznab_limit.xml'), content_type='text/xml')))
            with self.assertRaises(Exception):
                download(self.db, limited, self.root)
            self.assertIn('limit', self.item(limited)['error'])

        save_source_config(self.db, 'usenet_client', {'url': '', 'api_key': '', 'nzb_mode': 'fail'})
        with FakeSite() as indexer:
            nothing = self.queue(dict(self.row(indexer), url=self.row(indexer)['url'] + '&n=3'))
            with self.assertRaises(Exception):
                download(self.db, nothing, self.root)
        self.assertIn('need SABnzbd or NZBGet', self.item(nothing)['error'])

    def test_without_a_newsreader_the_nzb_can_be_saved_for_later(self):
        save_source_config(self.db, 'usenet_client', {'url': '', 'nzb_mode': 'save'})
        with FakeSite() as indexer:
            item_id = self.queue(self.row(indexer))
            download(self.db, item_id, self.root)
        item = self.item(item_id)
        self.assertEqual(item['state'], 'downloaded')
        self.assertTrue(item['file_path'].endswith('.nzb'))
        self.assertEqual(Path(item['file_path']).read_bytes(), fixture('sample.nzb'))
        self.assertEqual(item['progress_message'], 'NZB saved: open it in your newsreader')

    def _background(self, item_id):
        """Download in a worker thread with its own connection, as the GUI does."""
        def run():
            db = connect(self.root)
            try:
                download(db, item_id, self.root)
            except Exception:
                pass  # pause and cancel end the download with an exception by design
            finally:
                db.close()
        worker = threading.Thread(target=run)
        worker.start()
        return worker

    def _state(self, item_id):
        return self.db.execute('SELECT state FROM items WHERE id=?', (item_id,)).fetchone()[0]

    @staticmethod
    def _wait(condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError('condition not reached')


def bencode(value):
    if isinstance(value, int):
        return b'i%de' % value
    if isinstance(value, bytes):
        return b'%d:%s' % (len(value), value)
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, dict):
        return b'd' + b''.join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b'e'
    raise TypeError(value)


try:
    import libtorrent as lt
except ImportError:  # optional dependency (requirements-torrent.txt)
    lt = None


@unittest.skipIf(lt is None, 'libtorrent is not installed')
class TorrentLoopbackDownloadTest(DownloadCase):
    """A real BitTorrent transfer: local seeder, local tracker, the app's own torrent engine."""
    approve = ('thepiratebay', '1337x')

    def setUp(self):
        super().setUp()
        from media_intake import torrents
        torrents.reset_session()
        set_setting(self.db, 'download_seed_ratio_pct', 0)
        set_setting(self.db, 'download_seed_minutes', 0)
        self.seed_dir = self.root / 'seed'
        self.seed_dir.mkdir()
        self.payload = hashlib.sha256(b'fixture').digest() * (3 * 1024 * 1024 // 32)
        name = 'Night.of.the.Living.Dead.1968.SAMPLE.mkv'
        (self.seed_dir / name).write_bytes(self.payload)
        files = lt.file_storage()
        lt.add_files(files, str(self.seed_dir / name))
        creator = lt.create_torrent(files, 256 * 1024)
        lt.set_piece_hashes(creator, str(self.seed_dir))
        self.torrent = lt.bencode(creator.generate())
        info = lt.torrent_info(lt.bdecode(self.torrent))
        self.info_hash = str(info.info_hash())
        self.name = name
        self.seeder = lt.session({'listen_interfaces': '127.0.0.1:0', 'enable_dht': False, 'enable_lsd': False,
                                  'enable_upnp': False, 'enable_natpmp': False})
        params = lt.add_torrent_params()
        params.ti = info
        params.save_path = str(self.seed_dir)
        params.flags |= lt.torrent_flags.seed_mode
        self.seed_handle = self.seeder.add_torrent(params)
        self.tracker = FakeSite()
        port = self.seeder.listen_port()
        peers = bytes([127, 0, 0, 1]) + port.to_bytes(2, 'big')
        self.tracker.route('GET', '/announce', Reply(bencode({'interval': 30, 'peers': peers}),
                                                     content_type='text/plain'))
        self.tracker.__enter__()
        self.trackers = patch('media_intake.connectors.releases.PUBLIC_TRACKERS', (self.tracker.base + '/announce',))
        self.trackers.start()

    def tearDown(self):
        from media_intake import torrents
        self.trackers.stop()
        self.tracker.__exit__()
        torrents.reset_session()
        transfer.LIMITER.set_rate(0)
        super().tearDown()

    def tpb_site(self):
        site = FakeSite()
        site.route('GET', '/q.php', Reply(json.dumps([{
            'id': '1', 'name': self.name, 'info_hash': self.info_hash.upper(), 'seeders': '1', 'leechers': '0',
            'size': str(len(self.payload)), 'num_files': '1', 'username': 'test', 'added': '1700000000',
            'status': 'member', 'category': '201', 'imdb': ''}]), content_type='application/json'))
        return site

    def test_search_queue_and_download_a_torrent_through_the_app_engine(self):
        with self.tpb_site() as site:
            page = search_one('thepiratebay', 'night of the living dead', 'Videos', 1,
                              {'thepiratebay': {'base': site.base}})
        row = page.rows[0]
        self.assertIn(urllib.parse.quote(self.tracker.base, safe=''), row['url'])
        item_id = self.queue(row)
        download(self.db, item_id, self.root)
        item = self.item(item_id)
        self.assertEqual(item['state'], 'downloaded')
        self.assertEqual(Path(item['file_path']).read_bytes(), self.payload)
        self.assertEqual(item['sha256'], hashlib.sha256(self.payload).hexdigest())
        self.assertTrue(self.tracker.hits('/announce'))

    def test_1337x_magnet_is_read_from_the_release_page_when_the_download_starts(self):
        from media_intake.connectors.releases import magnet_link
        with FakeSite() as site:
            site.route('GET', r'/(category-)?search/.*', Reply(fixture('1337x_search.html').replace(
                b'/torrent/5550001/', b'/torrent/7/')))
            site.route('GET', r'/torrent/7/.*', Reply(
                f'<html><a href="{magnet_link(self.info_hash, self.name)}">Magnet Download</a></html>'))
            save_source_config(self.db, '1337x', {'base': site.base})
            page = search_one('1337x', 'night of the living dead', 'Videos', 1, {'1337x': {'base': site.base}})
            row = page.rows[0]
            self.assertFalse(row['url'].startswith('magnet:'))
            item_id = self.queue(row)
            download(self.db, item_id, self.root)
        self.assertEqual(Path(self.item(item_id)['file_path']).read_bytes(), self.payload)

    def test_a_paused_torrent_resumes_from_saved_progress(self):
        # libtorrent exempts local peers from rate limits unless told otherwise; slow the seeder down
        # so the transfer can be paused part-way.
        self.seeder.apply_settings({'upload_rate_limit': 300 * 1024, 'ignore_limits_on_local_network': False})
        with self.tpb_site() as site:
            row = search_one('thepiratebay', 'night', 'Videos', 1, {'thepiratebay': {'base': site.base}}).rows[0]
        item_id = self.queue(row)
        errors = []

        def run():
            db = connect(self.root)
            try:
                download(db, item_id, self.root)
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()
        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = self.item(item_id)
            if (row['progress_bytes'] or 0) > 256 * 1024:
                break
            time.sleep(0.1)
        transfer.request_pause(('item', item_id))
        worker.join(30)
        paused = self.item(item_id)
        self.assertEqual(paused['state'], 'paused')
        self.assertTrue(any(isinstance(error, transfer.TransferPaused) for error in errors))
        self.assertTrue((self.root / 'downloads' / f'.partial-{item_id}.fastresume').exists())
        self.seeder.apply_settings({'upload_rate_limit': 0})
        download(self.db, item_id, self.root)
        self.assertEqual(Path(self.item(item_id)['file_path']).read_bytes(), self.payload)


if __name__ == '__main__':
    unittest.main()
