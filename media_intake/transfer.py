"""Native HTTP transfer engine: segmented, resumable, pausable and rate limited.

Folds the useful parts of standalone download managers (Gopeed, XDM, FileCxx)
into the app's own queue:

- one probe request detects byte-range support, size and validators (ETag /
  Last-Modified), and rejects HTML pages before anything is written;
- large files are split across several connections into a preallocated part file;
- progress is checkpointed beside the part file, so a paused, failed or
  interrupted transfer resumes after a restart (If-Range guards against the
  file changing on the server);
- transient failures retry with backoff, honouring Retry-After;
- pause and cancel requests and a global speed limit apply to running transfers.
"""
from dataclasses import dataclass
import email.message
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = 'MediaIntakePrototype/0.1 (personal library intake)'
CHUNK = 256 * 1024
SEGMENT_MIN_BYTES = 4 * 1024 * 1024
CHECKPOINT_SECONDS = 1.0
HTML_TYPES = ('text/html', 'application/xhtml+xml')
HLS_TYPES = frozenset({'application/vnd.apple.mpegurl', 'application/x-mpegurl',
                       'audio/mpegurl', 'audio/x-mpegurl'})


class TransferError(ValueError):
    pass


class TransferPaused(Exception):
    pass


class TransferCancelled(Exception):
    pass


class StreamDetected(Exception):
    """The link serves an HLS playlist; save it with streams.download_hls instead."""

    def __init__(self, url):
        super().__init__(url)
        self.url = url


class _Retryable(Exception):
    def __init__(self, message, delay=None):
        super().__init__(message)
        self.delay = delay


# ---------------------------------------------------------------------------
# Controls shared by every transfer in this process.

class RateLimiter:
    """Token bucket shared by all connections; 0 bytes/second means unlimited."""

    def __init__(self, bytes_per_second=0):
        self._lock = threading.Lock()
        self.rate = max(0, int(bytes_per_second or 0))
        self._allowance = float(self.rate)
        self._stamp = time.monotonic()

    def set_rate(self, bytes_per_second):
        with self._lock:
            self.rate = max(0, int(bytes_per_second or 0))
            self._allowance = float(self.rate)
            self._stamp = time.monotonic()

    def consume(self, amount):
        while True:
            with self._lock:
                if not self.rate:
                    return
                now = time.monotonic()
                self._allowance = min(float(self.rate), self._allowance + (now - self._stamp) * self.rate)
                self._stamp = now
                if self._allowance >= amount or self._allowance >= self.rate:
                    self._allowance -= amount
                    return
                wait = (amount - self._allowance) / self.rate
            time.sleep(min(wait, 0.25))


LIMITER = RateLimiter()
_CONTROLS = {}
_CONTROLS_LOCK = threading.Lock()


class Control:
    def __init__(self):
        self.pause = threading.Event()
        self.cancel = threading.Event()

    def check(self):
        if self.cancel.is_set():
            raise TransferCancelled('Download cancelled')
        if self.pause.is_set():
            raise TransferPaused('Download paused')


def control_for(key):
    with _CONTROLS_LOCK:
        return _CONTROLS.setdefault(key, Control())


def claim_control(key):
    """Atomically claim a transfer slot; None when that item is already transferring."""
    with _CONTROLS_LOCK:
        if key in _CONTROLS:
            return None
        control = _CONTROLS[key] = Control()
        return control


def release_control(key):
    with _CONTROLS_LOCK:
        _CONTROLS.pop(key, None)


def request_pause(key):
    with _CONTROLS_LOCK:
        control = _CONTROLS.get(key)
    if control:
        control.pause.set()
    return control is not None


def request_cancel(key):
    with _CONTROLS_LOCK:
        control = _CONTROLS.get(key)
    if control:
        control.cancel.set()
    return control is not None


def is_active(key):
    with _CONTROLS_LOCK:
        return key in _CONTROLS


# ---------------------------------------------------------------------------
# Helpers.

@dataclass
class TransferResult:
    path: Path
    size: int
    content_type: str
    final_url: str
    filename: str
    resumed: bool
    connections: int


def format_bytes(value):
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024


def format_eta(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f'{seconds // 3600}h {seconds % 3600 // 60:02d}m'
    if seconds >= 60:
        return f'{seconds // 60}m {seconds % 60:02d}s'
    return f'{seconds}s'


def filename_from_headers(headers, url):
    message = email.message.Message()
    message['Content-Disposition'] = headers.get('Content-Disposition') or ''
    name = message.get_filename() or ''
    if not name:
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1])
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', name).strip(' .')[:200]


def _retry_after(headers):
    value = (headers or {}).get('Retry-After')
    try:
        return min(120.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def _open(url, headers, timeout, opener):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, **headers})
    try:
        return opener(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        delay = _retry_after(exc.headers)
        code = exc.code
        exc.close()
        if code in (408, 425, 429, 500, 502, 503, 504):
            raise _Retryable(f'HTTP {code}', delay) from exc
        if code == 416:
            raise TransferError('Server rejected the requested byte range') from exc
        raise TransferError(f'HTTP {code}') from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise _Retryable(str(getattr(exc, 'reason', exc))) from exc


def _with_retries(action, retries, control):
    attempt = 0
    while True:
        control.check()
        try:
            return action()
        except _Retryable as exc:
            attempt += 1
            if attempt > retries:
                raise TransferError(f'Download failed after {retries} retries: {exc}') from exc
            delay = exc.delay if exc.delay is not None else min(30.0, 2 ** attempt)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                control.check()
                time.sleep(min(0.2, deadline - time.monotonic()))


class _Progress:
    """Aggregates bytes from all connections and reports speed and ETA."""

    def __init__(self, total, done, callback, connections):
        self.total, self.done, self.callback, self.connections = total, done, callback, connections
        self.lock = threading.Lock()
        self.samples = [(time.monotonic(), done)]
        self.last_report = 0.0

    def add(self, amount, report=True):
        with self.lock:
            self.done += amount
        if report:
            self.tick()

    def tick(self, force=False):
        """Report progress; call only from the thread that owns the callback's resources."""
        with self.lock:
            now = time.monotonic()
            if not force and now - self.last_report < 0.5:
                return
            self.last_report = now
            self.samples.append((now, self.done))
            self.samples = [sample for sample in self.samples if now - sample[0] <= 5] or self.samples[-1:]
            done = self.done
        self.report(now, done)

    def speed(self, now=None):
        first, last = self.samples[0], self.samples[-1]
        elapsed = (now or last[0]) - first[0]
        return (last[1] - first[1]) / elapsed if elapsed > 0 else 0.0

    def report(self, now=None, done=None, phase='Downloading'):
        if not self.callback:
            return
        done = self.done if done is None else done
        speed = self.speed(now)
        parts = [phase]
        if speed > 0:
            parts.append(f'{format_bytes(speed)}/s')
            if self.total:
                parts.append(f'{format_eta((self.total - done) / speed)} left')
        if self.connections > 1:
            parts.append(f'{self.connections} connections')
        if LIMITER.rate:
            parts.append(f'limit {format_bytes(LIMITER.rate)}/s')
        self.callback(done, self.total, ' · '.join(parts))


# ---------------------------------------------------------------------------
# The transfer.

def _state_path(part):
    return Path(str(part) + '.json')


def _load_state(part, url, total, validator):
    try:
        state = json.loads(_state_path(part).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (state.get('url') != url or state.get('total') != total or state.get('validator') != validator
            or not part.exists() or part.stat().st_size != total):
        return None
    return state


def _save_state(part, state):
    path = _state_path(part)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    os.replace(temporary, path)


def discard_partial(part):
    for path in (Path(part), _state_path(Path(part))):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def partial_progress(part):
    """Bytes already saved for a resumable partial download, or 0."""
    try:
        state = json.loads(_state_path(Path(part)).read_text(encoding='utf-8'))
        return sum(segment[2] for segment in state.get('segments', [])), state.get('total')
    except (OSError, ValueError, TypeError, IndexError):
        return 0, None


def _plan_segments(total, connections):
    connections = max(1, min(connections, total // SEGMENT_MIN_BYTES or 1))
    size = -(-total // connections)
    return [[start, min(total, start + size) - 1, 0] for start in range(0, total, size)]


def fetch(url, part, *, max_bytes, connections=4, retries=4, timeout=60, progress=None,
          control=None, opener=None):
    """Download `url` into `part`; returns TransferResult. Raises TransferPaused or
    TransferCancelled when asked to stop; the part file is kept for a later resume
    unless cancelled."""
    part = Path(part)
    part.parent.mkdir(parents=True, exist_ok=True)
    control = control or Control()
    opener = opener or urllib.request.urlopen

    def probe():
        return _open(url, {'Range': 'bytes=0-0'}, timeout, opener)

    response = _with_retries(probe, retries, control)
    headers = response.headers
    content_type = (headers.get('Content-Type') or '').split(';')[0].strip().lower()
    final_url = response.geturl()
    filename = filename_from_headers(headers, final_url)
    if content_type in HTML_TYPES:
        response.close()
        raise TransferError('URL returned an HTML page, not a media file')
    if content_type in HLS_TYPES:
        response.close()
        raise StreamDetected(final_url)
    status = getattr(response, 'status', 200)
    match = re.fullmatch(r'bytes\s+0-0/(\d+)', headers.get('Content-Range') or '')
    if status != 206 or not match:
        # No range support: one stream, from the start.
        try:
            return _single_stream(response, part, final_url, content_type, filename, max_bytes,
                                  progress, control)
        finally:
            response.close()
    response.close()
    total = int(match.group(1))
    if total <= 0:
        raise TransferError('Empty download')
    if total > max_bytes:
        raise TransferError(f'File is {format_bytes(total)}, above the {format_bytes(max_bytes)} limit')
    free = shutil.disk_usage(part.parent).free
    validator = headers.get('ETag') or headers.get('Last-Modified') or ''
    state = _load_state(part, final_url, total, validator)
    resumed = state is not None
    if not resumed:
        if free < total:
            raise TransferError(f'Not enough disk space: need {format_bytes(total)}, have {format_bytes(free)}')
        discard_partial(part)
        with open(part, 'wb') as handle:
            handle.truncate(total)
        state = {'url': final_url, 'total': total, 'validator': validator,
                 'segments': _plan_segments(total, connections)}
        _save_state(part, state)
    segments = state['segments']
    tracker = _Progress(total, sum(segment[2] for segment in segments),
                        progress, sum(1 for s in segments if s[2] < s[1] - s[0] + 1))
    lock = threading.Lock()
    errors = []
    stop = threading.Event()
    last_save = [time.monotonic()]

    def checkpoint(force=False):
        with lock:
            if force or time.monotonic() - last_save[0] >= CHECKPOINT_SECONDS:
                _save_state(part, state)
                last_save[0] = time.monotonic()

    def run_segment(segment):
        def attempt():
            start = segment[0] + segment[2]
            if start > segment[1]:
                return
            request_headers = {'Range': f'bytes={start}-{segment[1]}'}
            if validator:
                request_headers['If-Range'] = validator
            reply = _open(final_url, request_headers, timeout, opener)
            try:
                if getattr(reply, 'status', 200) != 206:
                    raise TransferError('The file changed on the server; restart the download')
                with open(part, 'r+b') as handle:
                    handle.seek(start)
                    while segment[2] < segment[1] - segment[0] + 1:
                        if stop.is_set():
                            return
                        control.check()
                        want = min(CHUNK, segment[1] - segment[0] + 1 - segment[2])
                        try:
                            chunk = reply.read(want)
                        except (OSError, TimeoutError) as exc:
                            raise _Retryable(str(exc)) from exc
                        if not chunk:
                            raise _Retryable('Connection closed early')
                        LIMITER.consume(len(chunk))
                        handle.write(chunk)
                        with lock:
                            segment[2] += len(chunk)
                        tracker.add(len(chunk), report=False)
                        checkpoint()
            finally:
                reply.close()
        try:
            _with_retries(attempt, retries, control)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller below
            errors.append(exc)
            stop.set()

    pending = [segment for segment in segments if segment[2] < segment[1] - segment[0] + 1]
    threads = [threading.Thread(target=run_segment, args=(segment,), daemon=True) for segment in pending]
    tracker.report()
    for thread in threads:
        thread.start()
    # Workers only count bytes; progress (often a SQLite write) is reported from
    # this thread, which owns the caller's connection.
    while any(thread.is_alive() for thread in threads):
        for thread in threads:
            thread.join(timeout=0.25)
            break
        tracker.tick()
    tracker.tick(force=True)
    checkpoint(force=True)
    if errors:
        for kind in (TransferCancelled, TransferPaused):
            if any(isinstance(error, kind) for error in errors):
                if kind is TransferCancelled:
                    discard_partial(part)
                raise kind(str(next(e for e in errors if isinstance(e, kind))))
        raise errors[0]
    _state_path(part).unlink(missing_ok=True)
    return TransferResult(part, total, content_type, final_url, filename, resumed, len(threads))


def _single_stream(response, part, final_url, content_type, filename, max_bytes, progress, control):
    try:
        expected = int(response.headers.get('Content-Length') or '') or None
    except ValueError:
        expected = None
    if expected and expected > max_bytes:
        raise TransferError(f'File is {format_bytes(expected)}, above the {format_bytes(max_bytes)} limit')
    if expected and shutil.disk_usage(part.parent).free < expected:
        raise TransferError(f'Not enough disk space for {format_bytes(expected)}')
    discard_partial(part)
    tracker = _Progress(expected, 0, progress, 1)
    total = 0
    try:
        with open(part, 'wb') as handle:
            while True:
                control.check()
                try:
                    chunk = response.read(CHUNK)
                except (OSError, TimeoutError) as exc:
                    raise TransferError(f'Connection lost: {exc}') from exc
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise TransferError('File exceeds size limit')
                LIMITER.consume(len(chunk))
                handle.write(chunk)
                tracker.add(len(chunk))
    except TransferPaused:
        # Without range support there is nothing to resume from.
        discard_partial(part)
        raise
    except BaseException:
        discard_partial(part)
        raise
    if not total:
        discard_partial(part)
        raise TransferError('Empty download')
    if expected is not None and total != expected:
        discard_partial(part)
        raise TransferError('Download truncated: byte count differs from Content-Length')
    return TransferResult(part, total, content_type, final_url, filename, False, 1)
