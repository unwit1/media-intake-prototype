"""Search a user-run Jackett instance through its Torznab API.

Jackett aggregates whichever indexers the user configured in it. Results are
torrents or magnets, which the built-in HTTP/IRC downloader does not handle, so
they are handed to an external downloader (external_downloaders.py) after the
same rights-evidence approval check, under the 'jackett' source key.
"""
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SOURCE_KEY = 'jackett'
TORZNAB_NS = 'http://torznab.com/schemas/2015/feed'
MAX_RESPONSE_BYTES = 8_000_000
# Newznab/Torznab category IDs for each dashboard category.
CATEGORY_IDS = {
    'Books': (7000, 7020, 3030), 'Light novels': (7020,), 'Web serials': (7020,),
    'Fanfiction': (7020,), 'Comics': (7030,), 'Manga': (7030,),
    'Anime': (5070, 2000), 'Videos': (2000, 5000), 'Games': (4050, 1000),
    'Software': (4000,),
}


class JackettError(ValueError):
    pass


def _base(base_url):
    parts = urllib.parse.urlsplit(str(base_url or '').strip().rstrip('/'))
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise JackettError('Jackett URL must look like http://127.0.0.1:9117')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def torznab_url(base_url, api_key, query, category=None, indexer='all'):
    if not str(api_key or '').strip():
        raise JackettError('Enter the Jackett API key from the Jackett dashboard')
    indexer = urllib.parse.quote(str(indexer or 'all').strip() or 'all', safe='')
    params = {'apikey': api_key.strip(), 't': 'search', 'q': str(query or '').strip()}
    if category in CATEGORY_IDS:
        params['cat'] = ','.join(str(value) for value in CATEGORY_IDS[category])
    return f'{_base(base_url)}/api/v2.0/indexers/{indexer}/results/torznab/api?{urllib.parse.urlencode(params)}'


def redact(url):
    """Hide Jackett API keys that appear in proxied download links."""
    parts = urllib.parse.urlsplit(str(url or ''))
    if not parts.query:
        return str(url or '')
    query = [(key, '***' if key.casefold() in ('apikey', 'jackett_apikey') else value)
             for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, safe='*')))


def _fetch(url, timeout):
    request = urllib.request.Request(url, headers={'User-Agent': 'MediaArchiveImporter/0.3 (+Jackett)',
                                                   'Accept': 'application/rss+xml, application/xml'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            body = reply.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise JackettError('Jackett rejected the API key') from exc
        raise JackettError(f'Jackett returned HTTP {exc.code}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JackettError(f'Could not reach Jackett: {getattr(exc, "reason", exc)}') from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise JackettError('Jackett response was too large')
    return body


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_torznab(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise JackettError('Jackett returned a response that is not Torznab XML') from exc
    if root.tag == 'error':
        raise JackettError(f"Jackett error {root.get('code')}: {root.get('description')}")
    results = []
    for item in root.iter('item'):
        attrs = {}
        for attr in item.findall(f'{{{TORZNAB_NS}}}attr'):
            attrs.setdefault(attr.get('name'), []).append(attr.get('value'))
        enclosure = item.find('enclosure')
        link = (item.findtext('link') or '').strip()
        if enclosure is not None and enclosure.get('url'):
            link = enclosure.get('url').strip()
        magnet = (attrs.get('magneturl') or [''])[0]
        if link.startswith('magnet:'):
            magnet, link = magnet or link, ''
        if not (magnet or link):
            continue
        indexer = item.find('jackettindexer')
        size = _int(item.findtext('size')) or _int((attrs.get('size') or [None])[0])
        results.append({
            'title': (item.findtext('title') or 'Untitled').strip()[:300],
            'size': size,
            'seeders': _int((attrs.get('seeders') or [None])[0]),
            'peers': _int((attrs.get('peers') or [None])[0]),
            'info_hash': (attrs.get('infohash') or [''])[0],
            'indexer': (indexer.text or indexer.get('id') or '').strip() if indexer is not None else '',
            'published': (item.findtext('pubDate') or '').strip(),
            'details_url': (item.findtext('comments') or item.findtext('guid') or '').strip(),
            'category_ids': [_int(value) for value in attrs.get('category', []) if _int(value) is not None],
            'magnet': magnet,
            'torrent_url': link,
        })
    return results


def search_jackett(query, base_url, api_key, category=None, indexer='all', limit=50, timeout=60,
                   fetch=None):
    if not str(query or '').strip():
        raise JackettError('Enter something to search for')
    body = (fetch or _fetch)(torznab_url(base_url, api_key, query, category, indexer), timeout)
    hits = sorted(parse_torznab(body), key=lambda row: -(row['seeders'] or 0))[:limit]
    results = []
    for hit in hits:
        label = f"Jackett · {hit['indexer']}" if hit['indexer'] else 'Jackett'
        results.append({
            **hit, 'category': category, 'provider': label, 'source_key': SOURCE_KEY,
            'source_role': 'download', 'site': SOURCE_KEY, 'format': 'torrent',
            'external_download': True, 'downloadable': False,
            'handoff_url': hit['magnet'] or hit['torrent_url'],
            'source': redact(hit['details_url'] or hit['torrent_url'] or hit['magnet']),
            'display_source': f"{label} · {hit['seeders'] if hit['seeders'] is not None else '?'} seeders",
        })
    return results


def test_connection(base_url, api_key, fetch=None):
    """Request Jackett's Torznab capabilities; returns the supported search modes."""
    url = (f'{_base(base_url)}/api/v2.0/indexers/all/results/torznab/api?'
           + urllib.parse.urlencode({'apikey': str(api_key or '').strip(), 't': 'caps'}))
    try:
        root = ET.fromstring((fetch or _fetch)(url, 30))
    except ET.ParseError as exc:
        raise JackettError('Jackett returned a response that is not Torznab XML') from exc
    if root.tag == 'error':
        raise JackettError(f"Jackett error {root.get('code')}: {root.get('description')}")
    return [child.tag for child in root.findall('searching/*') if child.get('available') != 'no']
