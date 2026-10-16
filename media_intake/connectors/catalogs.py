"""Metadata and discovery-only sources: AniDB, WorldCat and AIO Search.

None of these provide files. AniDB and WorldCat describe works (anime and
library holdings); AIO Search is a launcher that runs other sites' searches in
a browser, so it becomes one "open in browser" row.
"""
import base64
import gzip
import os
from pathlib import Path
import re
import threading
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET

from .base import SearchPage, SourceError, json_body, request

XML_LANG = '{http://www.w3.org/XML/1998/namespace}lang'


def _fold(text):
    text = unicodedata.normalize('NFKD', str(text or '').casefold())
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in text if not unicodedata.combining(c))))


# --------------------------------------------------------------------------- AniDB
ANIDB_DUMP_URL = 'https://anidb.net/api/anime-titles.xml.gz'
ANIDB_DUMP_MAX_AGE = 36 * 3600   # AniDB bans clients that fetch the dump more than once a day
ANIDB_DETAIL_MAX_AGE = 7 * 86400
_ANIDB_LOCK = threading.Lock()
_ANIDB_INDEX = {}
_ANIDB_LAST_API = [0.0]
ANIDB_TYPES = {'tv series': 'anime_series', 'movie': 'anime_movie', 'ova': 'anime_ova', 'web': 'anime_web_series',
               'tv special': 'anime_special', 'music video': 'anime_music_video'}


def _anidb_titles(spec, config, opener):
    cache = Path(config.get('cache_dir') or '.') / 'anidb-anime-titles.xml.gz'
    with _ANIDB_LOCK:
        if cache.exists() and time.time() - cache.stat().st_mtime < ANIDB_DUMP_MAX_AGE:
            stamp = cache.stat().st_mtime
            if _ANIDB_INDEX.get('stamp') == stamp:
                return _ANIDB_INDEX['titles']
            data = cache.read_bytes()
        else:
            data, _url, _headers = request(spec.label, config.get('dump_url') or ANIDB_DUMP_URL, opener=opener,
                                           max_bytes=40_000_000, timeout=60)
            if data[:2] != b'\x1f\x8b':
                data = gzip.compress(data)
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix('.tmp')
            temporary.write_bytes(data)
            os.replace(temporary, cache)
            stamp = cache.stat().st_mtime
        try:
            root = ET.fromstring(gzip.decompress(data))
        except (OSError, ET.ParseError) as exc:
            cache.unlink(missing_ok=True)
            raise SourceError(spec.label, 'the title list could not be read.', 'invalid',
                              'It will be downloaded again on the next search.') from exc
        titles = []
        for anime in root.iter('anime'):
            names = [(title.get('type'), title.get(XML_LANG), (title.text or '').strip())
                     for title in anime.findall('title')]
            titles.append((anime.get('aid'), names))
        _ANIDB_INDEX.update(stamp=stamp, titles=titles)
        return titles


def _anidb_details(spec, aid, config, opener):
    client = str(config.get('client') or '').strip()
    if not client:
        return None
    cache = Path(config.get('cache_dir') or '.') / 'anidb' / f'{aid}.xml'
    if cache.exists() and time.time() - cache.stat().st_mtime < ANIDB_DETAIL_MAX_AGE:
        body = cache.read_bytes()
    else:
        with _ANIDB_LOCK:
            wait = _ANIDB_LAST_API[0] + 2.1 - time.monotonic()   # AniDB allows one request every two seconds
            if wait > 0:
                time.sleep(wait)
            params = {'request': 'anime', 'client': client, 'clientver': config.get('clientver') or '1',
                      'protover': '1', 'aid': aid}
            try:
                body, _url, _headers = request(spec.label, f"{config.get('api_base') or 'http://api.anidb.net:9001'}"
                                               f"/httpapi?{urllib.parse.urlencode(params)}", opener=opener)
            finally:
                _ANIDB_LAST_API[0] = time.monotonic()
        if body.lstrip().startswith(b'<error'):
            message = ET.fromstring(body).text or 'unknown error'
            kind = 'auth' if 'client' in message.casefold() else 'limit' if 'banned' in message.casefold() \
                else 'invalid'
            raise SourceError(spec.label, f'the details API said "{message}".', kind,
                              'Register a client name at anidb.net and enter it in Settings → Sources.'
                              if kind == 'auth' else 'Wait a while before asking AniDB again.')
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)
    root = ET.fromstring(body)
    return {
        'type': root.findtext('type') or '', 'episodes': root.findtext('episodecount') or '',
        'start': root.findtext('startdate') or '', 'end': root.findtext('enddate') or '',
        'description': ' '.join((root.findtext('description') or '').split())[:600],
        'rating': (root.find('ratings/permanent').text if root.find('ratings/permanent') is not None else ''),
        'picture': root.findtext('picture') or '',
    }


def search_anidb(spec, query, category, page=1, config=None, opener=None):
    config = config or {}
    wanted = _fold(query)
    if not wanted:
        return SearchPage([], page)
    scored = []
    for aid, names in _anidb_titles(spec, config, opener):
        best = 0
        for kind, _lang, text in names:
            folded = _fold(text)
            if folded == wanted:
                score = 100 if kind in ('main', 'official') else 90
            elif folded.startswith(wanted + ' ') or folded.startswith(wanted):
                score = 70 - min(20, len(folded) - len(wanted)) / 2
            elif wanted in folded:
                score = 50 - min(20, len(folded) - len(wanted)) / 2
            else:
                continue
            best = max(best, score)
        if best:
            scored.append((-best, int(aid), aid, names))
    scored.sort()
    start = (page - 1) * 25
    rows = []
    for rank, (_score, _num, aid, names) in enumerate(scored[start:start + 25]):
        main = next((text for kind, _lang, text in names if kind == 'main'), names[0][2])
        english = next((text for kind, lang, text in names if kind == 'official' and lang == 'en'), '')
        row = {
            'title': english or main, 'author': 'Unknown', 'category': 'Anime', 'kind': 'anime',
            'provider': spec.label, 'source_key': spec.key, 'source_role': 'metadata', 'format': 'catalog',
            'discovery_only': True, 'downloadable': False, 'url': f'https://anidb.net/anime/{aid}',
            'source_url': f'https://anidb.net/anime/{aid}', 'source': spec.label,
            'alternative_titles': list(dict.fromkeys(text for _kind, _lang, text in names if text)),
            'external_ids': [{'provider': 'anidb', 'external_id': aid}], 'video_subtype': 'anime',
            'catalog_metadata': {'genres': ['anime'], 'anidb_aid': aid},
            'display_source': f'Metadata · {spec.label}',
        }
        if rank < 3 and page == 1:
            try:
                details = _anidb_details(spec, aid, config, opener)
            except SourceError:
                details = None  # titles still help; the error would hide every match
            if details:
                row['release_date'] = details['start']
                row['video_subtype'] = ANIDB_TYPES.get(details['type'].casefold(), 'anime')
                row['description'] = details['description']
                row['catalog_metadata'].update(episodes=details['episodes'], rating=details['rating'],
                                               end_date=details['end'], anidb_type=details['type'])
        rows.append(row)
    return SearchPage(rows, page, start + 25 < len(scored), 'direct')


# --------------------------------------------------------------------------- WorldCat
WORLDCAT_TOKEN_URL = 'https://oauth.oclc.org/token'
WORLDCAT_SEARCH_URL = 'https://americas.discovery.api.oclc.org/worldcat/search/v2/bibs'
_WORLDCAT_TOKENS = {}


def _worldcat_token(spec, config, opener):
    key, secret = str(config.get('client_id') or '').strip(), str(config.get('client_secret') or '').strip()
    cached = _WORLDCAT_TOKENS.get(key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    basic = base64.b64encode(f'{key}:{secret}'.encode()).decode('ascii')
    body, _url, _headers = request(spec.label, f"{config.get('token_url') or WORLDCAT_TOKEN_URL}?"
                                   'grant_type=client_credentials&scope=wcapi', data=b'', opener=opener,
                                   headers={'Authorization': f'Basic {basic}', 'Accept': 'application/json'})
    data = json_body(spec.label, body)
    if not data.get('access_token'):
        raise SourceError(spec.label, 'OCLC did not accept the API key and secret.', 'auth',
                          'Check the WorldCat Search API credentials in Settings → Sources.')
    _WORLDCAT_TOKENS[key] = (data['access_token'], time.time() + int(data.get('expires_in') or 1200))
    return data['access_token']


def search_worldcat(spec, query, category, page=1, config=None, opener=None):
    config = config or {}
    if not (config.get('client_id') and config.get('client_secret')):
        return SearchPage([browser_row(spec, query, category,
                                       'https://search.worldcat.org/search?q={query}',
                                       'Search WorldCat in your browser (add an OCLC API key in Settings → '
                                       'Sources to see results here)')], page, False, 'browser')
    token = _worldcat_token(spec, config, opener)
    limit = 50
    params = {'q': query, 'limit': limit, 'offset': (page - 1) * limit + 1, 'orderBy': 'bestMatch'}
    if category in ('Books', 'Light novels'):
        params['itemType'] = 'book'
    body, _url, _headers = request(spec.label, f"{config.get('search_url') or WORLDCAT_SEARCH_URL}?"
                                   f'{urllib.parse.urlencode(params)}', opener=opener,
                                   headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
    data = json_body(spec.label, body)
    rows = []
    for record in data.get('bibRecords') or []:
        identifier = record.get('identifier') or {}
        number = identifier.get('oclcNumber')
        titles = (record.get('title') or {}).get('mainTitles') or []
        title = (titles[0].get('text') if titles else '') or ''
        title = re.split(r'\s+/\s+', title)[0].strip()
        creators = (record.get('contributor') or {}).get('creators') or []
        authors = []
        for creator in creators:
            name = ' '.join(filter(None, ((creator.get('firstName') or {}).get('text'),
                                          (creator.get('secondName') or {}).get('text'))))
            name = name or (creator.get('nonPersonName') or {}).get('text') or ''
            if name:
                authors.append(name.strip(' ,.'))
        date = str((record.get('date') or {}).get('publicationDate') or '')
        year = re.search(r'\d{4}', date)
        fmt = record.get('format') or {}
        publisher = ((record.get('publishers') or [{}])[0].get('publisherName') or {}).get('text') or ''
        isbns = identifier.get('isbns') or []
        rows.append({
            'title': title or 'Untitled', 'author': ', '.join(authors[:3]) or 'Unknown', 'category': category,
            'kind': 'lightnovel' if category == 'Light novels' else 'book', 'provider': spec.label,
            'source_key': spec.key, 'source_role': 'metadata', 'format': 'catalog', 'discovery_only': True,
            'downloadable': False, 'url': f'https://search.worldcat.org/title/{number}',
            'source_url': f'https://search.worldcat.org/title/{number}', 'source': spec.label,
            'release_date': year.group(0) if year else '', 'publisher': publisher,
            'isbn': isbns[0] if isbns else '', 'isbns': isbns,
            'external_ids': [{'provider': 'oclc', 'external_id': number}],
            'catalog_metadata': {'oclc_number': number, 'general_format': fmt.get('generalFormat'),
                                 'specific_format': fmt.get('specificFormat'), 'isbn': isbns,
                                 'language': (record.get('language') or {}).get('itemLanguage')},
            'display_source': f"Metadata · {spec.label}" + (f" · {fmt.get('generalFormat')}"
                                                           if fmt.get('generalFormat') else ''),
        })
    total = int(data.get('numberOfRecords') or 0)
    return SearchPage(rows, page, page * limit < total, 'direct')


# --------------------------------------------------------------------------- AIO Search
AIO_SEARCH_TEMPLATE = 'https://www.aiosearch.com/?q={query}'


def browser_row(spec, query, category, template, label):
    url = template.replace('{query}', urllib.parse.quote(query))
    return {
        'title': f'{label}: “{query}”' if '{query}' not in label else label, 'author': 'Unknown',
        'category': category, 'provider': spec.label, 'source_key': spec.key, 'source_role': 'metadata',
        'format': 'link', 'discovery_only': True, 'downloadable': False, 'browser_search': True,
        'url': url, 'source_url': url, 'source': spec.label,
        'display_source': f'Browser search · {spec.label}',
    }


def search_aiosearch(spec, query, category, page=1, config=None, opener=None):
    template = str((config or {}).get('template') or AIO_SEARCH_TEMPLATE)
    if '{query}' not in template:
        raise SourceError(spec.label, 'the search address must contain {query}.', 'config',
                          'Fix it in Settings → Sources.')
    return SearchPage([browser_row(spec, query, category, template, 'Open AIO Search in your browser')],
                      page, False, 'browser',
                      'AIO Search runs other sites\' searches inside a browser page, so its results cannot be '
                      'listed here.')

