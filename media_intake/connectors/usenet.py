"""Usenet indexers (NZBGeek, DrunkenSlug) over the Newznab API, and NZB downloads.

Newznab search returns NZB files: lists of Usenet articles, not the media itself.
Fetching those articles needs a Usenet provider account and a newsreader, so the
queue hands NZBs to SABnzbd or NZBGet (configured in Settings → Sources) and
follows the job there for progress, pause, cancel and completion. Without one,
the app can save the .nzb file into the library when the user chooses that.
"""
import json
import time
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

from .base import SearchPage, SourceError, human_size, request
from .releases import parse_release, quality_label, video_subtype
from .torrent_sites import CATEGORY_KINDS

NEWZNAB_NS = '{http://www.newznab.com/DTD/2010/feeds/attributes/}'
NEWZNAB_CATEGORIES = {
    'Books': '7020,3030', 'Light novels': '7020', 'Comics': '7030', 'Manga': '7030',
    'Anime': '5070', 'Videos': '2000,5000', 'Games': '1000,4050', 'Software': '4000',
}
PAGE_SIZE = 100
NEWZNAB_ERRORS = {
    '100': ('the API key was not accepted.', 'auth', 'Copy the API key from your indexer account page into '
            'Settings → Sources.'),
    '101': ('the account is suspended.', 'auth', 'Check your account on the indexer website.'),
    '102': ('the account is not allowed to use the API.', 'auth', 'Check your membership level.'),
    '429': ('the daily API request limit has been reached.', 'limit', 'Searches will work again after the '
            'indexer resets your limit (usually at midnight UTC).'),
    '500': ('the daily request limit has been reached.', 'limit', 'Try again tomorrow.'),
    '910': ('the indexer\'s API is temporarily disabled.', 'unreachable', 'Try again later.'),
}


def _newznab_error(spec, root):
    code = root.get('code') or ''
    message, kind, hint = NEWZNAB_ERRORS.get(code, (f"it reported error {code}: {root.get('description')}",
                                                   'invalid', ''))
    if code.startswith('1') and code not in NEWZNAB_ERRORS:
        kind = 'auth'
    return SourceError(spec.label, message, kind, hint)


def search_newznab(spec, query, category, page=1, config=None, opener=None):
    config = config or {}
    api_key = str(config.get('api_key') or '').strip()
    if not api_key:
        raise SourceError(spec.label, 'searching needs your API key.', 'config',
                          f'Paste the API key from your {spec.label} account into Settings → Sources.')
    params = {'t': 'search', 'q': query, 'apikey': api_key, 'limit': PAGE_SIZE,
              'offset': (max(1, page) - 1) * PAGE_SIZE, 'extended': 1}
    if category in NEWZNAB_CATEGORIES:
        params['cat'] = NEWZNAB_CATEGORIES[category]
    if category == 'Books':
        params['t'] = 'search'
    url = f"{config['base']}/api?{urllib.parse.urlencode(params)}"
    body, _url, _headers = request(spec.label, url, opener=opener,
                                   headers={'Accept': 'application/rss+xml, application/xml'})
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceError(spec.label, 'the indexer sent a reply that is not a Newznab feed.', 'invalid',
                          'Check the indexer address in Settings → Sources.') from exc
    if root.tag == 'error':
        raise _newznab_error(spec, root)
    rows = []
    for item in root.iter('item'):
        attrs = {}
        for attr in item.findall(f'{NEWZNAB_NS}attr'):
            attrs.setdefault(attr.get('name'), []).append(attr.get('value'))
        enclosure = item.find('enclosure')
        link = enclosure.get('url') if enclosure is not None and enclosure.get('url') else item.findtext('link')
        name = (item.findtext('title') or '').strip()
        if not link or not name:
            continue
        size = _int((attrs.get('size') or [None])[0]) or _int(enclosure.get('length') if enclosure is not None
                                                             else None)
        guid = (attrs.get('guid') or [item.findtext('guid') or link])[0]
        info = parse_release(name)
        grabs = _int((attrs.get('grabs') or [None])[0])
        row = {
            'title': info['title'], 'release_title': name, 'author': 'Unknown', 'category': category,
            'kind': CATEGORY_KINDS.get(category, 'video'), 'url': link.strip(), 'format': '.nzb',
            'nzb_url': link.strip(), 'nzb_guid': guid, 'size': size, 'grabs': grabs,
            'published': (item.findtext('pubDate') or '').strip(),
            'details_url': (item.findtext('comments') or '').split('#')[0].strip(),
            'source': spec.label, 'source_key': spec.key, 'provider': spec.label,
            'source_role': 'download', 'downloadable': True, 'transfer': 'usenet',
            'quality': quality_label(info), 'source_id': guid, 'secret_url': True,
            'display_source': ' · '.join(part for part in (
                'Usenet', spec.label, f'{grabs} grabs' if grabs is not None else '', human_size(size)) if part),
        }
        for key in ('year', 'season', 'episode', 'resolution', 'video_source', 'codec', 'file_format'):
            if info.get(key) not in (None, ''):
                row[key] = info[key]
        if info.get('year'):
            row['release_year'] = info['year']
        imdb = (attrs.get('imdb') or attrs.get('imdbid') or [''])[0]
        if imdb:
            row['imdb_id'] = 'tt' + imdb.lstrip('t')
        if category in ('Videos', 'Anime'):
            row['video_subtype'] = video_subtype(info)
        rows.append(row)
    response = root.find(f'.//{NEWZNAB_NS}response')
    total = _int(response.get('total')) if response is not None else None
    offset = _int(response.get('offset')) if response is not None else None
    has_more = (total is not None and (offset or 0) + len(rows) < total) if total is not None else \
        len(rows) >= PAGE_SIZE
    return SearchPage(rows, page, has_more, 'direct')


def redact_nzb_url(url):
    parts = urllib.parse.urlsplit(str(url or ''))
    query = [(key, '***' if key.casefold() in ('apikey', 'r', 'i') else value)
             for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, safe='*')))


def fetch_nzb(source, url, opener=None):
    """Download and check an NZB; indexers answer errors (limits, bad keys) as XML or HTML."""
    body, _url, headers = request(source, url, opener=opener, max_bytes=50_000_000,
                                  headers={'Accept': 'application/x-nzb, application/xml'})
    head = body[:2000].lstrip()
    if head.startswith(b'<?xml') or head.startswith(b'<nzb') or head.startswith(b'<!DOCTYPE nzb'):
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SourceError(source, 'the NZB file is damaged.', 'invalid', 'Choose another release.') from exc
        if root.tag == 'error':
            from .base import ConnectorSpec
            raise _newznab_error(ConnectorSpec(source, source, 'usenet', frozenset(), ''), root)
        if root.tag.endswith('nzb') and any(child.tag.endswith('file') for child in root):
            return body
    raise SourceError(source, 'the indexer did not return an NZB file.', 'invalid',
                      'Your download limit may be used up, or the release was removed.')


# --------------------------------------------------------------------------- SABnzbd / NZBGet
class UsenetClientError(SourceError):
    pass


def _client_label(config):
    return 'NZBGet' if config.get('client') == 'nzbget' else 'SABnzbd'


def _sab(config, params, opener=None, data=None, headers=None):
    base = str(config.get('url') or '').rstrip('/')
    if not base or not config.get('api_key'):
        raise UsenetClientError('SABnzbd', 'it is not set up yet.', 'config',
                                'Enter the SABnzbd address and API key in Settings → Sources.')
    params = dict(params, apikey=config['api_key'], output='json')
    body, _url, _headers = request('SABnzbd', f'{base}/api?{urllib.parse.urlencode(params)}', data=data,
                                   opener=opener, headers=headers, timeout=30)
    try:
        value = json.loads(body.decode('utf-8'))
    except ValueError as exc:
        raise UsenetClientError('SABnzbd', 'it sent a reply the app could not read.', 'invalid',
                                'Check the SABnzbd address.') from exc
    if isinstance(value, dict) and value.get('status') is False:
        error = str(value.get('error') or 'unknown error')
        kind = 'auth' if 'api key' in error.casefold() else 'invalid'
        raise UsenetClientError('SABnzbd', f'it reported: {error}.', kind,
                                'Check the API key in Settings → Sources.' if kind == 'auth' else '')
    return value


def _nzbget(config, method, params=(), opener=None):
    base = str(config.get('url') or '').rstrip('/')
    if not base:
        raise UsenetClientError('NZBGet', 'it is not set up yet.', 'config',
                                'Enter the NZBGet address, username and password in Settings → Sources.')
    parts = urllib.parse.urlsplit(base)
    user = urllib.parse.quote(str(config.get('username') or 'nzbget'), safe='')
    password = urllib.parse.quote(str(config.get('password') or ''), safe='')
    import base64
    token = base64.b64encode(f"{urllib.parse.unquote(user)}:{urllib.parse.unquote(password)}".encode()).decode()
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/') + '/jsonrpc', '', ''))
    payload = json.dumps({'method': method, 'params': list(params), 'id': 1}).encode('utf-8')
    body, _url, _headers = request('NZBGet', url, data=payload, opener=opener, timeout=30,
                                   headers={'Content-Type': 'application/json', 'Authorization': f'Basic {token}'})
    value = json.loads(body.decode('utf-8'))
    if value.get('error'):
        raise UsenetClientError('NZBGet', f"it reported: {value['error'].get('message')}.", 'invalid')
    return value.get('result')


def _multipart(field, filename, content, extra=None):
    boundary = uuid.uuid4().hex
    lines = []
    for name, value in (extra or {}).items():
        lines += [f'--{boundary}', f'Content-Disposition: form-data; name="{name}"', '', str(value)]
    head = '\r\n'.join(lines + [f'--{boundary}',
                                f'Content-Disposition: form-data; name="{field}"; filename="{filename}"',
                                'Content-Type: application/x-nzb', '', '']).encode('utf-8')
    return head + content + f'\r\n--{boundary}--\r\n'.encode(), f'multipart/form-data; boundary={boundary}'


def add_nzb(config, nzb_bytes, name, category='', opener=None):
    """Send an NZB to the configured client; returns the client's job id."""
    filename = ''.join(char for char in name if char not in '\\/:*?"<>|')[:150] + '.nzb'
    if config.get('client') == 'nzbget':
        import base64
        job = _nzbget(config, 'append', (filename, base64.b64encode(nzb_bytes).decode('ascii'),
                                         category or '', 0, False, False, '', 0, 'SCORE', []), opener)
        if not job or int(job) <= 0:
            raise UsenetClientError('NZBGet', 'it did not accept the NZB.', 'invalid')
        return str(job)
    data, content_type = _multipart('name', filename, nzb_bytes)
    params = {'mode': 'addfile', 'nzbname': filename[:-4]}
    if category:
        params['cat'] = category
    value = _sab(config, params, opener, data=data, headers={'Content-Type': content_type})
    ids = value.get('nzo_ids') or []
    if not ids:
        raise UsenetClientError('SABnzbd', 'it did not accept the NZB.', 'invalid')
    return ids[0]


def job_status(config, job_id, opener=None):
    """Return {'state': queued|downloading|paused|processing|completed|failed, done, total, message, path}."""
    if config.get('client') == 'nzbget':
        for group in _nzbget(config, 'listgroups', (0,), opener) or []:
            if str(group.get('NZBID')) == str(job_id):
                total = int(group.get('FileSizeMB') or 0) * 1024 * 1024
                remaining = int(group.get('RemainingSizeMB') or 0) * 1024 * 1024
                status = str(group.get('Status') or '').upper()
                state = 'paused' if 'PAUSED' in status else 'queued' if status == 'QUEUED' else \
                    'processing' if status.startswith(('PP_', 'UNPACK', 'REPAIR', 'VERIFY', 'MOVING')) else 'downloading'
                return {'state': state, 'done': total - remaining, 'total': total or None,
                        'message': status.replace('_', ' ').title()}
        for entry in _nzbget(config, 'history', (False,), opener) or []:
            if str(entry.get('NZBID')) == str(job_id):
                status = str(entry.get('Status') or '').upper()
                total = int(entry.get('FileSizeMB') or 0) * 1024 * 1024
                if status.startswith('SUCCESS'):
                    return {'state': 'completed', 'done': total, 'total': total, 'path': entry.get('DestDir'),
                            'message': 'Completed'}
                return {'state': 'failed', 'message': f"NZBGet: {status.replace('/', ' – ').title()}",
                        'path': entry.get('DestDir')}
        return {'state': 'missing', 'message': 'The job is no longer in NZBGet'}
    queue = (_sab(config, {'mode': 'queue', 'search': '', 'nzo_ids': job_id}, opener).get('queue') or {})
    for slot in queue.get('slots') or []:
        if slot.get('nzo_id') == job_id:
            total = _float_mb(slot.get('mb'))
            left = _float_mb(slot.get('mbleft'))
            status = str(slot.get('status') or '')
            state = {'Paused': 'paused', 'Queued': 'queued', 'Grabbing': 'queued',
                     'Fetching': 'queued'}.get(status, 'downloading')
            message = status + (f" · {slot.get('timeleft')} left" if slot.get('timeleft') and state ==
                                'downloading' else '')
            return {'state': state, 'done': (total - left) if total else None, 'total': total, 'message': message}
    history = (_sab(config, {'mode': 'history', 'nzo_ids': job_id}, opener).get('history') or {})
    for slot in history.get('slots') or []:
        if slot.get('nzo_id') == job_id:
            status = str(slot.get('status') or '')
            size = _int(slot.get('bytes'))
            if status == 'Completed':
                return {'state': 'completed', 'done': size, 'total': size, 'path': slot.get('storage'),
                        'message': 'Completed'}
            if status == 'Failed':
                reason = slot.get('fail_message') or 'the download failed'
                return {'state': 'failed', 'message': f'SABnzbd: {reason}', 'path': slot.get('storage')}
            return {'state': 'processing', 'done': size, 'total': size,
                    'message': {'Verifying': 'Verifying', 'Repairing': 'Repairing', 'Extracting': 'Unpacking',
                                'Moving': 'Moving files', 'Running': 'Running script'}.get(status, status)}
    return {'state': 'missing', 'message': 'The job is no longer in SABnzbd'}


def control_job(config, job_id, action, opener=None):
    """action: pause | resume | delete."""
    if config.get('client') == 'nzbget':
        command = {'pause': 'GroupPause', 'resume': 'GroupResume', 'delete': 'GroupFinalDelete'}[action]
        _nzbget(config, 'editqueue', (command, '', [int(job_id)]), opener)
        return
    if action == 'delete':
        _sab(config, {'mode': 'queue', 'name': 'delete', 'value': job_id, 'del_files': 1}, opener)
    else:
        _sab(config, {'mode': 'queue', 'name': action, 'value': job_id}, opener)


def test_client(config, opener=None):
    if config.get('client') == 'nzbget':
        return f"NZBGet {_nzbget(config, 'version', (), opener)} connected"
    version = _sab(config, {'mode': 'version'}, opener).get('version')
    _sab(config, {'mode': 'queue', 'limit': 1}, opener)  # proves the API key, not just the address
    return f'SABnzbd {version} connected'


def follow_job(config, job_id, *, control, progress, poll=2.0, opener=None, stall_seconds=6 * 3600,
               sleep=time.sleep):
    """Mirror a SABnzbd/NZBGet job into the queue until it completes. Returns the completed folder."""
    from ..transfer import TransferCancelled, TransferError, TransferPaused, format_bytes
    first = True
    last_done, last_change = None, time.monotonic()
    while True:
        try:
            control.check()
        except TransferPaused:
            control_job(config, job_id, 'pause', opener)
            raise
        except TransferCancelled:
            control_job(config, job_id, 'delete', opener)
            raise
        status = job_status(config, job_id, opener)
        state = status['state']
        if state == 'completed':
            return status
        if state == 'failed':
            raise TransferError(status['message'] + '. Retry to search again or choose another release.')
        if state == 'missing':
            raise TransferError(f"{_client_label(config)} no longer has this download (it was removed there). "
                                'Retry to send it again.')
        if state == 'paused' and first:
            control_job(config, job_id, 'resume', opener)  # resumed from Media Intake: un-pause the job
        first = False
        done, total = status.get('done'), status.get('total')
        if done != last_done:
            last_done, last_change = done, time.monotonic()
        elif time.monotonic() - last_change > stall_seconds:
            raise TransferError(f'No Usenet progress for {stall_seconds // 3600} hours; check '
                                f'{_client_label(config)}. Retry continues the same job.')
        label = f"{_client_label(config)}: {status.get('message') or state.title()}"
        if total:
            label += f' · {format_bytes(done or 0)} of {format_bytes(total)}'
        progress(int(done) if done is not None else None, int(total) if total else None, label)
        sleep(poll)


def _float_mb(value):
    try:
        return int(float(value) * 1024 * 1024)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
