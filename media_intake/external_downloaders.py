"""Hand queue items and torrent results to a user-installed download manager.

Supported targets:
- Gopeed REST API (HTTP, magnet and .torrent): POST /api/v1/tasks, X-Api-Token header.
- Xtreme Download Manager command line: ``xdm-app.exe --url URL [-o FILE]`` (HTTP only).
- A custom command template for anything else (for example FileCxx), run without a
  shell; ``{url}``, ``{dir}`` and ``{name}`` are substituted per argument.

The same rights-evidence approval used by core.download is checked first.
"""
import json
from pathlib import Path
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request

DOWNLOADERS = ('gopeed', 'xdm', 'command')
DOWNLOADER_LABELS = {'gopeed': 'Gopeed', 'xdm': 'Xtreme Download Manager', 'command': 'Custom command'}
TORRENT_CAPABLE = frozenset({'gopeed', 'command'})


class HandoffError(ValueError):
    pass


def _handoff_url(url):
    url = str(url or '').strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == 'magnet':
        if 'xt=urn:bt' not in parts.query.casefold():
            raise HandoffError('Magnet link has no BitTorrent info hash')
        return url
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise HandoffError('Only HTTP(S) and magnet links can be sent to a download manager')
    return url


def is_torrent(url):
    parts = urllib.parse.urlsplit(str(url or ''))
    if parts.scheme == 'magnet' or parts.path.casefold().endswith('.torrent'):
        return True
    # Jackett proxies .torrent files through /dl/<indexer>/?jackett_apikey=...
    return '/dl/' in parts.path and 'jackett_apikey=' in parts.query


def _gopeed_base(base_url):
    parts = urllib.parse.urlsplit(str(base_url or '').strip().rstrip('/'))
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise HandoffError('Gopeed API address must look like http://127.0.0.1:9999 '
                           '(Settings → Advanced → Communication Protocol: TCP)')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _gopeed_call(base_url, token, method, path, payload=None, timeout=20, opener=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['X-Api-Token'] = token
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(_gopeed_base(base_url) + path, data=data, headers=headers, method=method)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as reply:
            body = json.loads(reply.read(1_000_000).decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise HandoffError('Gopeed rejected the API token') from exc
        raise HandoffError(f'Gopeed returned HTTP {exc.code}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HandoffError(f'Could not reach Gopeed: {getattr(exc, "reason", exc)}. '
                           'Is Gopeed running with the TCP API enabled?') from exc
    except ValueError as exc:
        raise HandoffError('Gopeed returned a response that is not JSON') from exc
    if body.get('code') not in (0, None):
        if body.get('code') == 1001:
            raise HandoffError('Gopeed rejected the API token')
        raise HandoffError(f"Gopeed error {body.get('code')}: {body.get('msg') or 'unknown'}")
    return body.get('data')


def gopeed_info(base_url, token, opener=None):
    return _gopeed_call(base_url, token, 'GET', '/api/v1/info', opener=opener)


def send_to_gopeed(url, base_url, token, directory=None, name=None, labels=None, opener=None):
    request = {'url': _handoff_url(url)}
    if labels:
        request['labels'] = {str(key): str(value) for key, value in labels.items()}
    options = {}
    if directory:
        options['path'] = str(directory)
    if name and not is_torrent(url):
        options['name'] = str(name)
    task_id = _gopeed_call(base_url, token, 'POST', '/api/v1/tasks',
                           {'req': request, 'opts': options}, opener=opener)
    return str(task_id or '')


def command_arguments(template, url, directory=None, name=None):
    template = str(template or '').strip()
    if not template:
        raise HandoffError('Enter the download manager command, for example "C:\\Tools\\app.exe" "{url}"')
    if '{url}' not in template:
        raise HandoffError('The command must contain {url}')
    values = {'url': _handoff_url(url), 'dir': str(directory or ''), 'name': str(name or '')}
    # posix=False keeps Windows backslashes; strip the quotes it leaves in place.
    arguments = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part
                 for part in shlex.split(template, posix=False)]
    return [part.replace('{url}', values['url']).replace('{dir}', values['dir']).replace('{name}', values['name'])
            for part in arguments]


def xdm_arguments(executable, url, directory=None, name=None):
    executable = str(executable or '').strip().strip('"')
    if not executable:
        raise HandoffError('Choose xdm-app.exe in Settings')
    if is_torrent(url):
        raise HandoffError('Xtreme Download Manager cannot download torrents or magnets; use Gopeed')
    arguments = [executable, '--url', _handoff_url(url)]
    if directory and name:
        arguments += ['-o', str(Path(directory) / name)]
    return arguments


def _launch(arguments, popen=None):
    flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        (popen or subprocess.Popen)(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    except OSError as exc:
        raise HandoffError(f'Could not start {arguments[0]}: {exc}') from exc


def _check_rights(item, rights_path):
    if not rights_path or not Path(rights_path).is_file():
        return
    from .rights_evidence import load_rights_evidence, source_download_rights, source_key_for_item
    document = load_rights_evidence(rights_path)
    work_record = (document.get('works') or {}).get(str(item.get('rights_work_key') or ''))
    if isinstance(work_record, dict):
        approved = work_record.get('approved_for_download') is True
    else:
        approved = source_download_rights(document, source_key_for_item(item)) is not None
    if not approved:
        source = source_key_for_item(item)
        raise HandoffError(f'Download not approved: set {source}.approved_for_download to true in {rights_path}')


def hand_off(item, settings, rights_path=None, opener=None, popen=None):
    """Send one item to the configured download manager. Returns a short status line."""
    settings = dict(settings or {})
    downloader = settings.get('downloader') or 'gopeed'
    if downloader not in DOWNLOADERS:
        raise HandoffError(f'Unknown download manager: {downloader}')
    url = item.get('handoff_url') or item.get('magnet') or item.get('url')
    url = _handoff_url(url)
    if is_torrent(url) and downloader not in TORRENT_CAPABLE:
        raise HandoffError(f'{DOWNLOADER_LABELS[downloader]} cannot download torrents or magnets; use Gopeed')
    _check_rights(item, rights_path)
    directory = settings.get('directory') or ''
    name = item.get('source_filename') or ''
    label = DOWNLOADER_LABELS[downloader]
    if downloader == 'gopeed':
        task_id = send_to_gopeed(url, settings.get('gopeed_url') or 'http://127.0.0.1:9999',
                                 settings.get('gopeed_token') or '', directory, name,
                                 {'source': 'Media Intake', 'title': str(item.get('title') or '')[:200]},
                                 opener=opener)
        return f'Sent to Gopeed (task {task_id})' if task_id else 'Sent to Gopeed'
    if downloader == 'xdm':
        _launch(xdm_arguments(settings.get('xdm_path'), url, directory, name), popen)
    else:
        _launch(command_arguments(settings.get('command'), url, directory, name), popen)
    return f'Sent to {label}'
