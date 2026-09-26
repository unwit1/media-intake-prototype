"""Public comic and manga catalog adapters."""
import datetime as dt
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from .core import http_get

ANILIST_URL = 'https://graphql.anilist.co'
TADDY_URL = 'https://api.taddy.org'
MANGADEX_API = 'https://api.mangadex.org'
MANGADEX_SITE = 'https://mangadex.org'
MANGADEX_ID_RE = re.compile(r'/title/([0-9a-f-]{36})(?:/|$)', re.I)
_MANGADEX_REQUEST_LOCK = threading.Lock()
_MANGADEX_LAST_REQUEST = 0.0


def mangadex_http_get(url, max_bytes=10_000_000):
    """Serialize MangaDex and image CDN requests below its published global rate."""
    global _MANGADEX_LAST_REQUEST
    with _MANGADEX_REQUEST_LOCK:
        wait = 0.25 - (time.monotonic() - _MANGADEX_LAST_REQUEST)
        if wait > 0:
            time.sleep(wait)
        try:
            return http_get(url, max_bytes=max_bytes)
        finally:
            _MANGADEX_LAST_REQUEST = time.monotonic()


def _query_text(query):
    value = str(query or '').strip()
    if not value or len(value) > 150 or any(ord(char) < 32 for char in value):
        raise ValueError('Enter a search up to 150 characters')
    return value


def _anilist_release_date(value):
    if not value or not value.get('year'):
        return None
    year = int(value['year'])
    month = int(value.get('month') or 1)
    day = int(value.get('day') or 1)
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def search_anilist(query, limit=20):
    """Search AniList's public manga catalog and normalize its metadata."""
    query = _query_text(query)
    limit = max(1, min(int(limit), 30))
    gql = '''query ($search: String!, $limit: Int!) {
      Page(page: 1, perPage: $limit) {
        media(search: $search, type: MANGA, isAdult: false) {
          id siteUrl format status chapters volumes countryOfOrigin genres
          title { english romaji native }
          description(asHtml: false)
          startDate { year month day }
          externalLinks { site url }
          staff(page: 1, perPage: 15) {
            edges { role node { name { full } } }
          }
        }
      }
    }'''
    request = urllib.request.Request(
        ANILIST_URL,
        data=json.dumps({'query': gql, 'variables': {'search': query, 'limit': limit}}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'MediaIntakePrototype/0.2'},
        method='POST')
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('AniList response exceeds size limit')
    payload = json.loads(raw.decode('utf-8'))
    if payload.get('errors'):
        messages = [str(error.get('message') or 'AniList query failed') for error in payload['errors']]
        raise ValueError('; '.join(messages[:2]))

    results = []
    media_rows = payload.get('data', {}).get('Page', {}).get('media') or []
    for media in media_rows:
        titles = media.get('title') or {}
        title = titles.get('english') or titles.get('romaji') or titles.get('native') or 'Untitled manga'
        creators = []
        for edge in (media.get('staff') or {}).get('edges') or []:
            role = str(edge.get('role') or '').casefold()
            name = ((edge.get('node') or {}).get('name') or {}).get('full')
            if name and any(word in role for word in ('story', 'art', 'original creator', 'author')):
                if name not in creators:
                    creators.append(name)
        external_links = media.get('externalLinks') or []
        mangadex_id = None
        for link in external_links:
            if str(link.get('site') or '').casefold() in ('mangadex', 'mangadex.org'):
                match = MANGADEX_ID_RE.search(urllib.parse.urlsplit(link.get('url') or '').path)
                if match:
                    mangadex_id = match.group(1)
                    break
        results.append({
            'provider': 'anilist',
            'kind': 'webtoon' if str(media.get('format') or '').upper() == 'WEBTOON' else 'manga',
            'title': title,
            'author': ', '.join(creators) or 'Unknown',
            'release_date': _anilist_release_date(media.get('startDate')),
            'source_url': media.get('siteUrl') or f'https://anilist.co/manga/{media.get("id")}',
            'anilist_id': media.get('id'),
            'mangadex_id': mangadex_id,
            'description': re.sub(r'\s+', ' ', str(media.get('description') or '')).strip(),
            'format': media.get('format'), 'status': media.get('status'),
            'chapters': media.get('chapters'), 'volumes': media.get('volumes'),
            'country_of_origin': media.get('countryOfOrigin'),
            'genres': media.get('genres') or [],
            'external_links': [{'site': link.get('site'), 'url': link.get('url'),
                                'type': link.get('type')}
                               for link in external_links if link.get('url')],
        })
    return results


def search_mangadex(query, limit=20):
    """Search MangaDex titles. The result can also drive chapter tracking."""
    query = _query_text(query)
    limit = max(1, min(int(limit), 30))
    url = MANGADEX_API + '/manga?' + urllib.parse.urlencode([
        ('title', query), ('limit', str(limit)), ('order[relevance]', 'desc'),
        ('contentRating[]', 'safe'), ('includes[]', 'author'), ('includes[]', 'artist')])
    raw, _ = mangadex_http_get(url, max_bytes=4_000_000)
    payload = json.loads(raw.decode('utf-8'))
    results = []
    for manga in payload.get('data') or []:
        attrs = manga.get('attributes') or {}
        titles = attrs.get('title') or {}
        title = titles.get('en') or next(iter(titles.values()), 'Untitled manga')
        creators = []
        for relation in manga.get('relationships') or []:
            if relation.get('type') not in ('author', 'artist'):
                continue
            name = ((relation.get('attributes') or {}).get('name') or '').strip()
            if name and name not in creators:
                creators.append(name)
        manga_id = manga.get('id')
        results.append({
            'provider': 'mangadex', 'kind': 'manga', 'title': title,
            'author': ', '.join(creators) or 'Unknown',
            'source_url': f'{MANGADEX_SITE}/title/{manga_id}',
            'mangadex_id': manga_id,
            'description': str((attrs.get('description') or {}).get('en') or ''),
        })
    return results


def search_taddy(query, user_id, api_key, limit=20):
    """Search Taddy's SSS comic catalog using caller-provided credentials."""
    query = _query_text(query)
    user_id = str(user_id or '').strip()
    api_key = str(api_key or '').strip()
    if not re.fullmatch(r'\d{1,20}', user_id):
        raise ValueError('Enter the numeric Taddy user ID')
    if not api_key or len(api_key) > 500:
        raise ValueError('Enter a valid Taddy API key')
    limit = max(1, min(int(limit), 25))
    gql = '''query SearchComicSeries($term: String!, $limit: Int!) {
      search(term: $term, filterForTypes: [COMICSERIES], limitPerPage: $limit) {
        comicSeries {
          uuid name description status sssUrl seriesType totalIssuesCount datePublished
          creators { name }
        }
      }
    }'''
    request = urllib.request.Request(
        TADDY_URL,
        data=json.dumps({'query': gql, 'variables': {'term': query, 'limit': limit}}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'X-USER-ID': user_id, 'X-API-KEY': api_key,
                 'User-Agent': 'MediaIntakePrototype/0.2'},
        method='POST')
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Taddy response exceeds size limit')
    payload = json.loads(raw.decode('utf-8'))
    if payload.get('errors'):
        messages = [str(error.get('message') or 'Taddy query failed')
                    for error in payload['errors'] if isinstance(error, dict)]
        raise ValueError('; '.join(messages[:3]) or 'Taddy comic search failed')
    series = (((payload.get('data') or {}).get('search') or {}).get('comicSeries') or [])
    kinds = {
        'WEBTOON': 'webtoon', 'MANGA': 'manga', 'MANHWA': 'manga', 'MANHUA': 'manga',
        'AMERICAN_STYLE_COMIC': 'webcomic', 'GRAPHIC_NOVEL': 'webcomic',
        'ANTHOLOGY': 'webcomic', 'ONE_SHOT': 'webcomic',
    }
    results = []
    for entry in series:
        if not isinstance(entry, dict) or not entry.get('name'):
            continue
        feed_url = str(entry.get('sssUrl') or '').strip()
        parsed_feed = urllib.parse.urlsplit(feed_url)
        if parsed_feed.scheme != 'https' or not parsed_feed.netloc or parsed_feed.username or parsed_feed.password:
            feed_url = ''
        creators = entry.get('creators') or []
        author_names = [str(creator.get('name') or '').strip() for creator in creators
                        if isinstance(creator, dict) and creator.get('name')]
        release_date = None
        try:
            timestamp = int(entry.get('datePublished'))
            release_date = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        metadata = {key: entry.get(key) for key in
                    ('description', 'status', 'seriesType', 'totalIssuesCount') if entry.get(key) is not None}
        results.append({
            'provider': 'Taddy', 'taddy_id': str(entry.get('uuid') or ''),
            'title': str(entry['name']).strip(), 'author': ', '.join(author_names) or 'Unknown',
            'kind': kinds.get(str(entry.get('seriesType') or '').upper(), 'webcomic'),
            'release_date': release_date, 'source_url': feed_url,
            'sss_url': feed_url, 'catalog_metadata': metadata,
        })
    return results


def mangadex_id_from_url(url):
    parsed = urllib.parse.urlsplit(str(url or ''))
    if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('mangadex.org', 'www.mangadex.org'):
        return None
    match = MANGADEX_ID_RE.search(parsed.path)
    return match.group(1) if match else None


def list_mangadex_chapters(manga_id, language='en', limit=500):
    """List recent public, safe chapters in one language for a MangaDex title."""
    if not re.fullmatch(r'[0-9a-f-]{36}', str(manga_id or ''), flags=re.I):
        raise ValueError('Invalid MangaDex title ID')
    language = str(language or 'en').strip().casefold()
    if not re.fullmatch(r'[a-z]{2,3}', language):
        raise ValueError('MangaDex language must be a two or three letter code')
    limit = max(1, min(int(limit), 500))
    params = [
        ('limit', str(limit)), ('offset', '0'), ('order[readableAt]', 'desc'),
        ('translatedLanguage[]', language), ('contentRating[]', 'safe'),
        ('includes[]', 'scanlation_group'),
    ]
    url = f'{MANGADEX_API}/manga/{manga_id}/feed?' + urllib.parse.urlencode(params)
    raw, _ = mangadex_http_get(url, max_bytes=8_000_000)
    payload = json.loads(raw.decode('utf-8'))
    chapters = []
    for row in payload.get('data') or []:
        attrs = row.get('attributes') or {}
        chapter_id = row.get('id')
        if not chapter_id:
            continue
        number = str(attrs.get('chapter') or '').strip()
        volume = str(attrs.get('volume') or '').strip()
        chapter_title = str(attrs.get('title') or '').strip()
        display = ('Chapter ' + number if number else chapter_title or 'Chapter')
        if chapter_title and number:
            display += ' - ' + chapter_title
        groups = []
        for relation in row.get('relationships') or []:
            if relation.get('type') == 'scanlation_group':
                name = ((relation.get('attributes') or {}).get('name') or '').strip()
                credit = name or relation.get('id')
                if credit and credit not in groups:
                    groups.append(str(credit))
        chapters.append({
            'title': display, 'number': number, 'volume': volume,
            'published': attrs.get('readableAt') or attrs.get('publishAt') or attrs.get('createdAt'),
            'url': f'{MANGADEX_SITE}/chapter/{chapter_id}',
            'mangadex_id': chapter_id, 'groups': groups,
        })
    # A MangaDex feed may contain several translations of one chapter. Prefer
    # its most recent English release and avoid auto-saving duplicate pages.
    unique = {}
    for chapter in chapters:
        chapter_title = _stable_chapter_title(chapter['title'])
        key = ((chapter['volume'] + ':') if chapter['volume'] else '') + (
            chapter['number'] or chapter_title or chapter['mangadex_id'])
        if key not in unique:
            unique[key] = chapter
    return list(reversed(list(unique.values())))


def _stable_chapter_title(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').casefold()).strip()
