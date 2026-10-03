"""Public comic and manga catalog adapters."""
import datetime as dt
import json
import re
import threading
import time
import urllib.parse
import urllib.request

from .core import http_get, open_url

ANILIST_URL = 'https://graphql.anilist.co'
TADDY_URL = 'https://api.taddy.org'
COMICVINE_API = 'https://comicvine.gamespot.com/api'
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
    if not value or not value.get('year') or not value.get('month') or not value.get('day'):
        return None
    year = int(value['year'])
    month = int(value['month'])
    day = int(value['day'])
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def anilist_video_subtype(media_format):
    """Map AniList's anime formats into the shared Video subtype vocabulary."""
    return {
        'TV': 'anime_series', 'TV_SHORT': 'anime_short_series',
        'ONA': 'anime_web_series', 'MOVIE': 'anime_movie',
        'SPECIAL': 'anime_special', 'OVA': 'anime_ova',
        'MUSIC': 'anime_music_video',
    }.get(str(media_format or '').upper(), 'anime')


def search_anilist(query, limit=20, media_type='MANGA'):
    """Search AniList's public manga or anime catalog and normalize its metadata."""
    query = _query_text(query)
    limit = max(1, min(int(limit), 30))
    media_type = str(media_type or 'MANGA').upper()
    if media_type not in {'MANGA', 'ANIME'}:
        raise ValueError('AniList media type must be MANGA or ANIME')
    gql = '''query ($search: String!, $limit: Int!) {
      Page(page: 1, perPage: $limit) {
        media(search: $search, type: MEDIA_TYPE, isAdult: false) {
          id idMal siteUrl format status chapters volumes countryOfOrigin genres synonyms popularity
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
    gql = gql.replace('MEDIA_TYPE', media_type)
    request = urllib.request.Request(
        ANILIST_URL,
        data=json.dumps({'query': gql, 'variables': {'search': query, 'limit': limit}}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'MediaIntakePrototype/0.2'},
        method='POST')
    with open_url(request, timeout=30) as response:
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
        title = titles.get('english') or titles.get('romaji') or titles.get('native') or (
            'Untitled anime' if media_type == 'ANIME' else 'Untitled manga')
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
        anilist_id = media.get('id')
        external_ids = ([{'provider': 'anilist', 'external_id': str(anilist_id),
                          'source_url': media.get('siteUrl')}]
                        if anilist_id else [])
        if mangadex_id:
            external_ids.append({'provider': 'mangadex', 'external_id': str(mangadex_id),
                                 'source_url': f'https://mangadex.org/title/{mangadex_id}'})
        if media.get('idMal'):
            external_ids.append({'provider': 'mal_' + media_type.lower(), 'external_id': str(media['idMal'])})
        # Every name the work goes by: the English title, romaji, native and
        # synonyms, so "JoJo's Bizarre Adventure" matches "JoJo no Kimyou na Bouken".
        alternative_titles = [name for name in dict.fromkeys(
            [titles.get('english'), titles.get('romaji'), titles.get('native'), *(media.get('synonyms') or [])])
            if name and name != title]
        anime_format = str(media.get('format') or '').upper() if media_type == 'ANIME' else ''
        anime_series = anime_format in {'TV', 'TV_SHORT', 'ONA'}
        anime_subtype = anilist_video_subtype(anime_format) if media_type == 'ANIME' else None
        catalog_metadata = {
            'catalog_provider': 'AniList', 'catalog_id': str(anilist_id or ''),
            'external_ids': external_ids,
            'format': media.get('format'), 'status': media.get('status'),
            'chapters': media.get('chapters'), 'volumes': media.get('volumes'),
            'country_of_origin': media.get('countryOfOrigin'),
            'genres': media.get('genres') or [],
            'description': re.sub(r'\s+', ' ', str(media.get('description') or '')).strip(),
        }
        results.append({
            'provider': 'anilist',
            'kind': ('anime' if media_type == 'ANIME' else
                     'webtoon' if str(media.get('format') or '').upper() == 'WEBTOON' else 'manga'),
            'entity_type': 'series' if anime_series else 'work',
            'video_subtype': anime_subtype,
            'title': title, 'alternative_titles': alternative_titles, 'popularity': media.get('popularity') or 0,
            'author': ', '.join(creators) or 'Unknown',
            'release_date': _anilist_release_date(media.get('startDate')),
            'source_url': media.get('siteUrl') or
                          f'https://anilist.co/{"anime" if media_type == "ANIME" else "manga"}/{media.get("id")}',
            'anilist_id': media.get('id'),
            'mangadex_id': mangadex_id,
            'description': re.sub(r'\s+', ' ', str(media.get('description') or '')).strip(),
            'format': media.get('format'), 'status': media.get('status'),
            'chapters': media.get('chapters'), 'volumes': media.get('volumes'),
            'country_of_origin': media.get('countryOfOrigin'),
            'genres': media.get('genres') or [],
            'publication_status': media.get('status'),
            'external_ids': external_ids,
            'catalog_metadata': catalog_metadata,
            'external_links': [{'site': link.get('site'), 'url': link.get('url'),
                                'type': link.get('type')}
                               for link in external_links if link.get('url')],
        })
    return results


def fetch_anilist_media(media_id):
    """Refresh one AniList anime/manga record, including dated follow-ups."""
    media_id = int(media_id)
    if media_id <= 0:
        raise ValueError('AniList ID must be positive')
    gql = '''query ($id: Int!) {
      Media(id: $id) {
        id siteUrl type format status episodes chapters volumes
        title { english romaji native }
        description(asHtml: false)
        startDate { year month day }
        endDate { year month day }
        nextAiringEpisode { airingAt episode timeUntilAiring }
        relations {
          edges {
            relationType
            node {
              id siteUrl type format status
              title { english romaji native }
              startDate { year month day }
            }
          }
        }
      }
    }'''
    request = urllib.request.Request(
        ANILIST_URL,
        data=json.dumps({'query': gql, 'variables': {'id': media_id}}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'MediaIntakePrototype/0.2'}, method='POST')
    with open_url(request, timeout=30) as response:
        raw, final_url = response.read(3_000_001), response.geturl()
    if len(raw) > 3_000_000 or urllib.parse.urlsplit(final_url).hostname != 'graphql.anilist.co':
        raise ValueError('AniList returned an unexpected response')
    payload = json.loads(raw.decode('utf-8'))
    if payload.get('errors'):
        messages = [str(error.get('message') or 'AniList query failed')
                    for error in payload['errors']]
        raise ValueError('; '.join(messages[:2]))
    result = (payload.get('data') or {}).get('Media')
    if not isinstance(result, dict) or int(result.get('id') or 0) != media_id:
        raise ValueError('AniList did not return that media record')
    return result


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
    with open_url(request, timeout=30) as response:
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


def search_comicvine(query, api_key, limit=20):
    """Search Comic Vine's issue-series catalog. The API key is used only in memory."""
    query = _query_text(query)
    api_key = str(api_key or '').strip()
    if not api_key or len(api_key) > 500:
        raise ValueError('Set COMICVINE_API_KEY or enter a Comic Vine API key')
    limit = max(1, min(int(limit), 50))
    params = urllib.parse.urlencode({
        'api_key': api_key, 'format': 'json', 'resources': 'volume',
        'query': query, 'limit': limit,
    })
    request = urllib.request.Request(
        COMICVINE_API + '/search/?' + params,
        headers={'Accept': 'application/json', 'User-Agent': 'MediaIntakePrototype/0.3'},
    )
    with open_url(request, timeout=30) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Comic Vine response exceeds size limit')
    payload = json.loads(raw.decode('utf-8'))
    if payload.get('error') not in (None, 'OK'):
        raise ValueError('Comic Vine search failed: ' + str(payload.get('error'))[:200])
    results = []
    for row in (payload.get('results') or [])[:limit]:
        volume_id = row.get('id')
        title = str(row.get('name') or '').strip()
        if not volume_id or not title:
            continue
        source_url = row.get('site_detail_url') or f'https://comicvine.gamespot.com/{volume_id}/'
        parts = urllib.parse.urlsplit(source_url)
        if parts.scheme != 'https' or parts.hostname not in ('comicvine.gamespot.com', 'www.comicvine.gamespot.com'):
            continue
        results.append({
            'provider': 'Comic Vine', 'kind': 'comic', 'title': title,
            'author': 'Unknown', 'source_url': source_url,
            'comicvine_id': str(volume_id), 'start_year': row.get('start_year'),
            'issue_count': row.get('count_of_issues'), 'format': 'catalog',
            'publisher': (row.get('publisher') or {}).get('name') if isinstance(row.get('publisher'), dict) else row.get('publisher'),
            'cover': (row.get('image') or {}).get('small_url') if isinstance(row.get('image'), dict) else None,
        })
    return results


def list_comicvine_issues(volume_id, api_key, limit=100, number=None):
    """List the most recent scheduled issues in a volume, including store dates."""
    volume_id = str(volume_id or '').strip()
    api_key = str(api_key or '').strip()
    if not re.fullmatch(r'\d{1,12}', volume_id):
        raise ValueError('Invalid Comic Vine volume ID')
    if not api_key or len(api_key) > 500:
        raise ValueError('Set COMICVINE_API_KEY or enter a Comic Vine API key')
    if number is not None and not re.fullmatch(r'\d+(?:\.\d+)?[a-zA-Z]?', str(number)):
        raise ValueError('Invalid Comic Vine issue number')
    limit = max(1, min(int(limit), 100))
    params = urllib.parse.urlencode({
        'api_key': api_key, 'format': 'json', 'filter': f'volume:{volume_id}' + (f',issue_number:{number}' if number is not None else ''),
        'sort': 'store_date:desc', 'limit': limit,
        'field_list': 'id,name,issue_number,store_date,cover_date,site_detail_url,image',
    })
    request = urllib.request.Request(
        COMICVINE_API + '/issues/?' + params,
        headers={'Accept': 'application/json', 'User-Agent': 'MediaIntakePrototype/0.3'},
    )
    with open_url(request, timeout=30) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError('Comic Vine issue response exceeds size limit')
    payload = json.loads(raw.decode('utf-8'))
    if payload.get('error') not in (None, 'OK'):
        raise ValueError('Comic Vine issue lookup failed: ' + str(payload.get('error'))[:200])
    issues = []
    for row in payload.get('results') or []:
        issue_id = row.get('id')
        if issue_id is None:
            continue
        store_date = str(row.get('store_date') or '').strip() or None
        cover_date = str(row.get('cover_date') or '').strip() or None
        title = str(row.get('name') or '').strip()
        number = str(row.get('issue_number') or '').strip()
        if not title:
            title = f'Issue #{number}' if number else 'Comic issue'
        issues.append({
            'provider': 'Comic Vine', 'comicvine_id': str(issue_id),
            'title': title, 'number': number, 'store_date': store_date,
            'cover_date': cover_date, 'release_date': store_date or cover_date,
            'cover': (row.get('image') or {}).get('small_url') if isinstance(row.get('image'), dict) else None,
            'source_url': row.get('site_detail_url') or f'https://comicvine.gamespot.com/issue/{issue_id}/',
        })
    return issues


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
