"""Metadata lookup and file intake for novel catalogs."""
import datetime as dt
import difflib
import html
from html.parser import HTMLParser
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from .core import http_get, open_url

NOVELARCHIVE_API = 'https://novelarchive.cc/api'
NOVELARCHIVE_SITE = 'https://novelarchive.cc'
LNORI_SITE = 'https://lnori.com'
RANOBEDB_API = 'https://ranobedb.org/api/v0'
MANGAUPDATES_API = 'https://api.mangaupdates.com/v1'
HARDCOVER_API = 'https://api.hardcover.app/v1/graphql'
_RANOBEDB_LOCK = threading.Lock()
_RANOBEDB_LAST_REQUEST = 0.0


def _query(value):
    text = str(value or '').strip()
    if not text or len(text) > 150 or any(ord(char) < 32 for char in text):
        raise ValueError('Enter a search up to 150 characters')
    return text


def _limit(value, maximum=30):
    return max(1, min(int(value), maximum))


def fetch_hardcover_list(url, api_token):
    """Read a Hardcover custom list through its documented GraphQL API."""
    parts = urllib.parse.urlsplit(str(url or '').strip())
    host = (parts.hostname or '').casefold()
    path = [urllib.parse.unquote(item) for item in parts.path.split('/') if item]
    if (parts.scheme != 'https' or not (host == 'hardcover.app' or host.endswith('.hardcover.app'))
            or parts.username or parts.password):
        raise ValueError('Enter a public HTTPS Hardcover list URL.')
    if not api_token:
        raise ValueError('Add a Hardcover API token in Settings → Metadata APIs to refresh this list.')
    try:
        list_index = path.index('lists')
        list_slug = path[list_index + 1]
    except (ValueError, IndexError):
        raise ValueError('Hardcover URL must point to a custom list, such as /@username/lists/slug.')
    if not list_slug or len(list_slug) > 160:
        raise ValueError('The Hardcover list URL does not contain a valid list name.')
    username = ''
    if list_index > 0 and path[list_index - 1].startswith('@'):
        username = path[list_index - 1][1:]
    books_fragment = ('list_books(limit: 1000) { book { id title slug release_date '
                      'contributions { author { name } } } }')

    def select_list(data, container):
        users = data.get(container)
        user = users[0] if isinstance(users, list) and users else users
        if not isinstance(user, dict):
            return None
        lists = user.get('lists') or []
        return next(iter(lists), None) if isinstance(lists, list) else None

    if username:
        query = ('query UserList($username: String!, $slug: String!) { '
                 'users(where: {username: {_eq: $username}}) { '
                 'lists(where: {slug: {_eq: $slug}}) { id name slug ' +
                 books_fragment + ' } } }')
        variables = {'username': username, 'slug': list_slug}
        selector = lambda data: select_list(data, 'users')
    elif list_slug.isdigit():
        query = ('query ListById($listId: Int!) { lists(where: {id: {_eq: $listId}}) '
                 '{ id name slug ' + books_fragment + ' } }')
        variables = {'listId': int(list_slug)}
        selector = lambda data: next(iter(data.get('lists') or []), None)
    else:
        query = ('query MyList($slug: String!) { me { lists(where: {slug: {_eq: $slug}}) '
                 '{ id name slug ' + books_fragment + ' } } }')
        variables = {'slug': list_slug}
        selector = lambda data: select_list(data, 'me')
    payload = json.dumps({'query': query, 'variables': variables}).encode('utf-8')
    request = urllib.request.Request(
        HARDCOVER_API, data=payload, method='POST',
        headers={'Authorization': 'Bearer ' + str(api_token).strip(),
                 'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'MediaIntakePrototype/0.1'})
    with open_url(request, timeout=35) as response:
        raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError('Hardcover response exceeds the 5 MB safety limit.')
    result = json.loads(raw.decode('utf-8'))
    if result.get('errors'):
        message = '; '.join(str(error.get('message') or 'GraphQL error')
                            for error in result['errors'][:3] if isinstance(error, dict))
        raise ValueError('Hardcover list lookup failed: ' + (message or 'check the API token and list URL'))
    listing = selector(result.get('data') or {})
    if not isinstance(listing, dict):
        raise ValueError('Hardcover did not return a list for this URL and account.')
    books = []
    for item in listing.get('list_books') or []:
        book = item.get('book') if isinstance(item, dict) else None
        if not isinstance(book, dict) or not str(book.get('title') or '').strip():
            continue
        author_names = []
        for contribution in book.get('contributions') or []:
            author = contribution.get('author') if isinstance(contribution, dict) else None
            name = str((author or {}).get('name') or '').strip() if isinstance(author, dict) else ''
            if name and name not in author_names:
                author_names.append(name)
        slug = str(book.get('slug') or book.get('id') or '').strip()
        book_url = f'https://hardcover.app/books/{urllib.parse.quote(slug)}' if slug else ''
        release_date = str(book.get('release_date') or '')[:10] or None
        books.append({
            'title': str(book['title']).strip(), 'author': ', '.join(author_names) or 'Unknown',
            'source_url': book_url,
            'release_date': release_date,
            'provider': 'Hardcover',
            'catalog_metadata': {'catalog_id': str(book.get('id') or ''),
                                 'list_name': str(listing.get('name') or '')},
        })
    return {'kind': 'book_list', 'provider': 'Hardcover',
            'title': str(listing.get('name') or list_slug),
            'author': 'Various authors', 'source_url': url,
            'books': books}


def _discovery_item(provider, title, author, url, **metadata):
    parts = urllib.parse.urlsplit(str(url or ''))
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password):
        raise ValueError('Catalog result did not contain a safe HTTPS page URL')
    return {
        'provider': provider, 'title': str(title or 'Untitled').strip(),
        'author': str(author or 'Unknown').strip(), 'url': url, 'source': url,
        'source_url': url, 'format': 'link', 'discovery_only': True,
        'metadata': metadata,
    }


def search_novelarchive(query, limit=20):
    """Search NovelArchive's public title metadata API, without reading chapters."""
    query = _query(query)
    limit = _limit(limit)
    url = NOVELARCHIVE_API + '/novels?' + urllib.parse.urlencode({'search': query, 'limit': limit})
    raw, final = http_get(url, max_bytes=3_000_000)
    if urllib.parse.urlsplit(final).hostname != 'novelarchive.cc':
        raise ValueError('NovelArchive search redirected away from its own site')
    payload = json.loads(raw.decode('utf-8'))
    novels = payload.get('novels') or []
    results = []
    for novel in novels[:limit]:
        if not isinstance(novel, dict) or not novel.get('id') or not novel.get('title'):
            continue
        page_url = NOVELARCHIVE_SITE + '/novel?' + urllib.parse.urlencode({'id': novel['id']})
        results.append(_discovery_item(
            'NovelArchive', novel.get('title'), novel.get('author'), page_url,
            catalog_id=str(novel['id']), description=str(novel.get('description') or '')[:1500],
            genres=str(novel.get('genres') or ''), chapters=str(novel.get('total_chapters') or ''),
            release_status=str(novel.get('release_status') or novel.get('ongoing') or ''),
            latest_release=str(novel.get('latest_release') or ''),
            has_image_chapters=bool(novel.get('has_image_chapters')),
        ))
    return results


class _LnoriCards(HTMLParser):
    """Extract only public series card attributes and their canonical page links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self.card = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(str(attrs.get('class') or '').split())
        if tag == 'article' and 'card' in classes and self.card is None:
            self.card = {
                'id': str(attrs.get('data-id') or ''), 'title': str(attrs.get('data-t') or ''),
                'author': str(attrs.get('data-a') or ''), 'year': str(attrs.get('data-d') or ''),
                'volumes': str(attrs.get('data-v') or ''), 'tags': str(attrs.get('data-tags') or ''),
                'url': '',
            }
            self.depth = 1
        elif self.card is not None:
            if tag == 'a' and not self.card['url']:
                href = urllib.parse.urljoin(LNORI_SITE + '/', attrs.get('href', ''))
                parts = urllib.parse.urlsplit(href)
                if (parts.scheme == 'https' and parts.hostname == 'lnori.com'
                        and re.fullmatch(r'/series/\d+(?:/[^?#]*)?', parts.path)):
                    self.card['url'] = href

    def handle_endtag(self, tag):
        if self.card is None:
            return
        if tag == 'article':
            if self.card['id'] and self.card['title'] and self.card['url']:
                self.cards.append(self.card)
            self.card = None
            self.depth = 0


class _CatalogPageLinks(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.links = []
        self.heading = []
        self.in_heading = False
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h1', 'h2'):
            self.in_heading = True
        if tag == 'a':
            self.current = {'url': urllib.parse.urljoin(self.page_url, attrs.get('href', '')), 'text': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data
        if self.in_heading:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = ' '.join(self.current['text'].split())
            self.links.append(self.current)
            self.current = None
        if tag in ('h1', 'h2'):
            self.in_heading = False


def resolve_catalog_file_links(item, approval, limit=20):
    """Resolve direct media links from an LNORI/NovelArchive page."""
    if not isinstance(approval, dict):
        approval = {}
    url = str(item.get('url') or '')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in {'lnori.com', 'novelarchive.cc'}:
        raise ValueError('Catalog page must belong to LNORI or NovelArchive')
    raw, final = http_get(url, max_bytes=5_000_000)
    parser = _CatalogPageLinks(final)
    parser.feed(raw.decode('utf-8', 'replace'))
    extensions = {'.epub', '.pdf', '.mobi', '.azw3', '.txt', '.cbz', '.cbr', '.mp3', '.m4b'}
    connector = 'lnori.com' if parts.hostname == 'lnori.com' else 'novelarchive.cc'
    results, seen = [], set()
    for link in parser.links:
        file_url = link['url']
        file_parts = urllib.parse.urlsplit(file_url)
        if file_parts.scheme != 'https' or not file_parts.hostname or file_parts.username or file_parts.password:
            continue
        ext = '.' + file_parts.path.rsplit('.', 1)[-1].casefold() if '.' in file_parts.path else ''
        if ext not in extensions or file_url in seen:
            continue
        seen.add(file_url)
        results.append({
            'provider': item.get('provider'), 'title': item.get('title') or 'Untitled',
            'author': item.get('author') or 'Unknown', 'url': file_url, 'source': final,
            # The file belongs to the catalog connector, even when its link
            # is hosted by a separate CDN or file service.
            'source_key': connector, 'format': ext,
            'discovery_only': False,
            **({'rights_work_key': approval['rights_work_key']}
               if approval.get('rights_work_key') else {}),
        })
        if len(results) >= max(1, min(int(limit), 50)):
            break
    return results


class _NovelArchiveChapterLinks(HTMLParser):
    def __init__(self, page_url):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.current = None
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attrs = dict(attrs)
            self.current = {'url': urllib.parse.urljoin(self.page_url, attrs.get('href', '')), 'text': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = ' '.join(self.current['text'].split())
            self.links.append(self.current)
            self.current = None


def discover_novelarchive_chapters(series_url, limit=500):
    """Build a chapter manifest from a NovelArchive series page."""
    parts = urllib.parse.urlsplit(str(series_url or ''))
    if parts.scheme != 'https' or parts.hostname != 'novelarchive.cc':
        raise ValueError('NovelArchive series URL must use its HTTPS host')
    raw, final = http_get(series_url, max_bytes=5_000_000)
    parser = _CatalogPageLinks(final)
    parser.feed(raw.decode('utf-8', 'replace'))
    anchors = _NovelArchiveChapterLinks(final)
    anchors.feed(raw.decode('utf-8', 'replace'))
    chapters, seen = [], set()
    for link in anchors.links:
        url = urllib.parse.urlsplit(link['url'])
        label = link['text']
        if (url.scheme != 'https' or url.hostname != 'novelarchive.cc'
                or not label or link['url'] in seen
                or not re.search(r'chapter|episode|part\s*\d', label, re.I)
                and not re.search(r'/chapter(?:s)?/', url.path, re.I)):
            continue
        seen.add(link['url'])
        chapters.append({'title': label, 'url': link['url']})
        if len(chapters) >= max(1, min(int(limit), 500)):
            break
    if not chapters:
        raise ValueError('No chapter links were available on the NovelArchive series page')
    chapters.reverse()
    return {
        'title': ' '.join(' '.join(parser.heading).split()),
        'author': 'Unknown', 'kind': 'webfiction', 'source': final,
        'selector': 'main,article,.chapter-content', 'chapters': chapters,
    }


def parse_lnori_catalog(content, query, limit=20):
    """Filter the server-rendered LNORI catalog cards by title, author, and tags."""
    query = _query(query)
    limit = _limit(limit)
    parser = _LnoriCards()
    parser.feed(content)
    needle = query.casefold()
    words = [word for word in re.findall(r'[\w]+', needle) if len(word) > 1]
    ranked = []
    for card in parser.cards:
        searchable = ' '.join((card['title'], card['author'], card['tags'])).casefold()
        if needle in searchable:
            score = 1.0
        else:
            title_score = difflib.SequenceMatcher(None, needle, card['title'].casefold()).ratio()
            overlap = sum(word in searchable for word in words) / max(1, len(words))
            score = max(title_score, overlap * 0.85)
        if score >= 0.45:
            ranked.append((score, card))
    ranked.sort(key=lambda value: (-value[0], value[1]['title'].casefold()))
    return [
        _discovery_item(
            'LNORI', card['title'], card['author'], card['url'], catalog_id=card['id'],
            publication_year=card['year'], volumes=card['volumes'], tags=card['tags'],
        ) for _, card in ranked[:limit]
    ]


def search_lnori(query, limit=20):
    """Search LNORI's public rendered catalog, returning metadata and page links only."""
    query = _query(query)
    raw, final = http_get(LNORI_SITE + '/library', max_bytes=3_000_000)
    if urllib.parse.urlsplit(final).hostname != 'lnori.com':
        raise ValueError('LNORI catalog redirected away from its own site')
    return parse_lnori_catalog(raw.decode('utf-8', 'replace'), query, limit)


def _ranobedb_get(path, params=None):
    """Call the public noncommercial API with a descriptive user agent and a 60/min cap."""
    global _RANOBEDB_LAST_REQUEST
    with _RANOBEDB_LOCK:
        wait = 1.05 - (time.monotonic() - _RANOBEDB_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = RANOBEDB_API + path + (('?' + query) if query else '')
        request = urllib.request.Request(url, headers={
            'Accept': 'application/json',
            'User-Agent': 'MediaIntakePrototype/0.3 (+https://github.com/unwit1/media-intake-prototype; metadata lookup)',
        })
        try:
            with open_url(request, timeout=30) as response:
                raw = response.read(3_000_001)
        finally:
            _RANOBEDB_LAST_REQUEST = time.monotonic()
    if len(raw) > 3_000_000:
        raise ValueError('RanobeDB response exceeds size limit')
    return json.loads(raw.decode('utf-8'))


def search_ranobedb(query, limit=20):
    """Search RanobeDB series metadata, including light novels and web novels."""
    query = _query(query)
    limit = _limit(limit, 50)
    payload = _ranobedb_get('/series', {'q': query, 'limit': limit})
    rows = payload.get('series') or []
    if isinstance(rows, dict):
        rows = [rows]
    results = []
    rows = [row for row in rows[:limit] if isinstance(row, dict) and row.get('id') is not None and row.get('title')]
    # sim_score: 2 = the title itself, 1 = an alias or part of a title, below 1 = loose.
    alias_hits = [row for row in rows if float(row.get('sim_score') or 0) >= 1]
    for row in rows:
        series_id = str(row['id'])
        names = [name for name in dict.fromkeys(
            [row.get('title'), row.get('romaji'), row.get('title_orig'), row.get('romaji_orig')]) if name]
        # "灼眼のシャナ" means little to an English reader; show "Shakugan no Shana".
        title = row['title']
        if re.search(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]', title) and row.get('romaji'):
            title = row['romaji']
        score = float(row.get('sim_score') or 0)
        item = _discovery_item(
            'RanobeDB', title, 'Unknown', f'https://ranobedb.org/series/{series_id}',
            catalog_id=series_id, original_language=row.get('olang') or row.get('lang'),
            book_count=row.get('c_num_books'), status=row.get('publication_status'),
            title_original=row.get('title_orig'), match_score=score,
        )
        item['alternative_titles'] = [name for name in names if name != title]
        # Trust a nickname match ("DanMachi") only when it is exact or the one alias
        # hit; "Hyouka" matches several unrelated titles containing that word.
        item['alias_match'] = score >= 2 or (score >= 1 and len(alias_hits) == 1 and row is alias_hits[0])
        results.append(item)
    return results


def ranobedb_series(series_id):
    """Fetch a series and its volume-level English release dates."""
    series_id = str(series_id or '').strip()
    if not re.fullmatch(r'\d{1,12}', series_id):
        raise ValueError('Invalid RanobeDB series ID')
    payload = _ranobedb_get('/series/' + series_id)
    series = payload.get('series') if isinstance(payload, dict) else None
    return series if isinstance(series, dict) else payload


def _mangaupdates_detail(series_id):
    if not re.fullmatch(r'\d{1,20}', str(series_id or '')):
        raise ValueError('Invalid MangaUpdates series ID')
    request = urllib.request.Request(
        MANGAUPDATES_API + '/series/' + str(series_id),
        headers={'Accept': 'application/json',
                 'User-Agent': 'MediaIntakePrototype/0.3 (personal metadata lookup)'})
    with open_url(request, timeout=30) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError('MangaUpdates response exceeds size limit')
    return json.loads(raw.decode('utf-8'))


def search_mangaupdates(query, limit=10):
    """Search MangaUpdates and fetch its English licensing flag for each match."""
    query = _query(query)
    limit = _limit(limit, 10)
    request = urllib.request.Request(
        MANGAUPDATES_API + '/series/search',
        data=json.dumps({'search': query, 'perpage': limit, 'stype': 'title'}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'MediaIntakePrototype/0.3 (personal metadata lookup)'},
        method='POST')
    with open_url(request, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('MangaUpdates response exceeds size limit')
    payload = json.loads(raw.decode('utf-8'))
    results = []
    for hit in (payload.get('results') or [])[:limit]:
        record = hit.get('record') or hit
        series_id = record.get('series_id') or record.get('id')
        if not series_id or not record.get('title'):
            continue
        detail = _mangaupdates_detail(str(series_id))
        publishers = detail.get('publishers') or []
        english_publishers = []
        for publisher in publishers:
            if not isinstance(publisher, dict):
                continue
            name = str(publisher.get('publisher_name') or publisher.get('name') or '').strip()
            language = str(publisher.get('lang_name') or publisher.get('language') or '').casefold()
            if name and (not language or language in ('english', 'en')) and name not in english_publishers:
                english_publishers.append(name)
        results.append(_discovery_item(
            'MangaUpdates', detail.get('title') or record['title'], 'Unknown',
            detail.get('url') or record.get('url') or f'https://www.mangaupdates.com/series/{series_id}',
            catalog_id=str(series_id), english_licensed=detail.get('licensed'),
            english_publishers=english_publishers, series_type=detail.get('type'),
        ))
    return results


def english_release_date(value):
    """Convert RanobeDB's numeric YYYYMMDD value to ISO, if present and valid."""
    if value in (None, '', 0, '0'):
        return None
    text = str(value).strip()
    if not re.fullmatch(r'\d{8}', text):
        return None
    try:
        return dt.datetime.strptime(text, '%Y%m%d').date().isoformat()
    except ValueError:
        return None
