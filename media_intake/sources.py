"""Source discovery adapters. A result page is metadata until a file URL is resolved."""
import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
import urllib.error
import html as html_module
from html.parser import HTMLParser
from pathlib import Path
from .book_identifiers import collect_isbns
from .core import EXTENSIONS, http_get, validate_item

SITE_PROFILES = {
    'annas': {'base': 'https://annas-archive.pk', 'path': '/search', 'query': 'q', 'detail': r'/md5/[a-f0-9]{32}(?:\b|/)'},
    'libgen': {'base': 'https://libgen.gl', 'path': '/index.php', 'query': 'req', 'detail': r'(?:/ads\.php\?md5=|book/index\.php\?md5=|/main/)[a-f0-9]{32}'},
}
SNAHP_FORUM = 'https://fora.snahp.eu'
_PHPBB_TOPIC_PATH = re.compile(r'^/viewtopic\.php$', re.I)


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current = {'href': dict(attrs).get('href', ''), 'text': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = ' '.join(self.current['text'].split())
            self.links.append(self.current)
            self.current = None


class ForumThreadAnchors(HTMLParser):
    """Read only same-forum thread links from a forum search result page."""
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.host = urllib.parse.urlsplit(base_url).hostname
        self.current = None
        self.threads = []
        self.seen = set()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get('class', '').split())
        title_classes = {'topictitle'}
        in_title = bool(classes & title_classes) or any(parent & title_classes for _, parent in self.stack)
        if tag == 'a' and in_title:
            href = urllib.parse.urljoin(SNAHP_FORUM + '/', attrs.get('href', ''))
            parts = urllib.parse.urlsplit(href)
            topic_ids = urllib.parse.parse_qs(parts.query).get('t', [])
            topic_id = topic_ids[0] if topic_ids else ''
            if (parts.scheme == 'https' and parts.hostname == self.host and parts.port in (None, 443)
                    and not parts.username and not parts.password and _PHPBB_TOPIC_PATH.match(parts.path)
                    and re.fullmatch(r'\d{1,12}', topic_id)):
                # Keep only the topic ID; drop session IDs, post anchors, and tracking.
                canonical = urllib.parse.urlunsplit((parts.scheme, parts.netloc, '/viewtopic.php',
                                                     urllib.parse.urlencode({'t': topic_id}), ''))
                self.current = {'url': canonical, 'title_attr': attrs.get('title', '').strip(), 'text': ''}
        self.stack.append((tag, classes))

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += (' ' if self.current['text'] else '') + data.strip()

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            url = self.current['url']
            title = ' '.join((self.current['text'] or self.current['title_attr']).split())
            if url not in self.seen:
                self.seen.add(url)
                self.threads.append({
                    'site': 'snahp_forum', 'title': title or 'Untitled thread',
                    'author': 'Forum thread', 'format': 'thread', 'source': url,
                    'url': url, 'forum_thread': True,
                })
            self.current = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class _SameForumRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hostname):
        super().__init__()
        self.hostname = hostname

    def redirect_request(self, request, fp, code, message, headers, new_url):
        target = urllib.parse.urlsplit(new_url)
        if (target.scheme != 'https' or target.hostname != self.hostname
                or target.port not in (None, 443)):
            return None
        return super().redirect_request(request, fp, code, message, headers, new_url)


def search_url(site, query, base=None):
    profile = SITE_PROFILES[site]
    host = (base or profile['base']).rstrip('/')
    if urllib.parse.urlsplit(host).scheme != 'https':
        raise ValueError('Site base must use HTTPS')
    return host + profile['path'] + '?' + urllib.parse.urlencode({profile['query']: query})


def parse_site_results(site, html, page_url):
    if site == 'libgen':
        rows = parse_libgen_rows(html, page_url)
        if rows: return rows
    parser = Anchors(); parser.feed(html)
    profile = SITE_PROFILES[site]
    seen, results = set(), []
    for link in parser.links:
        url = urllib.parse.urljoin(page_url, link['href'])
        if not re.search(profile['detail'], url, re.I) or url in seen:
            continue
        seen.add(url)
        results.append({'site': site, 'title': link['text'] or 'Untitled result', 'detail_url': url, 'source': page_url})
    return results


def parse_libgen_rows(page, page_url):
    """Match an edition title with its file link within each result row."""
    found, seen = [], set()
    for row in re.findall(r'<tr\b[^>]*>.*?</tr>', page, flags=re.I | re.S):
        parser = Anchors(); parser.feed(row)
        title_link = next((a for a in parser.links if 'edition.php?id=' in a['href'] and a['text']), None)
        file_link = next((a for a in parser.links if re.search(r'ads\.php\?md5=[a-f0-9]{32}', a['href'], re.I)), None)
        if not title_link or not file_link: continue
        detail = urllib.parse.urljoin(page_url, file_link['href'])
        if detail in seen: continue
        seen.add(detail)
        raw_cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row, flags=re.I | re.S)
        cells = [html_module.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', cell))).strip() for cell in raw_cells]
        title_cell = next((i for i, cell in enumerate(raw_cells) if 'edition.php?id=' in cell), -1)
        author = cells[title_cell + 1].strip(' ,') if 0 <= title_cell < len(cells) - 1 else 'Unknown'
        fmt = re.search(r'<td\b[^>]*>\s*(epub|pdf|mobi|azw3)\s*</td>', row, flags=re.I)
        isbn = re.search(r'\b97[89][0-9]{10}\b', row)
        result = {'site':'libgen', 'title':title_link['text'].strip(), 'detail_url':detail,
                  'source':page_url, 'author': author or 'Unknown'}
        if fmt: result['format'] = '.' + fmt.group(1).lower()
        if isbn: result['isbn'] = isbn.group(0)
        found.append(result)
    return found


def search_site(site, query, base=None, html_path=None):
    url = search_url(site, query, base)
    if html_path:
        html = Path(html_path).read_text(encoding='utf-8')
    else:
        raw, url = http_get(url)
        html = raw.decode('utf-8', errors='replace')
        if 'site unavailable' in html.casefold():
            raise ValueError('Source returned Site Unavailable')
    return parse_site_results(site, html, url)


def search_forum_threads(query, base_url=SNAHP_FORUM, html_path=None):
    """Search a phpBB forum and return only same-site topic records.

    Search result snippets may contain file-host links; this adapter deliberately
    discards every URL except canonical `viewtopic.php?t=<id>` topic links.
    """
    query = str(query or '').strip()
    if not query:
        return []
    base = urllib.parse.urlsplit(base_url)
    if (base.scheme != 'https' or base.hostname != 'fora.snahp.eu'
            or base.port not in (None, 443) or base.username or base.password):
        raise ValueError('Forum base must be https://fora.snahp.eu')
    search_url = urllib.parse.urljoin(base_url.rstrip('/') + '/', 'search.php?' + urllib.parse.urlencode({'keywords': query}))
    if html_path:
        saved_page = Path(html_path)
        if not saved_page.is_file() or saved_page.stat().st_size > 5_000_000:
            raise ValueError('Saved forum search HTML must be a file smaller than 5 MB')
        page = saved_page.read_text(encoding='utf-8', errors='replace')
        final_url = search_url
    else:
        headers = {'User-Agent': 'MediaIntakePrototype/0.1 (thread search)',
                   'Accept': 'text/html,application/xhtml+xml'}
        request = urllib.request.Request(search_url, headers=headers)
        opener = urllib.request.build_opener(_SameForumRedirect(base.hostname))
        try:
            with opener.open(request, timeout=30) as response:
                final_url = response.geturl()
                page = response.read(5_000_001).decode('utf-8', errors='replace')
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403):
                raise ValueError(f'Forum search returned HTTP {status}; use a normal browser and load a saved results page') from exc
            raise
        if len(page) > 5_000_000:
            raise ValueError('Forum search response exceeds 5 MB')
    final = urllib.parse.urlsplit(final_url)
    if final.scheme != 'https' or final.hostname != base.hostname or final.port not in (None, 443):
        raise ValueError('Forum search redirected outside fora.snahp.eu')
    parser = ForumThreadAnchors(base_url)
    parser.feed(page)
    if not parser.threads and 'not permitted to use the search system' in page.casefold():
        raise ValueError('Forum search is restricted to signed-in members; open the forum in your browser and import saved results')
    return parser.threads


def resolve_detail(site, page_url, title, author='Unknown', ext=None, html_path=None):
    if site not in SITE_PROFILES and site != 'generic':
        raise ValueError('Unknown site')
    raw, final = (None, page_url) if html_path else http_get(page_url)
    html = Path(html_path).read_text(encoding='utf-8') if html_path else raw.decode('utf-8', errors='replace')
    parser = Anchors(); parser.feed(html)
    results, seen = [], set()
    for link in parser.links:
        url = urllib.parse.urljoin(final, link['href'])
        parsed = urllib.parse.urlsplit(url)
        found_ext = Path(parsed.path).suffix.lower()
        if found_ext not in EXTENSIONS:
            # A site may use an opaque download route. The caller supplies its format.
            if ext not in EXTENSIONS or not re.search(r'(?:download|/get(?:\.php)?|/slow_download)', parsed.path, re.I):
                continue
            found_ext = ext
        if parsed.scheme not in ('https', 'http') or url in seen:
            continue
        seen.add(url)
        results.append(validate_item({'title': title, 'author': author, 'url': url,
                                      'format': found_ext, 'source': final, 'site': site,
                                      'link_text': link['text']}))
    return results


def search_openlibrary(query, limit=10):
    """Search work metadata and resolve listed Internet Archive EPUB/PDF files."""
    params = urllib.parse.urlencode({'q': query, 'fields': 'key,title,author_name,ia', 'limit': min(limit, 25)})
    raw, _ = http_get('https://openlibrary.org/search.json?' + params)
    results = []
    for book in json.loads(raw).get('docs', []):
        for identifier in book.get('ia', [])[:2]:
            if not re.fullmatch(r'[\w.-]{1,128}', identifier): continue
            meta_raw, _ = http_get('https://archive.org/metadata/' + urllib.parse.quote(identifier))
            metadata = json.loads(meta_raw)
            if str(metadata.get('metadata', {}).get('access-restricted-item', '')).lower() == 'true':
                continue
            for file in metadata.get('files', []):
                filename = file.get('name', '')
                ext = Path(filename).suffix.lower()
                if (ext not in ('.epub', '.pdf') or str(file.get('private', '')).lower() == 'true'
                        or 'encrypted' in filename.lower() or '_lcp.' in filename.lower()
                        or 'encrypted' in file.get('format', '').lower()):
                    continue
                url = 'https://archive.org/download/' + urllib.parse.quote(identifier) + '/' + urllib.parse.quote(filename)
                results.append(validate_item({'title': book.get('title') or identifier,
                    'author': ', '.join(book.get('author_name', [])[:3]) or 'Unknown',
                    'url': url, 'format': ext, 'source': 'https://archive.org/details/' + identifier,
                    'source_id': identifier, 'site': 'openlibrary'}))
                break
            if results and results[-1].get('source_id') == identifier: break
    return results


# Open Library lists organisations as authors ("Nuffield Foundation.", "International
# Conference on the Three Body Problem"); for a series or title search they bury
# the books, so only people are offered as author results.
_CORPORATE_AUTHOR = re.compile(
    r'\b(?:foundation|conference|symposium|congress|society|institute|institution|university|college|'
    r'association|council|committee|commission|department|ministry|agency|bureau|project|library|museum|'
    r'company|corporation|inc|ltd|llc|gmbh|press|publishing|publishers|club|league|office|'
    r'workshop|seminar|meeting|colloquium|'
    # The same in the languages Open Library catalogues most ("Robert Bosch Stiftung").
    r'stiftung|verlag|gesellschaft|verein|universit[aä]t|fundaci[oó]n|fundac[aã]o|fondation|'
    r'fondazione|universit[eé]|universidad|universidade|universit[aà]|soci[eé]t[eé]|'
    r'sociedad|societ[aà]|[eé]ditions?|editorial|ediciones|instituto|institut)\b|'
    r'^friends\s+of\b|\((?:U\.S\.|Great Britain|Firm)\)', re.I)


MIN_AUTHOR_WORKS = 3


def search_openlibrary_metadata(query, limit=20):
    """Search Open Library for authors, books, and series with member works."""
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    limit = max(1, min(int(limit), 30))
    results, errors = [], []

    author_url = 'https://openlibrary.org/search/authors.json?' + urllib.parse.urlencode(
        {'q': query, 'limit': min(limit, 10)})
    seen_authors = set()
    try:
        raw, final = http_get(author_url, max_bytes=2_000_000)
        if urllib.parse.urlsplit(final).hostname != 'openlibrary.org':
            raise ValueError('Open Library author search redirected away from its own site')
        for row in json.loads(raw.decode('utf-8')).get('docs', []):
            key = str(row.get('key') or '')
            name = str(row.get('name') or '').strip()
            key = key if key.startswith('/authors/') else '/authors/' + key
            normalized_name = ' '.join(name.casefold().split())
            # "Bridgerton Fans" or "For The For The Culture 365" are one-off records,
            # not authors to follow; real authors have a body of work.
            if (name and normalized_name not in seen_authors and not _CORPORATE_AUTHOR.search(name) and
                    int(row.get('work_count') or 0) >= MIN_AUTHOR_WORKS and
                    re.fullmatch(r'/authors/OL\d+A', key)):
                seen_authors.add(normalized_name)
                results.append({
                    'provider': 'Open Library', 'kind': 'author', 'title': name,
                    'author': 'Unknown', 'source_url': 'https://openlibrary.org' + key,
                    'catalog_metadata': {'catalog_id': key, 'work_count': row.get('work_count')},
                })
    except Exception as exc:
        errors.append(f'author search: {exc}')

    params = urllib.parse.urlencode({
        'q': query, 'fields': 'key,title,author_name,first_publish_year,edition_count,series,isbn',
        'limit': 100,
    })
    try:
        raw, final = http_get('https://openlibrary.org/search.json?' + params,
                              max_bytes=4_000_000)
        if urllib.parse.urlsplit(final).hostname != 'openlibrary.org':
            raise ValueError('Open Library book search redirected away from its own site')
        docs = json.loads(raw.decode('utf-8')).get('docs', [])
        series_groups = {}
        normalized_query = ' '.join(re.findall(r'[a-z0-9]+', query.casefold()))
        normalized_query = re.sub(r'^(?:the|a|an)\s+', '', normalized_query)
        for row in docs:
            key = str(row.get('key') or '')
            title = str(row.get('title') or '').strip()
            if not title or not re.fullmatch(r'/works/OL\d+W', key):
                continue
            authors = row.get('author_name') or []
            source_url = 'https://openlibrary.org' + key
            author = ', '.join(str(name) for name in authors[:3]) or 'Unknown'
            series_values = row.get('series') or []
            if isinstance(series_values, str):
                series_values = [series_values]
            parsed_series = []
            for raw_series in series_values:
                series_name = str(raw_series or '').strip()
                series_position = None
                position_match = re.search(
                    r'\s+(?:#|book\s+|volume\s+)(\d+(?:\.\d+)?)\s*$',
                    series_name, flags=re.I)
                if position_match:
                    parsed_position = float(position_match.group(1))
                    series_position = (int(parsed_position) if parsed_position.is_integer()
                                       else parsed_position)
                    series_name = series_name[:position_match.start()].strip()
                clean_series = re.sub(r'\s*\((?:book\s+)?series\)\s*$', '', series_name,
                                      flags=re.I).strip()
                if clean_series:
                    parsed_series.append({'name': clean_series,
                                          'series_index': series_position})
                normalized_series = ' '.join(re.findall(r'[a-z0-9]+', clean_series.casefold()))
                normalized_series = re.sub(r'^(?:the|a|an)\s+', '', normalized_series)
                if clean_series and normalized_series == normalized_query:
                    try:
                        year = int(row.get('first_publish_year'))
                        release_date = f'{year:04d}-01-01' if 1000 <= year <= 9999 else None
                    except (TypeError, ValueError):
                        release_date = None
                    member = {'title': title, 'author': author,
                              'source_url': source_url,
                              'release_date': release_date,
                              'series': clean_series,
                              'series_index': series_position,
                              'series_position_source': ('Open Library series field'
                                                         if series_position is not None else None),
                              'catalog_metadata': {'catalog_id': key,
                                                   'first_publish_year': row.get('first_publish_year'),
                                                   'edition_count': row.get('edition_count'),
                                                   'isbns': collect_isbns(row.get('isbn'))}}
                    series_groups.setdefault(clean_series, {})[source_url] = member
            catalog_metadata = {'catalog_id': key,
                                'first_publish_year': row.get('first_publish_year'),
                                'edition_count': row.get('edition_count'),
                                'series': series_values,
                                'normalized_series': parsed_series}
            isbns = collect_isbns(row.get('isbn'))
            if isbns:
                catalog_metadata['isbns'] = isbns
            results.append({
                'provider': 'Open Library', 'kind': 'book', 'title': title,
                'author': author, 'source_url': source_url,
                'series': parsed_series[0]['name'] if len(parsed_series) == 1 else None,
                'series_index': (parsed_series[0]['series_index']
                                 if len(parsed_series) == 1 else None),
                'series_position_source': ('Open Library series field'
                                           if len(parsed_series) == 1 and
                                           parsed_series[0]['series_index'] is not None else None),
                'catalog_metadata': catalog_metadata,
            })
        for series_name, members in series_groups.items():
            if len(members) < 2:
                continue
            series_url = 'https://openlibrary.org/search.json?' + urllib.parse.urlencode(
                {'series': series_name})
            results.append({
                'provider': 'Open Library', 'kind': 'series', 'title': series_name,
                'author': 'Unknown', 'source_url': series_url,
                'books': list(members.values()),
                'catalog_metadata': {'catalog_id': 'series:' + series_name,
                                     'series_name': series_name,
                                     'member_count': len(members)},
            })
    except Exception as exc:
        errors.append(f'book search: {exc}')

    deduplicated = {}
    for result in results:
        deduplicated[(result['kind'], result['source_url'])] = result
    found = list(deduplicated.values())
    found.sort(key=lambda item: 0 if item.get('kind') == 'series' else 1)
    found = found[:limit]
    if not found and errors:
        raise ValueError('Open Library metadata search failed: ' + '; '.join(errors))
    return found


def search_google_books_metadata(query, limit=20):
    """Search Google Books for book records and explicit series metadata.

    Google Books is treated as a metadata catalog only. It never returns a
    downloadable file candidate.
    """
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    limit = max(1, min(int(limit), 40))
    fields = {'q': query, 'maxResults': limit, 'projection': 'full', 'printType': 'books'}
    # Without a key, every app shares one small quota and Google answers 429.
    # A free key (Google Cloud console, Books API) gives this app its own quota.
    key = os.environ.get('GOOGLE_BOOKS_API_KEY', '').strip()
    if key:
        fields['key'] = key
    raw, final = http_get('https://www.googleapis.com/books/v1/volumes?' + urllib.parse.urlencode(fields),
                          max_bytes=5_000_000)
    if urllib.parse.urlsplit(final).hostname != 'www.googleapis.com':
        raise ValueError('Google Books search redirected away from its API host')
    payload = json.loads(raw.decode('utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('Google Books returned an invalid search response')
    results, series_groups = [], {}
    for volume in payload.get('items') or []:
        if not isinstance(volume, dict):
            continue
        volume_id = str(volume.get('id') or '').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', volume_id):
            continue
        info = volume.get('volumeInfo') or {}
        if not isinstance(info, dict):
            continue
        title = str(info.get('title') or '').strip()
        if not title:
            continue
        authors = info.get('authors') or []
        if not isinstance(authors, list):
            authors = []
        author = ', '.join(str(value).strip() for value in authors[:5]
                           if str(value).strip()) or 'Unknown'
        source_url = 'https://books.google.com/books?' + urllib.parse.urlencode(
            {'id': volume_id})
        identifiers = [str(value.get('identifier') or '')
                       for value in info.get('industryIdentifiers') or []
                       if isinstance(value, dict) and
                       str(value.get('type') or '').upper() in {'ISBN_10', 'ISBN_13'}]
        isbns = collect_isbns(identifiers)
        published = str(info.get('publishedDate') or '').strip()
        release_date = None
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', published):
            try:
                release_date = dt.date.fromisoformat(published).isoformat()
            except ValueError:
                pass
        series_info = info.get('seriesInfo') or {}
        if not isinstance(series_info, dict):
            series_info = {}
        display_number = str(series_info.get('bookDisplayNumber') or '').strip()
        position_match = re.search(r'(?<!\d)(\d+(?:\.\d+)?)(?!\d)', display_number)
        series_position = None
        if position_match:
            value = float(position_match.group(1))
            series_position = int(value) if value.is_integer() else value
        volume_series = series_info.get('volumeSeries') or []
        if isinstance(volume_series, dict):
            volume_series = [volume_series]
        series_references = []
        for reference in volume_series:
            if not isinstance(reference, dict):
                continue
            series_name = str(reference.get('seriesTitle') or reference.get('title') or
                               info.get('seriesTitle') or '').strip()
            series_id = str(reference.get('seriesId') or '').strip()
            order = reference.get('orderNumber') or reference.get('volumeSeriesOrder')
            if series_position is None and order is not None:
                order_match = re.search(r'\d+(?:\.\d+)?', str(order))
                if order_match:
                    value = float(order_match.group(0))
                    series_position = int(value) if value.is_integer() else value
            if series_name or series_id:
                series_references.append({'name': series_name, 'series_id': series_id,
                                          'series_index': series_position})
        series_name = next((item['name'] for item in series_references if item['name']), '')
        catalog_metadata = {
            'catalog_id': volume_id, 'google_books_id': volume_id,
            'published_date': published, 'page_count': info.get('pageCount'),
            'language': info.get('language'), 'series_references': series_references,
        }
        if isbns:
            catalog_metadata['isbns'] = isbns
        if series_name:
            catalog_metadata['series'] = [
                f'{series_name} #{series_position}' if series_position is not None
                else series_name]
        member = {
            'title': title, 'author': author, 'source_url': source_url,
            'release_date': release_date, 'series': series_name or None,
            'series_index': series_position,
            'series_position_source': ('Google Books seriesInfo'
                                       if series_position is not None else None),
            'provider': 'Google Books', 'catalog_metadata': catalog_metadata,
        }
        results.append({**member, 'kind': 'book'})
        if series_name:
            group_key = next((item['series_id'] for item in series_references
                              if item['name'] == series_name and item['series_id']),
                             'name:' + ' '.join(series_name.casefold().split()))
            group = series_groups.setdefault(group_key, {'title': series_name, 'books': {}})
            group['books'][volume_id] = member

    for group_key, group in series_groups.items():
        if len(group['books']) < 2:
            continue
        results.append({
            'provider': 'Google Books', 'kind': 'series', 'title': group['title'],
            'author': 'Unknown',
            'source_url': 'https://books.google.com/books?' +
                          urllib.parse.urlencode({'q': 'series:' + group['title']}),
            'books': list(group['books'].values()),
            'catalog_metadata': {'catalog_id': group_key, 'series_id': group_key,
                                 'member_count': len(group['books'])},
        })
    return results
