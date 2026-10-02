"""Public file-host resolution shared by download-source connectors.

No login, CAPTCHA, decryption, limit circumvention or premium-link generation.
Resolve only links the host exposes through its public API/HTML. Shortlinks in
front of a host are followed only where they expose the destination (shortlinks.py).
Source approval continues to belong to the caller and is checked again by core.download.
"""
from dataclasses import dataclass
from html.parser import HTMLParser
import json
from pathlib import PurePosixPath
import re
import urllib.parse

from .webcomics.http import HttpClient


class ManualDownloadRequired(ValueError):
    pass


@dataclass
class HostedFile:
    url: str
    format: str
    host: str
    filename: str = ''
    size: int | None = None
    sha256: str | None = None
    share_url: str = ''
    shortlink: str = ''


def public_get(url, max_bytes=2_000_000):
    # A fresh client has no thread-affine DB, while host locks/robots/backoff
    # remain process-wide in the existing generic HTTP infrastructure.
    response = HttpClient(user_agent='MediaArchiveImporter/0.3 (+public file resolution)').get(
        url, max_bytes=max_bytes, conditional=False)
    return response.body, response.url


def safe_url(url):
    parts = urllib.parse.urlsplit(str(url))
    if (parts.scheme != 'https' or not parts.hostname or parts.username
            or parts.password or parts.port not in (None, 443)):
        raise ValueError('File host URL must be public HTTPS without credentials')
    return parts


def extension(filename, allowed):
    ext = PurePosixPath(urllib.parse.unquote(filename)).suffix.casefold()
    ext = {'.zip': '.cbz' if '.cbz' in allowed else '.zip',
           '.rar': '.cbr' if '.cbr' in allowed else '.rar'}.get(ext, ext)
    return ext if ext in allowed else ''


class Pixeldrain:
    id = 'pixeldrain'
    domains = frozenset({'pixeldrain.com', 'www.pixeldrain.com'})

    def resolve(self, url, allowed):
        parts = safe_url(url)
        match = re.fullmatch(r'/(?:u|api/file)/([A-Za-z0-9_-]{4,80})(?:/info)?/?', parts.path)
        if not match:
            raise ManualDownloadRequired('Pixeldrain folders/lists require manual selection; open the source link')
        file_id = match.group(1)
        raw, final = public_get(f'https://pixeldrain.com/api/file/{file_id}/info')
        if safe_url(final).hostname not in self.domains:
            raise ValueError('Pixeldrain API redirected outside its host')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('success') is False:
            raise ManualDownloadRequired('Pixeldrain file is unavailable')
        if data.get('availability') or data.get('can_download') is False:
            raise ManualDownloadRequired('Pixeldrain download restricted: ' + str(
                data.get('availability') or 'manual account/session required'))
        name = str(data.get('name') or '')
        ext = extension(name, allowed)
        if not ext:
            return []
        size = data.get('size')
        checksum = data.get('hash_sha256')
        return [HostedFile(f'https://pixeldrain.com/api/file/{file_id}?download', ext,
                           self.id, name, size if isinstance(size, int) else None,
                           checksum if re.fullmatch(r'[a-fA-F0-9]{64}', str(checksum)) else None, url)]


class _DownloadButton(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('id') == 'downloadButton' and attrs.get('href'):
            self.urls.append(attrs['href'])


class MediaFire:
    id = 'mediafire'
    domains = frozenset({'mediafire.com', 'www.mediafire.com'})

    def resolve(self, url, allowed):
        parts = safe_url(url)
        if not parts.path.startswith(('/file/', '/download/')):
            raise ManualDownloadRequired('MediaFire folder requires manual file selection')
        raw, final = public_get(url)
        if safe_url(final).hostname not in self.domains:
            raise ValueError('MediaFire page redirected outside its host')
        parser = _DownloadButton()
        parser.feed(raw.decode('utf-8', 'replace'))
        if not parser.urls:
            raise ManualDownloadRequired('MediaFire has no public download button; open the source link')
        results = []
        for target in dict.fromkeys(parser.urls):
            target_parts = safe_url(urllib.parse.urljoin(final, target))
            if not (target_parts.hostname.endswith('.mediafire.com')
                    and target_parts.hostname.startswith('download')):
                raise ValueError('MediaFire download button points outside its download hosts')
            name = urllib.parse.unquote(target_parts.path.rsplit('/', 1)[-1])
            ext = extension(name, allowed)
            if ext:
                results.append(HostedFile(target_parts.geturl(), ext, self.id, name, share_url=url))
        return results


class Dropbox:
    id = 'dropbox'
    domains = frozenset({'dropbox.com', 'www.dropbox.com'})

    def resolve(self, url, allowed):
        parts = safe_url(url)
        if not parts.path.startswith(('/s/', '/scl/fi/')):
            raise ManualDownloadRequired('Dropbox link is not an individual shared file')
        name = urllib.parse.unquote(parts.path.rsplit('/', 1)[-1])
        ext = extension(name, allowed)
        if not ext:
            return []
        # Preserve rlkey and other shared-link parameters.
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                 if key not in ('dl', 'raw')]
        query.append(('dl', '1'))
        return [HostedFile(urllib.parse.urlunsplit(parts._replace(
            query=urllib.parse.urlencode(query), fragment='')), ext, self.id, name, share_url=url)]


FILE_HOSTS = {domain: adapter for adapter in (Pixeldrain(), MediaFire(), Dropbox())
              for domain in adapter.domains}
MANUAL_HOSTS = {
    'mega.nz': 'MEGA requires a legitimate client/session and download-limit handling',
    'mega.io': 'MEGA requires a legitimate client/session and download-limit handling',
    'wetransfer.com': 'WeTransfer requires an expiring transfer/session connector',
    'we.tl': 'WeTransfer requires an expiring transfer/session connector',
    'gofile.io': 'GoFile requires a legitimate authenticated/session connector',
    'terabox.com': 'Terabox requires a legitimate authenticated/session connector',
    '1024terabox.com': 'Terabox requires a legitimate authenticated/session connector',
    '1fichier.com': '1fichier requires host wait/session handling',
    'drive.google.com': 'Google Drive requires a legitimate sharing/session connector',
}


def _resolve_shortlink(url, allowed_formats):
    from .shortlinks import ShortlinkNeedsBrowser, resolve_shortlink
    try:
        target = resolve_shortlink(url)
    except ShortlinkNeedsBrowser as exc:
        raise ManualDownloadRequired(f'{exc}; use Resolve shortlink under Add a direct file on the Search tab') from exc
    hosted = resolve_hosted_file(target, allowed_formats, follow_shortlinks=False)
    if hosted is None:
        name = urllib.parse.unquote(urllib.parse.urlsplit(target).path.rsplit('/', 1)[-1])
        ext = extension(name, set(allowed_formats))
        if not ext:
            return []
        safe_url(target)
        hosted = [HostedFile(target, ext, 'direct', name, share_url=target)]
    for file in hosted:
        file.shortlink = url
    return hosted


def resolve_hosted_file(url, allowed_formats, follow_shortlinks=True):
    host = safe_url(url).hostname.casefold()
    if follow_shortlinks and host not in FILE_HOSTS:
        from .shortlinks import is_shortlink
        if is_shortlink(url):
            return _resolve_shortlink(url, allowed_formats)
    adapter = FILE_HOSTS.get(host)
    if adapter:
        return adapter.resolve(url, set(allowed_formats))
    manual = MANUAL_HOSTS.get(host.removeprefix('www.'))
    if manual:
        raise ManualDownloadRequired(manual + '; open the source link')
    return None  # Unrecognized host: caller may still handle a direct file.
