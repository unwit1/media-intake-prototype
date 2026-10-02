"""GetComics release discovery and a separately configured comic mirror."""
import html
from html.parser import HTMLParser
import re
import urllib.parse
import urllib.request

from .core import validate_item
from .file_hosts import FILE_HOSTS, MANUAL_HOSTS, public_get as http_get, resolve_hosted_file
from .webcomics.http import HttpClient

GETCOMICS_HOSTS = {'getcomics.info', 'www.getcomics.info', 'getcomics.org', 'www.getcomics.org'}
_COMIC_FILE_EXTENSIONS = {'.cbz', '.pdf'}


def _safe_base(base_url, reject_getcomics=False):
    parts = urllib.parse.urlsplit(str(base_url or '').strip())
    local_http = parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1', '::1')
    if (parts.scheme != 'https' and not local_http) or not parts.hostname or parts.username or parts.password:
        raise ValueError('Comic mirror base must be HTTPS and contain no credentials')
    host = parts.hostname.casefold()
    if reject_getcomics and (host in GETCOMICS_HOSTS or any(host.endswith('.' + item) for item in GETCOMICS_HOSTS)):
        raise ValueError('Configure your separate public-domain mirror; GetComics is discovery-only')
    return parts


class _PostLinks(HTMLParser):
    """Collect linked post headings from simple WordPress/GetComics layouts."""
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links = []
        self.in_heading = False
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h1', 'h2', 'h3'):
            self.in_heading = True
        if self.in_heading and tag == 'a':
            href = urllib.parse.urljoin(self.base_url, attrs.get('href', ''))
            self.current = {'url': href, 'text': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = ' '.join(self.current['text'].split())
            self.links.append(self.current)
            self.current = None
        if tag in ('h1', 'h2', 'h3'):
            self.in_heading = False


class _ReleasePage(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.links = []
        self.text = []
        self.heading = []
        self.in_heading = False
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h1', 'h2'):
            self.in_heading = True
        if tag == 'a':
            self.current_link = {
                'href': urllib.parse.urljoin(self.page_url, attrs.get('href', '')),
                'text': '',
                'title': attrs.get('title', ''),
            }

    def handle_data(self, data):
        self.text.append(data)
        if self.in_heading:
            self.heading.append(data)
        if self.current_link is not None:
            self.current_link['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current_link is not None:
            self.current_link['text'] = ' '.join(self.current_link['text'].split())
            self.links.append(self.current_link)
            self.current_link = None
        if tag in ('h1', 'h2'):
            self.in_heading = False


def _same_origin(url, origin):
    candidate = urllib.parse.urlsplit(url)
    return (candidate.scheme, candidate.hostname, candidate.port) == (
        origin.scheme, origin.hostname, origin.port)


def parse_getcomics_results(content, page_url, limit=20):
    """Return GetComics post references without following any file-host links."""
    page = urllib.parse.urlsplit(page_url)
    if page.scheme != 'https' or page.hostname not in GETCOMICS_HOSTS:
        raise ValueError('GetComics result page must be on the official site')
    parser = _PostLinks(page_url)
    parser.feed(content)
    found, seen = [], set()
    for link in parser.links:
        parts = urllib.parse.urlsplit(link['url'])
        if (parts.scheme != 'https' or parts.hostname not in GETCOMICS_HOSTS
                or not link['text'] or link['url'] in seen):
            continue
        # Skip category/navigation headings and keep only post-like permalinks.
        if (parts.path.rstrip('/') in ('', '/category', '/tag', '/page')
                or any(segment in parts.path.casefold() for segment in ('/category/', '/tag/', '/author/', '/page/'))):
            continue
        seen.add(link['url'])
        found.append({
            'provider': 'GetComics', 'title': link['text'], 'author': 'Unknown',
            'url': link['url'], 'source': link['url'], 'format': 'link',
            'discovery_only': True,
        })
        if len(found) >= max(1, min(int(limit), 50)):
            break
    return found


def search_getcomics(query, limit=20):
    """Search paginated GetComics results as discovery metadata only."""
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    results, seen = [], set()
    max_results = max(1, min(int(limit), 50))
    # This follows GetComics Downloader's paged search route (/page/N/?s=...)
    # while keeping all page parsing local to this app.
    for page_number in range(1, 6):
        url = 'https://getcomics.org/page/' + str(page_number) + '/?' + urllib.parse.urlencode({'s': query})
        try:
            raw, final = http_get(url, max_bytes=5_000_000)
        except OSError as exc:
            if results and getattr(exc, 'status', getattr(exc, 'code', None)) == 404:
                break
            raise
        if urllib.parse.urlsplit(final).hostname not in GETCOMICS_HOSTS:
            raise ValueError('GetComics search redirected away from its official site')
        page_results = parse_getcomics_results(raw.decode('utf-8', 'replace'), final, max_results)
        added = 0
        for item in page_results:
            if item['url'] in seen:
                continue
            seen.add(item['url'])
            results.append(item)
            added += 1
            if len(results) >= max_results:
                return results
        if not added:
            break
    return results


_FILE_EXTENSIONS = {'.cbz', '.cbr', '.pdf'}
_MIME_EXTENSIONS = {
    'application/zip': '.cbz', 'application/x-cbz': '.cbz',
    'application/vnd.rar': '.cbr', 'application/x-rar-compressed': '.cbr',
    'application/pdf': '.pdf',
}


def _head_download_target(url):
    """Follow a GetComics server redirect without downloading its response body."""
    def share_host(target):
        host = (urllib.parse.urlsplit(target).hostname or '').casefold()
        return host in FILE_HOSTS or host.removeprefix('www.') in MANUAL_HOSTS
    response = HttpClient(user_agent='MediaArchiveImporter/0.3 (+public file resolution)').head(url, stop_at=share_host)
    content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().casefold()
    return response.url, content_type, response.headers.get('content-disposition', '')


def _download_extension(url, content_type='', disposition=''):
    path = urllib.parse.urlsplit(url).path
    ext = '.' + path.rsplit('.', 1)[-1].casefold() if '.' in path.rsplit('/', 1)[-1] else ''
    if ext == '.zip':
        return '.cbz'
    if ext in _FILE_EXTENSIONS:
        return ext
    name = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition, flags=re.I)
    if name:
        name_ext = '.' + urllib.parse.unquote(name.group(1).strip().strip('"')).rsplit('.', 1)[-1].casefold()
        if name_ext == '.zip':
            return '.cbz'
        if name_ext == '.rar':
            return '.cbr'
        if name_ext in _FILE_EXTENSIONS:
            return name_ext
    if content_type in _MIME_EXTENSIONS:
        return _MIME_EXTENSIONS[content_type]
    return ''


def resolve_getcomics_post(item, approval, limit=20):
    """Find direct comic files from a release page after explicit local YES."""
    if not isinstance(approval, dict) or approval.get('approved_for_download') is not True:
        return []
    post_url = str(item.get('url') or '')
    parts = urllib.parse.urlsplit(post_url)
    if parts.scheme != 'https' or parts.hostname not in GETCOMICS_HOSTS:
        raise ValueError('GetComics post must use its official HTTPS host')
    raw, final = http_get(post_url, max_bytes=5_000_000)
    if urllib.parse.urlsplit(final).hostname not in GETCOMICS_HOSTS:
        raise ValueError('GetComics post redirected away from its official site')
    parser = _ReleasePage(final)
    parser.feed(raw.decode('utf-8', 'replace'))
    results, seen, resolved_urls = [], set(), set()
    item['connector_warnings'] = []
    max_results = max(1, min(int(limit), 50))
    def add_hosted(files):
        for file in files:
            if file.url in resolved_urls:
                continue
            resolved_urls.add(file.url)
            results.append(validate_item({
                'provider': 'GetComics', 'source_key': 'getcomics.org',
                'title': item.get('title') or 'Comic release',
                'author': item.get('author') or 'Unknown', 'url': file.url,
                'source': final, 'format': file.format,
                'release_identity': final,
                'file_host': file.host, 'file_host_share_url': file.share_url,
                'source_filename': file.filename, 'expected_size': file.size,
                'expected_sha256': file.sha256,
                **({'rights_work_key': approval['rights_work_key']}
                   if approval.get('rights_work_key') else {}),
                'rights_approval': 'rights-evidence.json approved_for_download: true',
            }))
            if len(results) >= max_results:
                return True
        return False
    for link in parser.links:
        candidate_url = link['href']
        file_parts = urllib.parse.urlsplit(candidate_url)
        label = (link.get('text', '') + ' ' + link.get('title', '')).casefold()
        ext = _download_extension(candidate_url)
        server_redirect = (
            file_parts.hostname in GETCOMICS_HOSTS
            and any(file_parts.path.casefold().startswith(prefix)
                    for prefix in ('/download', '/dlds/', '/dls/'))
        )
        download_action = any(term in label for term in ('download now', 'main server'))
        if (file_parts.scheme != 'https' or not file_parts.hostname or file_parts.username
                or file_parts.password or candidate_url in seen):
            continue
        seen.add(candidate_url)
        try:
            hosted = resolve_hosted_file(candidate_url, _FILE_EXTENSIONS)
        except (ValueError, OSError) as exc:
            item['connector_warnings'].append(str(exc))
            continue
        if hosted is not None:
            if add_hosted(hosted):
                return results
            continue
        content_type = disposition = ''
        file_url = candidate_url
        if not ext and (server_redirect or download_action):
            try:
                file_url, content_type, disposition = _head_download_target(candidate_url)
            except Exception as exc:
                # MediaFire and other browser-oriented hosts may not expose
                # a direct file to HEAD. Like the referenced CLI, leave those
                # pages for a user to open instead of treating HTML as a file.
                item['connector_warnings'].append(f'Download link unavailable: {exc}')
                continue
            target = urllib.parse.urlsplit(file_url)
            if (target.scheme not in ('https', 'http') or not target.hostname
                    or target.username or target.password):
                continue
            ext = _download_extension(file_url, content_type, disposition)
            if target.scheme == 'https':
                try:
                    hosted = resolve_hosted_file(file_url, _FILE_EXTENSIONS)
                except (ValueError, OSError) as exc:
                    item['connector_warnings'].append(str(exc))
                    continue
                if hosted is not None:
                    if add_hosted(hosted):
                        return results
                    continue
        if not ext:
            continue
        if file_url in resolved_urls:
            continue
        resolved_urls.add(file_url)
        results.append(validate_item({
            'provider': 'GetComics', 'source_key': 'getcomics.org',
            'title': item.get('title') or 'Comic release', 'author': item.get('author') or 'Unknown',
            'url': file_url, 'source': final, 'format': ext,
            'release_identity': final,
            **({'rights_work_key': approval['rights_work_key']}
               if approval.get('rights_work_key') else {}),
            'rights_approval': 'rights-evidence.json approved_for_download: true',
        }))
        if len(results) >= max(1, min(int(limit), 50)):
            break
    return results


def parse_public_domain_release(content, page_url, mirror_base, manual_rights=None):
    """Extract same-host comic files with a page marker or explicit local YES."""
    origin = _safe_base(mirror_base, reject_getcomics=True)
    page = urllib.parse.urlsplit(page_url)
    if not _same_origin(page_url, origin):
        raise ValueError('Release page must be hosted on the configured public-domain mirror')
    parser = _ReleasePage(page_url)
    parser.feed(content)
    visible_text = html.unescape(' '.join(parser.text)).casefold()
    page_marked = bool(re.search(
        r'\bpublic\s+domain\b|\bcc0\b|creativecommons\.org/publicdomain', visible_text))
    local_approval = _usable_manual_approval(manual_rights)
    if manual_rights is not None and not local_approval:
        return []
    if manual_rights is None and not page_marked:
        return []
    return _parse_release_links(parser, page_url, page_marked, local_approval)


def search_public_domain_comic_mirror(query, base_url, limit=20, manual_rights=None):
    """Search a configured mirror and resolve same-host files if its local approval is YES."""
    if manual_rights is not None and not _usable_manual_approval(manual_rights):
        return []
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    origin = _safe_base(base_url, reject_getcomics=True)
    base = urllib.parse.urlunsplit((origin.scheme, origin.netloc, origin.path.rstrip('/'), '', ''))
    search_url = base.rstrip('/') + '/?' + urllib.parse.urlencode({'s': query})
    raw, final = http_get(search_url, max_bytes=5_000_000)
    if not _same_origin(final, origin):
        raise ValueError('Comic mirror search redirected outside the configured host')
    parser = _PostLinks(final)
    parser.feed(raw.decode('utf-8', 'replace'))
    posts, seen = [], set()
    for link in parser.links:
        if not link['text'] or not _same_origin(link['url'], origin) or link['url'] in seen:
            continue
        seen.add(link['url'])
        posts.append(link)
        if len(posts) >= max(1, min(int(limit), 20)):
            break
    results = []
    for post in posts:
        try:
            detail, detail_url = http_get(post['url'], max_bytes=5_000_000)
            if not _same_origin(detail_url, origin):
                continue
            results.extend(parse_public_domain_release(
                detail.decode('utf-8', 'replace'), detail_url, base, manual_rights))
        except (OSError, ValueError):
            continue
    return results


def _usable_manual_approval(rights):
    if not isinstance(rights, dict) or rights.get('approved_for_download') is not True:
        return None
    return rights


def _parse_release_links(parser, page_url, page_marked, local_approval):
    title = ' '.join(' '.join(parser.heading).split()) or 'Public-domain comic'
    results, seen = [], set()
    origin = urllib.parse.urlsplit(page_url)
    for link in parser.links:
        url = link['href']
        parts = urllib.parse.urlsplit(url)
        suffix = parts.path.rsplit('.', 1)[-1].casefold() if '.' in parts.path else ''
        ext = '.' + suffix
        if (ext not in _COMIC_FILE_EXTENSIONS or not _same_origin(url, origin)
                or url in seen or parts.username or parts.password):
            continue
        seen.add(url)
        if page_marked:
            rights_label = 'Public Domain marker on mirror page'
        elif local_approval.get('status') == 'public_domain':
            rights_label = 'Locally approved as public domain'
        else:
            rights_label = 'Locally approved for download'
        results.append(validate_item({
            'title': title, 'author': 'Unknown', 'url': url,
            'format': ext, 'source': page_url, 'site': 'public_domain_comic_mirror',
            'rights': rights_label,
        }))
    return results
