"""Native BitTorrent downloads for queued magnet and .torrent items (optional).

Uses libtorrent when installed (``pip install -r requirements-torrent.txt``);
without it, torrent items explain how to enable it or send them to Gopeed.
Behaviour matches the HTTP engine: progress with speed/peers/ETA, pause keeps
fast-resume data beside the download, cancel deletes partial data, the global
speed limit applies, and transfers stop when complete (download only, no seeding).
"""
from pathlib import Path
import threading
import time
import urllib.parse

from .transfer import LIMITER, TransferCancelled, TransferError, TransferPaused, format_bytes, format_eta

MAX_TORRENT_FILE_BYTES = 10_000_000
STALL_SECONDS = 15 * 60
_SESSION = None
_SESSION_LOCK = threading.Lock()


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
        _SESSION.apply_settings({'download_rate_limit': int(LIMITER.rate or 0)})
        return _SESSION


def reset_session():
    global _SESSION
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
                     stall_seconds=STALL_SECONDS):
    """Download a magnet or .torrent into `folder`. Returns (path, total_bytes)."""
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
                _remove(lt, session, handle)
                resume.unlink(missing_ok=True)
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
