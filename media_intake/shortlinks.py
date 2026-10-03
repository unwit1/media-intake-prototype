"""Shortlink (ad-gate) resolution for direct downloads.

Shortener detection and the parameter-carrying rules come from the vendored
"Bypass All Shortlinks Debloated" userscript (data/shortlinks, see SOURCE.json).
Headless resolution only reads destinations a shortener already exposes: HTTP
redirects, URL parameters (plain or base64), meta refresh and literal script
redirects. Anything that needs page interaction is left to the user-operated
browser helper (embedded_browser.shortlink_main), which runs the userscript itself.
No CAPTCHA is solved, and the caller's source approval still governs downloads.
"""
import base64
import binascii
from functools import lru_cache
import html
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse

DATA = Path(__file__).resolve().parent / 'data' / 'shortlinks'
USERSCRIPT = 'Bypass_All_Shortlinks.user.js'
MAX_HOPS = 6

# The userscript matches these to apply optional or non-shortener tweaks; links
# on them are ordinary pages, not ad gates, so never treat them as shortlinks.
NOT_SHORTLINKS = frozenset({
    'google.com', 'recaptcha.net', 'youtube.com', 'youtube-nocookie.com', 't.me', 'vk.com',
    'facebook.com', 'instagram.com', 'tiktok.com', 'reddit.com', 'x.com', 'twitter.com',
    'github.com', 'flickr.com',
})
# Parameters the userscript's declarative rules read, plus common redirect keys.
DESTINATION_PARAMS = ('url', 'urls', 'link', 'linkAlias', 'dest', 'destination', 'target', 'to',
                      'go', 'r', 'redirect', 'redir', 'safe', 'id', 's', 'u', 'q', 'href', 'out')
_META_REFRESH = re.compile(
    r'<meta[^>]+http-equiv\s*=\s*["\']?refresh["\']?[^>]*content\s*=\s*["\'][^"\']*?url\s*=\s*([^"\'>\s]+)',
    re.I)
_SCRIPT_REDIRECT = re.compile(
    r'(?:window\.|document\.|top\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*["\'](https?://[^"\']+)["\']',
    re.I)


class ShortlinkNeedsBrowser(ValueError):
    """The destination is only revealed after page interaction (timers, buttons, CAPTCHA)."""


@lru_cache(maxsize=1)
def _rules():
    """Compile the userscript's own @include regexes and @match host patterns."""
    includes, hosts = [], set()
    header = (DATA / USERSCRIPT).read_text(encoding='utf-8').split('// ==/UserScript==', 1)[0]
    for kind, rule in re.findall(r'^// @(include|match)\s+(\S+)\s*$', header, re.M):
        if kind == 'include' and rule.startswith('/') and rule.rfind('/') > 0:
            try:
                includes.append(re.compile(rule[1:rule.rindex('/')], re.I))
            except re.error:
                continue
            continue
        match = re.fullmatch(r'(?:\*|https?)://(?:\*\.)?([^/*]+)/.*', rule)
        if match:  # "*://*/path" patterns have no host and are skipped.
            hosts.add(match.group(1).casefold())
    return tuple(includes), frozenset(hosts)


def source_info():
    return json.loads((DATA / 'SOURCE.json').read_text(encoding='utf-8'))


def _host_in(host, domains):
    parts = host.split('.')
    return any('.'.join(parts[index:]) in domains for index in range(len(parts) - 1))


def is_shortlink(url):
    parts = urllib.parse.urlsplit(str(url))
    host = (parts.hostname or '').casefold()
    if parts.scheme not in ('http', 'https') or not host or _host_in(host, NOT_SHORTLINKS):
        return False
    from .file_hosts import FILE_HOSTS, MANUAL_HOSTS
    # The userscript also matches file hosts (and their CDN subdomains) for its
    # auto-download extras; those are files, not ad gates.
    if _host_in(host, set(FILE_HOSTS) | set(MANUAL_HOSTS)):
        return False
    includes, hosts = _rules()
    if _host_in(host, hosts):
        return True
    target = f'https://{host}{parts.path or "/"}'
    return any(rule.search(target) for rule in includes)


def _as_url(value):
    value = str(value or '').strip()
    for _ in range(2):
        if re.match(r'https?://', value, re.I):
            return value
        if re.match(r'https?%3A', value, re.I):
            value = urllib.parse.unquote(value)
            continue
        break
    # Base64 (standard, URL-safe, unpadded, or reversed), as several gates use.
    for candidate in (value, value[::-1]):
        if len(candidate) < 12 or not re.fullmatch(r'[A-Za-z0-9+/_-]+=*', candidate):
            continue
        try:
            decoded = base64.urlsafe_b64decode(
                candidate.replace('+', '-').replace('/', '_') + '=' * (-len(candidate) % 4)
            ).decode('utf-8')
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        decoded = urllib.parse.unquote(decoded.strip()) if re.match(r'https?%3A', decoded, re.I) else decoded.strip()
        if re.match(r'https?://[^\s]+$', decoded, re.I):
            return decoded
    return None


def destination_from_url(url):
    """Return a destination carried in the shortlink URL itself, if any."""
    parts = urllib.parse.urlsplit(str(url))
    raw = parts.query
    # The userscript's "url+N" rules: a destination appended after "&url=".
    if '&url=' in raw:
        found = _as_url(urllib.parse.unquote(raw.split('&url=', 1)[1]))
        if found:
            return found
    params = urllib.parse.parse_qs(raw, keep_blank_values=False)
    fragment = urllib.parse.parse_qs(parts.fragment, keep_blank_values=False)
    for key in DESTINATION_PARAMS:
        for value in params.get(key, []) + fragment.get(key, []):
            found = _as_url(value)
            if found and urllib.parse.urlsplit(found).hostname != parts.hostname:
                return found
    return None


def destination_from_html(body, base_url):
    text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body)
    for pattern in (_META_REFRESH, _SCRIPT_REDIRECT):
        for match in pattern.finditer(text):
            target = urllib.parse.urljoin(base_url, html.unescape(match.group(1)).strip('\'"'))
            if urllib.parse.urlsplit(target).hostname not in (None, urllib.parse.urlsplit(base_url).hostname):
                return target
    return None


def _client():
    from .webcomics.http import HttpClient
    return HttpClient(user_agent='MediaArchiveImporter/0.3 (+shortlink resolution)', retries=0)


def resolve_shortlink(url, client=None, max_hops=MAX_HOPS):
    """Follow a shortlink to the first non-shortlink destination, without interaction."""
    from .webcomics.http import ExtractionError, SourceUnavailable
    client = client or _client()
    current, seen = str(url), set()
    for _ in range(max_hops):
        if not is_shortlink(current):
            return current
        if current in seen:
            raise ShortlinkNeedsBrowser('Shortlink redirect loop')
        seen.add(current)
        target = destination_from_url(current)
        if target:
            current = target
            continue
        try:
            response = client.get(current, max_bytes=2_000_000, conditional=False)
        except SourceUnavailable as exc:
            reason = 'robots.txt disallows automated access' if exc.status == 403 else f'HTTP {exc.status or "error"}'
            raise ShortlinkNeedsBrowser(f'Shortlink {urllib.parse.urlsplit(current).hostname}: {reason}') from exc
        except (ExtractionError, urllib.error.URLError, OSError) as exc:
            raise ShortlinkNeedsBrowser(f'Shortlink {urllib.parse.urlsplit(current).hostname}: {exc}') from exc
        if response.url != current and not is_shortlink(response.url):
            return response.url
        target = destination_from_url(response.url) or destination_from_html(response.body, response.url)
        if not target:
            raise ShortlinkNeedsBrowser(
                f'Shortlink {urllib.parse.urlsplit(current).hostname} needs page interaction')
        current = target
    if is_shortlink(current):
        raise ShortlinkNeedsBrowser('Shortlink exceeded its redirect budget')
    return current
