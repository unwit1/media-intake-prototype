"""Save HLS (.m3u8) video streams to a single file — downloading only, no playback.

- A master playlist's best variant is chosen (highest bandwidth, optionally capped
  by resolution); a separate audio rendition is merged with ffmpeg when present.
- Media segments download in parallel into ``.partial-<id>.hls/`` beside the
  library; finished segments are kept, so pause, failures and restarts resume.
- Byte-range segments and fMP4 initialisation sections (EXT-X-MAP) are supported.
- Live playlists (no EXT-X-ENDLIST) are recorded until the stream ends, or until
  Pause, which keeps what was recorded.
- Segments are joined in order: MPEG-TS becomes ``.ts`` (remuxed to ``.mp4``
  without re-encoding when ffmpeg is installed) and fMP4 becomes ``.mp4``.
- Encrypted streams (any EXT-X-KEY / EXT-X-SESSION-KEY method other than NONE,
  including DRM key systems) are refused rather than decrypted.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .transfer import (HLS_TYPES, LIMITER, Control, TransferError, TransferPaused, _open, _Retryable,
                       _with_retries, format_bytes)
MAX_PLAYLIST_BYTES = 5_000_000
MAX_SEGMENTS = 20_000
LIVE_IDLE_LIMIT = 6  # playlist refreshes without new segments before a live stream counts as ended


class StreamError(TransferError):
    pass


def is_hls_url(url):
    return urllib.parse.urlsplit(str(url or '')).path.casefold().endswith(('.m3u8', '.m3u'))


def is_stream_item(item):
    return item.get('stream') == 'hls' or is_hls_url(item.get('resolved_url') or item.get('url'))


def ffmpeg_path():
    return shutil.which('ffmpeg')


def _attributes(text):
    return {key: value.strip('"') for key, value in
            re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text)}


def _check_encryption(line):
    tag, _, rest = line.partition(':')
    method = _attributes(rest).get('METHOD', 'NONE').upper()
    if method != 'NONE':
        raise StreamError(f'This stream is encrypted ({method}); encrypted and DRM-protected streams are not saved')


def parse_playlist(text, base_url):
    """Return {'variants': [...]} for a master playlist or a media playlist dict."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines or not lines[0].startswith('#EXTM3U'):
        raise StreamError('Not an HLS playlist')
    if any(line.startswith('#EXT-X-STREAM-INF') for line in lines):
        audio, variants = {}, []
        for index, line in enumerate(lines):
            if line.startswith('#EXT-X-SESSION-KEY'):
                _check_encryption(line)
            if line.startswith('#EXT-X-MEDIA:'):
                attrs = _attributes(line.split(':', 1)[1])
                if attrs.get('TYPE') == 'AUDIO' and attrs.get('URI'):
                    group = audio.setdefault(attrs.get('GROUP-ID', ''), [])
                    group.append({'uri': urllib.parse.urljoin(base_url, attrs['URI']),
                                  'default': attrs.get('DEFAULT') == 'YES'})
            if line.startswith('#EXT-X-STREAM-INF:'):
                attrs = _attributes(line.split(':', 1)[1])
                uri = next((candidate for candidate in lines[index + 1:] if not candidate.startswith('#')), None)
                if not uri:
                    continue
                height = int(attrs['RESOLUTION'].split('x')[1]) if 'x' in attrs.get('RESOLUTION', '') else 0
                variants.append({'uri': urllib.parse.urljoin(base_url, uri),
                                 'bandwidth': int(attrs.get('BANDWIDTH') or 0), 'height': height,
                                 'audio': audio.get(attrs.get('AUDIO', ''), [])})
        for variant in variants:
            variant['audio'] = sorted(variant['audio'], key=lambda a: not a['default'])
        return {'variants': variants}
    playlist = {'segments': [], 'endlist': False, 'target': 6.0, 'sequence': 0}
    duration, byterange, init = None, None, None
    next_offset = {}
    for line in lines[1:]:
        if line.startswith(('#EXT-X-KEY', '#EXT-X-SESSION-KEY')):
            _check_encryption(line)
        elif line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            playlist['sequence'] = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-TARGETDURATION:'):
            playlist['target'] = float(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-ENDLIST'):
            playlist['endlist'] = True
        elif line.startswith('#EXT-X-MAP:'):
            attrs = _attributes(line.split(':', 1)[1])
            init = {'uri': urllib.parse.urljoin(base_url, attrs['URI']), 'range': None}
            if attrs.get('BYTERANGE'):
                length, _, offset = attrs['BYTERANGE'].partition('@')
                init['range'] = (int(offset or 0), int(length))
            playlist['init'] = init
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',')[0] or 0)
        elif line.startswith('#EXT-X-BYTERANGE:'):
            length, _, offset = line.split(':', 1)[1].partition('@')
            byterange = (int(length), int(offset) if offset else None)
        elif not line.startswith('#'):
            uri = urllib.parse.urljoin(base_url, line)
            segment = {'uri': uri, 'duration': duration or 0.0,
                       'sequence': playlist['sequence'] + len(playlist['segments']), 'range': None}
            if byterange:
                length, offset = byterange
                offset = next_offset.get(uri, 0) if offset is None else offset
                segment['range'] = (offset, length)
                next_offset[uri] = offset + length
            playlist['segments'].append(segment)
            duration = byterange = None
    if len(playlist['segments']) > MAX_SEGMENTS:
        raise StreamError('Playlist has too many segments')
    return playlist


def choose_variant(variants, max_height=None):
    usable = [v for v in variants if not max_height or not v['height'] or v['height'] <= max_height] or variants
    return max(usable, key=lambda v: (v['bandwidth'], v['height']))


def _fetch_bytes(url, opener, retries, control, range_=None, limit=None):
    def attempt():
        headers = {}
        if range_:
            headers['Range'] = f'bytes={range_[0]}-{range_[0] + range_[1] - 1}'
        reply = _open(url, headers, 60, opener)
        try:
            chunks, size = [], 0
            while True:
                control.check()
                try:
                    chunk = reply.read(256 * 1024)
                except (OSError, TimeoutError) as exc:
                    raise _Retryable(str(exc)) from exc
                if not chunk:
                    break
                size += len(chunk)
                if limit and size > limit:
                    raise StreamError('Response too large')
                LIMITER.consume(len(chunk))
                chunks.append(chunk)
            data = b''.join(chunks)
            if range_ and getattr(reply, 'status', 200) == 200 and len(data) > range_[1]:
                data = data[range_[0]:range_[0] + range_[1]]  # server ignored Range
            return data
        finally:
            reply.close()
    return _with_retries(attempt, retries, control)


def _fetch_playlist(url, opener, retries, control):
    return parse_playlist(_fetch_bytes(url, opener, retries, control, limit=MAX_PLAYLIST_BYTES)
                          .decode('utf-8', 'replace'), url)


class _Track:
    """Downloads one media playlist's segments into a folder, resumably."""

    def __init__(self, url, folder, opener, retries, control):
        self.url, self.folder = url, Path(folder)
        self.opener, self.retries, self.control = opener, retries, control
        self.folder.mkdir(parents=True, exist_ok=True)
        self.playlist = _fetch_playlist(url, opener, retries, control)
        self.known = {}  # sequence -> segment
        self.add(self.playlist['segments'])
        self.bytes = sum(path.stat().st_size for path in self.folder.glob('*.seg'))

    def add(self, segments):
        new = [s for s in segments if s['sequence'] not in self.known]
        for segment in new:
            self.known[segment['sequence']] = segment
        return new

    def path(self, segment):
        return self.folder / f"{segment['sequence']:010d}.seg"

    def pending(self):
        return [s for seq, s in sorted(self.known.items()) if not self.path(s).exists()]

    def fetch(self, segment):
        target = self.path(segment)
        data = _fetch_bytes(segment['uri'], self.opener, self.retries, self.control, segment['range'])
        if not data:
            raise _Retryable('Empty segment')
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(target)
        return len(data)

    def init_bytes(self):
        init = self.playlist.get('init')
        if not init:
            return b''
        cached = self.folder / 'init.bin'
        if not cached.exists():
            cached.write_bytes(_fetch_bytes(init['uri'], self.opener, self.retries, self.control, init['range']))
        return cached.read_bytes()

    def join(self, target):
        with open(target, 'wb') as out:
            out.write(self.init_bytes())
            for _seq, segment in sorted(self.known.items()):
                path = self.path(segment)
                if path.exists():
                    out.write(path.read_bytes())
        return target


def _run_segments(tracks, connections, control, progress, live=False):
    """Fetch every pending segment of every track; returns when all are saved."""
    total_segments = sum(len(track.known) for track in tracks)
    done_segments = sum(len(track.known) - len(track.pending()) for track in tracks)
    downloaded = sum(track.bytes for track in tracks)
    started, start_bytes = time.monotonic(), downloaded
    jobs = [(track, segment) for track in tracks for segment in track.pending()]
    with ThreadPoolExecutor(max_workers=max(1, connections)) as pool:
        futures = {pool.submit(track.fetch, segment): track for track, segment in jobs}
        while futures:
            finished, _ = wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in finished:
                track = futures.pop(future)
                try:
                    size = future.result()
                except BaseException:
                    for other in futures:
                        other.cancel()
                    raise
                track.bytes += size
                downloaded += size
                done_segments += 1
            elapsed = time.monotonic() - started
            speed = (downloaded - start_bytes) / elapsed if elapsed > 0 else 0
            estimate = int(downloaded / done_segments * total_segments) if done_segments else None
            parts = ['Recording live stream' if live else 'Downloading stream',
                     f'{done_segments}/{total_segments} segments']
            if speed:
                parts.append(f'{format_bytes(speed)}/s')
            if progress:
                progress(downloaded, None if live else estimate, ' · '.join(parts))
    return downloaded


def download_hls(url, workdir, *, connections=4, retries=4, control=None, progress=None, opener=None,
                 max_height=None, ffmpeg=None, run=None, poll=None):
    """Save the stream at `url`; returns (path, extension) of the finished file inside `workdir`."""
    control = control or Control()
    opener = opener or urllib.request.urlopen
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_path() if ffmpeg is None else ffmpeg
    run = run or subprocess.run
    first = _fetch_playlist(url, opener, retries, control)
    audio_url = None
    if 'variants' in first:
        if not first['variants']:
            raise StreamError('The stream has no playable variants')
        variant = choose_variant(first['variants'], max_height)
        url = variant['uri']
        if variant['audio']:
            if not ffmpeg:
                raise StreamError('This stream keeps its audio in a separate track; install ffmpeg to save it')
            audio_url = variant['audio'][0]['uri']
    tracks = [_Track(url, workdir / 'video', opener, retries, control)]
    if audio_url:
        tracks.append(_Track(audio_url, workdir / 'audio', opener, retries, control))
    live = not tracks[0].playlist['endlist']
    stopped_live = False
    idle = 0
    while True:
        try:
            _run_segments(tracks, connections, control, progress, live)
        except TransferPaused:
            if not live:
                raise
            stopped_live = True  # Pause ends a live recording and keeps it.
        if not live or stopped_live:
            break
        wait_seconds = poll if poll is not None else max(1.0, tracks[0].playlist['target'] / 2)
        deadline = time.monotonic() + wait_seconds
        try:
            while time.monotonic() < deadline:
                control.check()
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        except TransferPaused:
            break
        ended, added = True, 0
        for track in tracks:
            track.playlist = _fetch_playlist(track.url, opener, retries, control)
            added += len(track.add(track.playlist['segments']))
            ended = ended and track.playlist['endlist']
        idle = 0 if added else idle + 1
        if ended:
            _run_segments(tracks, connections, control, progress, live)
            break
        if idle >= LIVE_IDLE_LIMIT:
            break
    if not any(track.path(s).exists() for track in tracks for s in track.known.values()):
        raise StreamError('No stream segments were saved')
    fmp4 = bool(tracks[0].playlist.get('init'))
    video = tracks[0].join(workdir / ('joined-video.mp4' if fmp4 else 'joined-video.ts'))
    if audio_url:
        audio = tracks[1].join(workdir / ('joined-audio.mp4' if tracks[1].playlist.get('init') else 'joined-audio.ts'))
        output = workdir / 'stream.mp4'
        result = run([ffmpeg, '-y', '-loglevel', 'error', '-i', str(video), '-i', str(audio),
                      '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', str(output)],
                     capture_output=True, timeout=3600)
        if result.returncode != 0 or not output.exists():
            raise StreamError('ffmpeg could not merge the video and audio tracks')
        return output, '.mp4'
    if fmp4:
        return video, '.mp4'
    if ffmpeg:
        output = workdir / 'stream.mp4'
        try:
            result = run([ffmpeg, '-y', '-loglevel', 'error', '-i', str(video), '-c', 'copy',
                          '-bsf:a', 'aac_adtstoasc', '-movflags', '+faststart', str(output)],
                         capture_output=True, timeout=3600)
            if result.returncode == 0 and output.exists() and output.stat().st_size:
                return output, '.mp4'
        except (OSError, subprocess.SubprocessError):
            pass
    return video, '.ts'


def discard_stream_partial(workdir):
    shutil.rmtree(workdir, ignore_errors=True)
