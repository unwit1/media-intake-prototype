"""Shared plumbing for the source connectors: specs, pages, HTTP and readable errors.

Every connector answers a search with a ``SearchPage`` of normalized rows. Failures
become ``SourceError`` with a message written for someone who does not know how the
site works, and a ``kind`` the GUI and source health can act on.
"""
from dataclasses import dataclass, field
import gzip
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib


USER_AGENT = 'Mozilla/5.0 (compatible; MediaIntake/0.4; +personal library intake)'
MAX_RESPONSE_BYTES = 6_000_000


class SourceError(ValueError):
    """A connector failure with a plain-language explanation.

    kind is one of: unreachable, blocked, auth, limit, invalid, unsupported, config.
    """

    def __init__(self, source, message, kind='invalid', hint=''):
        self.source, self.kind, self.hint = source, kind, hint
        text = f'{source}: {message}'
        if hint:
            text += f' {hint}'
        super().__init__(text)


@dataclass
class ConnectorSpec:
    key: str                 # rights-evidence / source-health key
    label: str               # shown to people
    role: str                # torrent | usenet | direct | metadata | browser
    categories: frozenset
    summary: str
    default_base: str = ''
    settings: tuple = ()     # (setting name, label, secret?) the connector reads
    jackett_indexer: str = ''
    pages: bool = True       # supports "Load more"
    needs: tuple = ()        # settings that must be filled before searching

    @property
    def file_capable(self):
        return self.role in ('torrent', 'usenet', 'direct')


@dataclass
class SearchPage:
    rows: list = field(default_factory=list)
    page: int = 1
    has_more: bool = False
    via: str = ''            # 'direct' or 'jackett'
    notice: str = ''         # non-fatal note for the status bar


def clean_base(source, value, default):
    """Validate a configurable site address: HTTPS anywhere, plain HTTP only on this machine/LAN."""
    raw = str(value or default or '').strip().rstrip('/')
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise SourceError(source, f'the site address "{raw}" is not a valid web address.', 'config',
                          'Fix it in Settings → Sources.')
    if parts.scheme == 'http' and not _local_host(parts.hostname):
        raise SourceError(source, 'only https:// addresses are allowed for internet sites.', 'config',
                          'Fix it in Settings → Sources.')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))


def _local_host(host):
    if host in ('localhost',):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _decode(body, encoding):
    encoding = (encoding or '').casefold()
    if encoding == 'gzip' or body[:2] == b'\x1f\x8b':
        return gzip.decompress(body)
    if encoding == 'deflate':
        return zlib.decompress(body)
    return body


def looks_like_challenge(body):
    head = body[:6000].lower()
    return any(marker in head for marker in (
        b'cf-browser-verification', b'challenge-platform', b'just a moment...',
        b'attention required! | cloudflare', b'ddos-guard', b'cf_chl_opt'))


def _urlopen(req, timeout):
    # Unlike core.open_url this keeps an error reply readable, so a browser-check page
    # behind a 403/503 can be recognised.
    return urllib.request.urlopen(req, timeout=timeout)


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # hand the 3xx reply (and its Set-Cookie headers) back to the caller


_NO_REDIRECT = urllib.request.build_opener(_KeepRedirect())


def request(source, url, *, data=None, headers=None, timeout=25, max_bytes=MAX_RESPONSE_BYTES,
            opener=None, method=None, follow_redirects=True):
    """Fetch a page or API response, turning network and HTTP failures into SourceError.

    Returns (body bytes, final url, response headers). With follow_redirects=False a
    3xx reply is returned as-is, which sign-in forms need to keep their cookies.
    """
    if not follow_redirects and opener is None:
        opener = lambda req, timeout: _NO_REDIRECT.open(req, timeout=timeout)
    base_headers = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'gzip',
                    'Accept': '*/*', 'Accept-Language': 'en;q=0.9,*;q=0.5'}
    base_headers.update(headers or {})
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode('utf-8')
        base_headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
    req = urllib.request.Request(url, data=data, headers=base_headers, method=method)
    try:
        with (opener or _urlopen)(req, timeout=timeout) as reply:
            body = reply.read(max_bytes + 1)
            final_url = reply.geturl()
            reply_headers = reply.headers
    except urllib.error.HTTPError as exc:
        body = b''
        try:
            body = _decode(exc.read(20000) or b'', exc.headers.get('Content-Encoding'))
        except Exception:
            pass
        finally:
            exc.close()
        if not follow_redirects and 300 <= exc.code < 400:
            return body, url, exc.headers
        raise http_error(source, exc.code, body) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise network_error(source, exc) from exc
    if len(body) > max_bytes:
        raise SourceError(source, 'the site sent a much larger reply than expected.', 'invalid')
    try:
        body = _decode(body, reply_headers.get('Content-Encoding'))
    except (OSError, zlib.error) as exc:
        raise SourceError(source, 'the site sent a damaged reply.', 'invalid', 'Try again later.') from exc
    if looks_like_challenge(body):
        raise blocked_error(source)
    return body, final_url, reply_headers


def blocked_error(source):
    return SourceError(source, 'the site is showing a browser check (Cloudflare or similar), so the app cannot '
                       'read its results.', 'blocked',
                       'Set this source to search through Jackett in Settings → Sources, or open the site in '
                       'your browser.')


def http_error(source, status, body=b''):
    if looks_like_challenge(body):
        return blocked_error(source)
    if status in (401, 403):
        return SourceError(source, f'the site refused the request (HTTP {status}).', 'auth',
                           'Check the login or API key in Settings → Sources. If they are right, the site may '
                           'be blocking automated access; try routing it through Jackett.')
    if status == 404:
        return SourceError(source, 'the search address was not found (HTTP 404).', 'unreachable',
                           'The site may have moved. Update its address in Settings → Sources.')
    if status == 429:
        return SourceError(source, 'the site says too many requests were made (HTTP 429).', 'limit',
                           'Wait a few minutes before searching it again.')
    if 500 <= status < 600:
        return SourceError(source, f'the site is having problems right now (HTTP {status}).', 'unreachable',
                           'Try again later; other sources are unaffected.')
    return SourceError(source, f'the site answered with HTTP {status}.', 'invalid')


def network_error(source, exc):
    reason = getattr(exc, 'reason', exc)
    text = str(reason).casefold()
    if isinstance(reason, socket.gaierror) or 'name or service not known' in text or 'getaddrinfo' in text \
            or 'nodename nor servname' in text or 'no address associated' in text:
        return SourceError(source, 'the site address could not be found.', 'unreachable',
                           'It may be down, renamed, or blocked by your internet provider. Set a working '
                           'mirror address in Settings → Sources.')
    if isinstance(reason, (socket.timeout, TimeoutError)) or 'timed out' in text:
        return SourceError(source, 'the site did not answer in time.', 'unreachable',
                           'It may be overloaded or blocked on your network. Try again later.')
    if 'certificate' in text or 'ssl' in text:
        return SourceError(source, 'the site\'s secure connection could not be verified.', 'unreachable',
                           'The address may be an impostor or intercepted. Check it in Settings → Sources.')
    if 'refused' in text:
        return SourceError(source, 'the connection was refused.', 'unreachable',
                           'If this is a program on your computer (Jackett, SABnzbd), make sure it is running.')
    return SourceError(source, f'the site could not be reached ({reason}).', 'unreachable',
                       'Check your internet connection or try again later.')


def json_body(source, body):
    try:
        return json.loads(body.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SourceError(source, 'the site sent a reply the app could not read (expected JSON).', 'invalid',
                          'The site may have changed or be showing an error page.') from exc


def human_size(value):
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return ''


def parse_size(text):
    """'1.4 GiB', '700 MB', '1,2 GB' → bytes."""
    import re
    match = re.search(r'([\d.,]+)\s*([KMGT]?i?B)\b', str(text or ''), re.I)
    if not match:
        return None
    number = match.group(1).replace(',', '.') if match.group(1).count(',') == 1 and '.' not in match.group(1) \
        else match.group(1).replace(',', '')
    try:
        value = float(number)
    except ValueError:
        return None
    unit = match.group(2).upper().replace('I', '')
    power = {'B': 0, 'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4}.get(unit, 0)
    return int(value * 1024 ** power)
