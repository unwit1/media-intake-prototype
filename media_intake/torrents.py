"""Native BitTorrent downloads for queued magnet and .torrent items (optional).

Uses libtorrent when installed (``pip install -r requirements-torrent.txt``);
without it, torrent items explain how to enable it or send them to Gopeed.
Behaviour matches the HTTP engine: progress with speed/peers/ETA, pause keeps
fast-resume data beside the download, cancel deletes partial data and the global
speed limit applies. Completed torrents keep seeding (Seeder) until a share ratio
or time limit is reached, under an upload limit; seeding resumes after a restart.
"""
import json
from pathlib import Path
import threading
import time
import urllib.parse

from .transfer import LIMITER, TransferCancelled, TransferError, TransferPaused, format_bytes, format_eta

MAX_TORRENT_FILE_BYTES = 10_000_000
STALL_SECONDS = 15 * 60
_SESSION = None
_SESSION_LOCK = threading.Lock()
UPLOAD_LIMIT = [0]  # bytes/second; 0 = unlimited


def load_libtorrent():
    try:
        import libtorrent
    except ImportError:
        return None
    return libtorrent


def available():
    return load_libtorrent() is not None


def is_torrent_item(item):
    url = str(item.get('resolved_url') or item.get('url') or '')
    parts = urllib.parse.urlsplit(url)
    return (parts.scheme == 'magnet' or str(item.get('format') or '').casefold() in ('.torrent', 'torrent')
            or parts.path.casefold().endswith('.torrent'))


def _session(lt):
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            mask = 0
            for name in ('status_notification', 'storage_notification', 'error_notification'):
                mask |= int(getattr(lt.alert.category_t, name, 0))
            _SESSION = lt.session({'listen_interfaces': '0.0.0.0:6881,[::]:6881', 'enable_dht': True,
                                   'enable_lsd': True, 'enable_upnp': True, 'enable_natpmp': True,
                                   'alert_mask': mask, 'user_agent': 'MediaIntake/0.3'})
        _SESSION.apply_settings({'download_rate_limit': int(LIMITER.rate or 0),
                                 'upload_rate_limit': int(UPLOAD_LIMIT[0] or 0)})
        return _SESSION


def set_upload_limit(bytes_per_second):
    UPLOAD_LIMIT[0] = max(0, int(bytes_per_second or 0))
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.apply_settings({'upload_rate_limit': UPLOAD_LIMIT[0]})


def reset_session():
    global _SESSION
    SEEDER.clear()
    with _SESSION_LOCK:
        _SESSION = None


def _resume_path(folder, item_id):
    return Path(folder) / f'.partial-{item_id}.fastresume'


def _params(lt, item, folder, item_id, fetch):
    resume = _resume_path(folder, item_id)
    if resume.exists():
        try:
            params = lt.read_resume_data(resume.read_bytes())
            params.save_path = str(folder)
            return params, True
        except Exception:
            resume.unlink(missing_ok=True)
    url = str(item.get('resolved_url') or item['url'])
    if url.startswith('magnet:'):
        params = lt.parse_magnet_uri(url)
    else:
        data = fetch(url)
        if not data or len(data) > MAX_TORRENT_FILE_BYTES or not data.startswith(b'd'):
            raise TransferError('The link did not return a .torrent file')
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(lt.bdecode(data))
    params.save_path = str(folder)
    return params, False


def _save_resume(lt, session, handle, path, timeout=10):
    """Ask libtorrent for fast-resume data and write it atomically."""
    flags = getattr(getattr(lt, 'save_resume_flags_t', None), 'save_info_dict', 0)
    handle.save_resume_data(flags)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for alert in session.pop_alerts():
            if type(alert).__name__ == 'save_resume_data_alert':
                data = lt.write_resume_data_buf(alert.params)
                temporary = Path(str(path) + '.tmp')
                temporary.write_bytes(bytes(data))
                temporary.replace(path)
                return True
            if type(alert).__name__ == 'save_resume_data_failed_alert':
                return False
        time.sleep(0.1)
    return False


def _remove(lt, session, handle, delete_files=False):
    flag = 0
    if delete_files:
        options = getattr(lt, 'options_t', None) or getattr(lt, 'session', None)
        flag = getattr(options, 'delete_files', 0)
    session.remove_torrent(handle, flag)


STATE_LABELS = {'checking_files': 'Checking files', 'downloading_metadata': 'Finding peers and metadata',
                'downloading': 'Downloading', 'finished': 'Finished', 'seeding': 'Finished',
                'checking_resume_data': 'Checking saved progress', 'allocating': 'Allocating'}


def download_torrent(item, folder, item_id, *, control, progress, fetch, lt=None, poll=0.5,
                     stall_seconds=STALL_SECONDS, seed=None):
    """Download a magnet or .torrent into `folder`. Returns (path, total_bytes).

    With seed={'ratio': float, 'minutes': int} (either above zero), the completed
    torrent stays in the session and SEEDER takes it over."""
    lt = lt or load_libtorrent()
    if lt is None:
        raise TransferError('Torrent downloads need libtorrent: run "pip install -r requirements-torrent.txt", '
                            'or use Send to download manager (Gopeed)')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    session = _session(lt)
    params, resumed = _params(lt, item, folder, item_id, fetch)
    handle = session.add_torrent(params)
    resume = _resume_path(folder, item_id)
    last_done, last_change = -1, time.monotonic()
    try:
        while True:
            try:
                control.check()
            except TransferPaused:
                handle.pause()
                _save_resume(lt, session, handle, resume)
                _remove(lt, session, handle)
                raise
            except TransferCancelled:
                _remove(lt, session, handle, delete_files=True)
                resume.unlink(missing_ok=True)
                raise
            session.apply_settings({'download_rate_limit': int(LIMITER.rate or 0)})
            status = handle.status()
            state = str(getattr(status.state, 'name', status.state)).rsplit('.', 1)[-1]
            if getattr(status, 'errc', None) and getattr(status.errc, 'value', lambda: 0)():
                message = status.errc.message()
                _save_resume(lt, session, handle, resume)
                _remove(lt, session, handle)
                raise TransferError(f'Torrent error: {message}')
            done, total = int(status.total_wanted_done), int(status.total_wanted)
            if state in ('finished', 'seeding') or (getattr(status, 'is_finished', False) and total):
                name = status.name or handle.torrent_file().name()
                resume.unlink(missing_ok=True)
                if seed and (seed.get('ratio') or seed.get('minutes')):
                    _save_resume(lt, session, handle, _seed_resume_path(folder, item_id))
                    SEEDER.add(item_id, lt, session, handle, folder, seed.get('ratio') or 0,
                               seed.get('minutes') or 0)
                else:
                    _remove(lt, session, handle)
                return folder / name, total
            if done != last_done:
                last_done, last_change = done, time.monotonic()
            elif time.monotonic() - last_change > stall_seconds:
                _save_resume(lt, session, handle, resume)
                _remove(lt, session, handle)
                raise TransferError(f'No torrent progress for {stall_seconds // 60} minutes '
                                    f'({status.num_peers} peers); retry resumes from saved progress')
            parts = [STATE_LABELS.get(state, state.replace('_', ' ').capitalize())]
            rate = int(status.download_rate or 0)
            if rate:
                parts.append(f'{format_bytes(rate)}/s')
                if total:
                    parts.append(f'{format_eta((total - done) / rate)} left')
            parts.append(f'{status.num_peers} peers')
            if resumed:
                parts.append('resumed')
            progress(done, total or None, ' · '.join(parts))
            time.sleep(poll)
    except (TransferPaused, TransferCancelled, TransferError):
        raise
    except Exception as exc:
        try:
            _save_resume(lt, session, handle, resume)
            _remove(lt, session, handle)
        except Exception:
            pass
        raise TransferError(f'Torrent download failed: {exc}') from exc


def discard_torrent_partial(folder, item_id):
    _resume_path(folder, item_id).unlink(missing_ok=True)


def torrent_partial_exists(folder, item_id):
    return _resume_path(folder, item_id).exists()


# ---------------------------------------------------------------------------
# Seeding.

def _seed_resume_path(folder, item_id):
    return Path(folder) / f'.seed-{item_id}.fastresume'


class Seeder:
    """Seeds completed torrents until their ratio or time limit, on one background thread.

    Status lines go to the queue item's progress message through a connection the
    seeding thread opens itself (db_factory), never the caller's connection.
    """

    def __init__(self, interval=2.0):
        self.interval = interval
        self.run_thread = True  # tests drive tick() directly
        self.lock = threading.Lock()
        self.entries = {}
        self.thread = None
        self.db_factory = None

    def configure(self, db_factory):
        self.db_factory = db_factory

    def is_seeding(self, item_id):
        with self.lock:
            return item_id in self.entries

    def seeding_ids(self):
        with self.lock:
            return sorted(self.entries)

    def add(self, item_id, lt, session, handle, folder, ratio, minutes, started=None):
        entry = {'lt': lt, 'session': session, 'handle': handle, 'folder': str(folder),
                 'ratio': float(ratio or 0), 'minutes': int(minutes or 0),
                 'started': float(started or time.time())}
        self._write(item_id, seeding={'ratio': entry['ratio'], 'minutes': entry['minutes'],
                                      'started': entry['started'], 'folder': entry['folder']},
                    message='Seeding')
        with self.lock:
            self.entries[item_id] = entry
            # Create and start under the lock so concurrent completions share one thread.
            if self.run_thread and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._run, name='torrent-seeder', daemon=True)
                self.thread.start()

    def stop(self, item_id, message='Seeding stopped'):
        with self.lock:
            entry = self.entries.pop(item_id, None)
        if entry is None:
            return False
        try:
            _remove(entry['lt'], entry['session'], entry['handle'])
        except Exception:
            pass
        _seed_resume_path(entry['folder'], item_id).unlink(missing_ok=True)
        self._write(item_id, seeding=None, message=message)
        return True

    def clear(self):
        with self.lock:
            self.entries.clear()

    def _write(self, item_id, seeding=..., message=None):
        if self.db_factory is None:
            return
        db = self.db_factory()
        try:
            if seeding is not ...:
                row = db.execute('SELECT metadata FROM items WHERE id=?', (item_id,)).fetchone()
                if row:
                    metadata = json.loads(row['metadata'] or '{}')
                    if seeding is None:
                        metadata.pop('seeding', None)
                    else:
                        metadata['seeding'] = seeding
                    db.execute('UPDATE items SET metadata=? WHERE id=?', (json.dumps(metadata), item_id))
            db.execute('UPDATE items SET progress_message=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (message, item_id))
            db.commit()
        finally:
            db.close()

    def tick(self):
        with self.lock:
            entries = list(self.entries.items())
        for item_id, entry in entries:
            try:
                status = entry['handle'].status()
            except Exception as exc:
                self.stop(item_id, f'Seeding stopped: {exc}')
                continue
            size = max(1, int(getattr(status, 'total_wanted', 0) or 0))
            ratio = int(getattr(status, 'all_time_upload', 0) or 0) / size
            elapsed = time.time() - entry['started']
            limit = entry['minutes'] * 60
            if (entry['ratio'] and ratio >= entry['ratio']) or (limit and elapsed >= limit):
                self.stop(item_id, f'Seeding complete · ratio {ratio:.2f}')
                continue
            parts = ['Seeding', f'ratio {ratio:.2f}' + (f"/{entry['ratio']:.2f}" if entry['ratio'] else '')]
            parts.append(f"{int(getattr(status, 'num_peers', 0) or 0)} peers")
            upload = int(getattr(status, 'upload_rate', 0) or 0)
            if upload:
                parts.append(f'{format_bytes(upload)}/s up')
            if limit:
                parts.append(f'{format_eta(max(0, limit - elapsed))} left')
            self._write(item_id, message=' · '.join(parts))

    def _run(self):
        while True:
            with self.lock:
                if not self.entries:
                    self.thread = None
                    return
            try:
                self.tick()
            except Exception:
                pass
            time.sleep(self.interval)


SEEDER = Seeder()


def resume_seeding(db, db_factory, lt=None):
    """Restart seeding for completed torrents that had not reached their limits."""
    lt = lt or load_libtorrent()
    SEEDER.configure(db_factory)
    resumed = 0
    rows = db.execute("SELECT id, metadata FROM items WHERE state='downloaded' AND metadata LIKE ?",
                      ('%"seeding"%',)).fetchall()
    for row in rows:
        seeding = json.loads(row['metadata'] or '{}').get('seeding')
        if not seeding or SEEDER.is_seeding(row['id']):
            continue
        path = _seed_resume_path(seeding['folder'], row['id'])
        if lt is None or not path.exists():
            continue
        try:
            params = lt.read_resume_data(path.read_bytes())
            params.save_path = seeding['folder']
            session = _session(lt)
            handle = session.add_torrent(params)
        except Exception:
            continue
        SEEDER.add(row['id'], lt, session, handle, seeding['folder'], seeding.get('ratio'),
                   seeding.get('minutes'), seeding.get('started'))
        resumed += 1
    return resumed
