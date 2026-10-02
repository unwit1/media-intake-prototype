"""Recognise download links copied to the clipboard (as download managers do).

classify_links() picks magnets, .torrent links, file-host share pages,
shortlinks and direct media-file URLs out of copied text, so the app can offer
to queue them in one click. Ordinary web pages are ignored.
"""
from pathlib import PurePosixPath
import re
import urllib.parse

MAX_CLIPBOARD_CHARS = 20_000
MAX_LINKS = 50
_URL = re.compile(r'''(magnet:\?[^\s"'<>]+|https?://[^\s"'<>]+)''', re.I)
KIND_LABELS = {'magnet': 'Magnet link', 'torrent': '.torrent link', 'file_host': 'File host link',
               'shortlink': 'Shortlink', 'direct': 'Direct file'}


def _suffix(path):
    name = PurePosixPath(urllib.parse.unquote(path)).name.casefold()
    for compound in ('.tar.gz', '.tar.bz2', '.tar.xz', '.tar.zst'):
        if name.endswith(compound):
            return compound
    return PurePosixPath(name).suffix


def classify_link(url):
    """Return (kind, format) for a download link, or None for anything else."""
    from .core import EXTENSIONS
    from .file_hosts import FILE_HOSTS, MANUAL_HOSTS
    from .shortlinks import is_shortlink
    url = str(url or '').strip().rstrip('.,;)]}>')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == 'magnet':
        return ('magnet', '.torrent') if 'xt=urn:bt' in parts.query.casefold() else None
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        return None
    host = parts.hostname.casefold()
    suffix = _suffix(parts.path)
    if suffix == '.torrent':
        return 'torrent', '.torrent'
    if host in FILE_HOSTS or host.removeprefix('www.') in MANUAL_HOSTS:
        return 'file_host', suffix if suffix in EXTENSIONS else None
    if is_shortlink(url):
        return 'shortlink', None
    if suffix in EXTENSIONS:
        return 'direct', suffix
    return None


def classify_links(text):
    """All recognised download links in `text`, in order and without duplicates."""
    found, seen = [], set()
    for match in _URL.finditer(str(text or '')[:MAX_CLIPBOARD_CHARS]):
        url = match.group(1).rstrip('.,;)]}>')
        if url in seen:
            continue
        seen.add(url)
        kind = classify_link(url)
        if kind:
            found.append({'url': url, 'kind': kind[0], 'format': kind[1]})
            if len(found) >= MAX_LINKS:
                break
    return found


def queue_item(link, category_format=None):
    """A queue item for a captured link; the download step resolves hosts and shortlinks."""
    url = link['url']
    if link['kind'] in ('magnet', 'torrent'):
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('dn', [''])[0]
        title = name or 'Torrent'
    else:
        title = PurePosixPath(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem or 'Download'
    return {'title': title[:300], 'author': 'Unknown', 'url': url, 'source': url,
            'format': link.get('format') or category_format or '.zip', 'captured_from': 'clipboard',
            'format_guessed': not link.get('format')}
