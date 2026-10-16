"""Torrent index connectors: The Pirate Bay, 1337x, RuTracker, Nyaa, YTS, EZTVx and FitGirl Repacks.

Each search returns normalized torrent rows that queue for the native torrent
engine (torrents.py). Where a listing does not carry the magnet (1337x and
RuTracker list pages), the row keeps the release page and the magnet is read
when the item starts downloading (``resolve_torrent``), so searching stays fast.
"""
import datetime as dt
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .base import SearchPage, SourceError, human_size, json_body, parse_size, request
from .html import parse_html
from .releases import magnet_link, normalize_info_hash, parse_release, quality_label, video_subtype

CATEGORY_KINDS = {
    'Books': 'book', 'Light novels': 'lightnovel', 'Comics': 'comic', 'Manga': 'manga',
    'Anime': 'anime', 'Videos': 'video', 'Games': 'game', 'Software': 'software',
    'Web serials': 'webfiction', 'Fanfiction': 'fanfiction',
}


def torrent_row(spec, category, *, name, magnet='', torrent_url='', info_hash='', size=None, seeders=None,
                peers=None, published='', details_url='', source_id='', extra=None, needs_resolve=False):
    info = parse_release(name)
    info_hash = normalize_info_hash(info_hash)
    if not magnet and info_hash:
        magnet = magnet_link(info_hash, name)
    url = magnet or torrent_url or details_url
    quality = quality_label(info)
    seeds = f'{seeders} seeders' if seeders is not None else 'seeders unknown'
    size_text = human_size(size)
    row = {
        'title': info['title'], 'release_title': name, 'author': 'Unknown', 'category': category,
        'kind': CATEGORY_KINDS.get(category, 'video'), 'url': url, 'format': '.torrent',
        'magnet': magnet, 'torrent_url': torrent_url, 'info_hash': info_hash, 'size': size,
        'seeders': seeders, 'peers': peers, 'published': published, 'details_url': details_url,
        'source': details_url or spec.label, 'source_key': spec.key, 'provider': spec.label,
        'source_role': 'download', 'downloadable': True, 'external_download': True,
        'transfer': 'torrent', 'quality': quality, 'source_id': str(source_id or ''),
        'display_source': ' · '.join(part for part in ('Torrent', spec.label, seeds, size_text) if part),
    }
    for key in ('year', 'season', 'episode', 'episode_end', 'resolution', 'video_source', 'codec',
                'file_format', 'platform', 'release_group'):
        if info.get(key) not in (None, ''):
            row[key] = info[key]
    if info.get('year'):
        row['release_year'] = info['year']
    if category in ('Videos', 'Anime'):
        row['video_subtype'] = video_subtype(info)
        if category == 'Anime':
            row['kind'] = 'anime'
    if needs_resolve:
        row['resolve_connector'] = spec.key
    row.update(extra or {})
    return row


def _keep_for_category(row, category):
    """Drop obvious cross-category noise from general indexes."""
    title = str(row.get('release_title') or '').casefold()
    if category in ('Books', 'Light novels', 'Comics', 'Manga'):
        return not row.get('resolution') and not row.get('season')
    if category == 'Games':
        return not row.get('resolution') and not row.get('episode')
    if category == 'Software':
        return not row.get('resolution') and 'season' not in title
    return True


# --------------------------------------------------------------------------- The Pirate Bay
TPB_CATEGORIES = {
    'Videos': '200', 'Anime': '200', 'Games': '400', 'Software': '300',
    'Books': '601', 'Light novels': '601', 'Comics': '602', 'Manga': '602',
}


def search_tpb(spec, query, category, page=1, config=None, opener=None):
    """The Pirate Bay's JSON API (apibay): one page of up to 100 results, no paging."""
    base = (config or {}).get('base')
    params = {'q': query, 'cat': TPB_CATEGORIES.get(category, '0')}
    body, _url, _headers = request(spec.label, f'{base}/q.php?{urllib.parse.urlencode(params)}', opener=opener,
                                   headers={'Accept': 'application/json'})
    data = json_body(spec.label, body)
    if not isinstance(data, list):
        raise SourceError(spec.label, 'the search API sent an unexpected reply.', 'invalid')
    site = (config or {}).get('site') or 'https://thepiratebay.org'
    rows = []
    for hit in data:
        if not isinstance(hit, dict) or str(hit.get('id')) == '0' or not normalize_info_hash(hit.get('info_hash')):
            continue  # apibay answers "No results returned" with id 0 and an all-zero hash
        if set(str(hit.get('info_hash'))) == {'0'}:
            continue
        added = ''
        try:
            added = dt.datetime.fromtimestamp(int(hit.get('added') or 0), dt.timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        row = torrent_row(spec, category, name=hit.get('name') or 'Untitled', info_hash=hit.get('info_hash'),
                          size=_int(hit.get('size')), seeders=_int(hit.get('seeders')),
                          peers=_int(hit.get('leechers')), published=added, source_id=hit.get('id'),
                          details_url=f"{site}/description.php?id={hit.get('id')}",
                          extra={'uploader': hit.get('username') or '', 'uploader_status': hit.get('status') or '',
                                 'imdb_id': hit.get('imdb') or '', 'file_count': _int(hit.get('num_files'))})
        if _keep_for_category(row, category):
            rows.append(row)
    rows.sort(key=lambda row: -(row['seeders'] or 0))
    return SearchPage(rows, page, False, 'direct')


# --------------------------------------------------------------------------- 1337x
X1337_CATEGORIES = {'Videos': 'Movies', 'Anime': 'Anime', 'Games': 'Games', 'Software': 'Apps',
                    'Books': 'Other', 'Light novels': 'Other', 'Comics': 'Other', 'Manga': 'Anime'}


def search_1337x(spec, query, category, page=1, config=None, opener=None):
    base = (config or {}).get('base')
    quoted = urllib.parse.quote(query)
    section = X1337_CATEGORIES.get(category)
    path = (f'/category-search/{quoted}/{section}/{page}/' if section else f'/search/{quoted}/{page}/')
    body, _url, _headers = request(spec.label, base + path, opener=opener)
    tree = parse_html(body)
    rows = []
    for tr in tree.find_all('tr'):
        name_cell = tr.find('td', class_='name')
        if name_cell is None:
            continue
        link = next((a for a in name_cell.find_all('a') if a.get('href').startswith('/torrent/')), None)
        if link is None:
            continue
        seeds = tr.find('td', class_='seeds')
        leeches = tr.find('td', class_='leeches')
        size_cell = tr.find('td', class_='size')
        size_text = ''
        if size_cell is not None:
            size_text = ''.join(child for child in size_cell.children if isinstance(child, str))
        date_cell = tr.find('td', class_='coll-date')
        match = re.match(r'/torrent/(\d+)/', link.get('href'))
        details = urllib.parse.urljoin(base + '/', link.get('href'))
        rows.append(torrent_row(
            spec, category, name=link.text(), details_url=details, size=parse_size(size_text),
            seeders=_int(seeds.text() if seeds is not None else None),
            peers=_int(leeches.text() if leeches is not None else None),
            published=date_cell.text() if date_cell is not None else '',
            source_id=match.group(1) if match else details, needs_resolve=True))
    if not rows and b'no results were returned' not in body.casefold() and b'search-page' not in body:
        if b'<table' not in body:
            raise SourceError(spec.label, 'the search page did not contain a results table.', 'invalid',
                              'The site may have changed or the address may point to an impostor mirror.')
    pagination = tree.find('div', class_='pagination')
    has_more = False
    if pagination is not None:
        has_more = any(a.text().strip() in ('>>', 'Last', '»') or a.text().strip() == str(page + 1)
                       for a in pagination.find_all('a'))
    rows = [row for row in rows if _keep_for_category(row, category)]
    return SearchPage(rows, page, has_more, 'direct')


def resolve_1337x(spec, item, config=None, opener=None):
    body, _url, _headers = request(spec.label, item['details_url'], opener=opener)
    match = re.search(rb'href="(magnet:\?xt=urn:btih:[^"]+)"', body)
    if not match:
        raise SourceError(spec.label, 'the release page has no magnet link (it may have been removed).',
                          'invalid', 'Choose another release.')
    return {'url': match.group(1).decode('utf-8', 'replace').replace('&amp;', '&')}


# --------------------------------------------------------------------------- RuTracker
RUTRACKER_FORUM_HINTS = {
    'Videos': ('фильм', 'кино', 'сериал', 'movie', 'film', 'tv', 'hd video', 'документ', 'uhd'),
    'Anime': ('аниме', 'anime'),
    'Games': ('игр', 'game', 'консол', 'nintendo', 'playstation', 'xbox'),
    'Software': ('программ', 'софт', 'software', 'windows', 'linux', 'macos', 'операционн'),
    'Books': ('книг', 'литератур', 'book', 'журнал', 'аудиокниг', 'учебн'),
    'Light novels': ('книг', 'литератур', 'book', 'ранобэ'),
    'Comics': ('комикс', 'comic', 'манга', 'manga'),
    'Manga': ('манга', 'manga', 'комикс'),
}


def rutracker_login(spec, config, opener):
    username, password = (config.get('username') or '').strip(), config.get('password') or ''
    if not username or not password:
        raise SourceError(spec.label, 'searching needs a RuTracker account.', 'config',
                          'Enter your RuTracker username and password in Settings → Sources.')
    form = {'login_username': username.encode('cp1251', 'replace'),
            'login_password': password.encode('cp1251', 'replace'), 'login': 'вход'.encode('cp1251')}
    body, _url, headers = request(spec.label, f"{config['base']}/forum/login.php",
                                  data=urllib.parse.urlencode(form).encode('ascii'), opener=opener,
                                  headers={'Content-Type': 'application/x-www-form-urlencoded'},
                                  follow_redirects=False)
    cookies = [value.split(';', 1)[0] for value in headers.get_all('Set-Cookie') or []]
    session = next((cookie for cookie in cookies if cookie.startswith('bb_session=')), '')
    if not session:
        if 'cap_sid'.encode() in body or 'captcha'.encode() in body.casefold():
            raise SourceError(spec.label, 'RuTracker asked for a CAPTCHA before signing in.', 'auth',
                              'Sign in once in your browser to clear it, then search again.')
        raise SourceError(spec.label, 'RuTracker did not accept the username or password.', 'auth',
                          'Check them in Settings → Sources.')
    return '; '.join(cookies)


def search_rutracker(spec, query, category, page=1, config=None, opener=None):
    config = dict(config or {})
    cookie = config.get('cookie') or rutracker_login(spec, config, opener)
    config['cookie'] = cookie
    start = (max(1, page) - 1) * 50
    url = f"{config['base']}/forum/tracker.php?{urllib.parse.urlencode({'nm': query, 'start': start})}"
    body, final_url, _headers = request(spec.label, url, opener=opener, headers={'Cookie': cookie})
    text = body.decode('cp1251', 'replace') if b'charset=windows-1251' in body[:3000].lower() else \
        body.decode('utf-8', 'replace')
    if 'login.php' in final_url or 'login_username' in text and 'tor-tbl' not in text:
        raise SourceError(spec.label, 'RuTracker signed the app out before searching.', 'auth',
                          'Check your username and password in Settings → Sources.')
    tree = parse_html(text)
    hints = RUTRACKER_FORUM_HINTS.get(category, ())
    rows = []
    for tr in tree.find_all('tr', class_='hl-tr'):
        title = tr.find('a', class_='tLink')
        if title is None:
            continue
        topic_id = title.get('data-topic_id') or (re.search(r't=(\d+)', title.get('href')) or [None, ''])[1]
        forum = tr.find('a', class_='f')
        forum_name = forum.text() if forum is not None else ''
        if hints and not any(hint in forum_name.casefold() for hint in hints):
            continue
        size_cell = tr.find('td', class_='tor-size')
        size = _int(size_cell.get('data-ts_text')) if size_cell is not None else None
        if size is None and size_cell is not None:
            size = parse_size(size_cell.text().replace('\xa0', ' '))
        seeds = tr.find('b', class_='seedmed')
        leech = tr.find('td', class_='leechmed')
        added = tr.find('td', class_='row4 small nowrap')
        rows.append(torrent_row(
            spec, category, name=title.text(), details_url=f"{config['base']}/forum/viewtopic.php?t={topic_id}",
            size=size, seeders=_int(seeds.text() if seeds is not None else None),
            peers=_int(leech.text() if leech is not None else None),
            published=added.text() if added is not None else '', source_id=topic_id, needs_resolve=True,
            extra={'forum': forum_name}))
    if not rows and 'tor-tbl' not in text:
        raise SourceError(spec.label, 'the search page did not contain a results table.', 'invalid',
                          'RuTracker may have changed its page layout.')
    pages = tree.find('a', class_='pg')
    has_more = bool(pages) and f'start={start + 50}'.encode() in body
    return SearchPage(rows, page, has_more, 'direct')


def resolve_rutracker(spec, item, config=None, opener=None):
    """RuTracker's public API gives a topic's info hash without signing in."""
    config = config or {}
    topic = str(item.get('source_id') or '')
    api = config.get('api_base') or 'https://api.rutracker.cc'
    try:
        body, _url, _headers = request(spec.label, f'{api}/v1/get_tor_hash?by=topic_id&val={topic}',
                                       opener=opener)
        info_hash = (json_body(spec.label, body).get('result') or {}).get(topic)
    except SourceError:
        info_hash = None
    if not info_hash:
        cookie = config.get('cookie') or rutracker_login(spec, dict(config), opener)
        body, _url, _headers = request(spec.label, item['details_url'], opener=opener, headers={'Cookie': cookie})
        match = re.search(rb'href="(magnet:\?xt=urn:btih:[^"]+)"', body)
        if not match:
            raise SourceError(spec.label, 'the topic has no magnet link (it may have been closed or replaced).',
                              'invalid', 'Choose another release.')
        return {'url': match.group(1).decode('utf-8', 'replace').replace('&amp;', '&')}
    return {'url': magnet_link(info_hash, item.get('release_title') or item.get('title')),
            'info_hash': normalize_info_hash(info_hash)}


# --------------------------------------------------------------------------- Nyaa
NYAA_NS = '{https://nyaa.si/xmlns/nyaa}'
NYAA_CATEGORIES = {'Anime': '1_0', 'Manga': '3_0', 'Light novels': '3_0', 'Books': '3_0',
                   'Software': '6_1', 'Games': '6_2', 'Videos': '4_0'}
NYAA_PAGE_SIZE = 75


def search_nyaa(spec, query, category, page=1, config=None, opener=None):
    config = config or {}
    params = {'page': 'rss', 'q': query, 'c': NYAA_CATEGORIES.get(category, '0_0'),
              'f': config.get('filter') or '0'}
    if page > 1:
        params['p'] = str(page)
    body, _url, _headers = request(spec.label, f"{config['base']}/?{urllib.parse.urlencode(params)}",
                                   opener=opener, headers={'Accept': 'application/rss+xml'})
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise SourceError(spec.label, 'the RSS feed could not be read.', 'invalid',
                          'The address may point to a mirror that is not a Nyaa site.') from exc
    rows = []
    for item in root.iter('item'):
        name = (item.findtext('title') or '').strip()
        info_hash = item.findtext(f'{NYAA_NS}infoHash') or ''
        if not name or not normalize_info_hash(info_hash):
            continue
        details = (item.findtext('guid') or '').strip()
        size_text = item.findtext(f'{NYAA_NS}size') or ''
        rows.append(torrent_row(
            spec, category, name=name, info_hash=info_hash, torrent_url=(item.findtext('link') or '').strip(),
            size=parse_size(size_text), seeders=_int(item.findtext(f'{NYAA_NS}seeders')),
            peers=_int(item.findtext(f'{NYAA_NS}leechers')), published=(item.findtext('pubDate') or '').strip(),
            details_url=details, source_id=details.rsplit('/', 1)[-1],
            extra={'nyaa_category': item.findtext(f'{NYAA_NS}category') or '',
                   'trusted': (item.findtext(f'{NYAA_NS}trusted') or '').casefold() == 'yes',
                   'remake': (item.findtext(f'{NYAA_NS}remake') or '').casefold() == 'yes'}))
    for row in rows:
        if category in ('Manga', 'Light novels', 'Books'):
            row['kind'] = CATEGORY_KINDS[category]
    return SearchPage(rows, page, len(rows) >= NYAA_PAGE_SIZE, 'direct')


# --------------------------------------------------------------------------- YTS
def search_yts(spec, query, category, page=1, config=None, opener=None):
    if category not in ('Videos', None):
        return SearchPage([], page, False, 'direct')
    params = {'query_term': query, 'page': page, 'limit': 20, 'sort_by': 'seeds'}
    body, _url, _headers = request(spec.label, f"{config['base']}/api/v2/list_movies.json?"
                                   f"{urllib.parse.urlencode(params)}", opener=opener,
                                   headers={'Accept': 'application/json'})
    data = json_body(spec.label, body)
    if not isinstance(data, dict) or data.get('status') != 'ok':
        raise SourceError(spec.label, f"the API reported a problem: {data.get('status_message') or 'unknown'}",
                          'invalid')
    payload = data.get('data') or {}
    rows = []
    for movie in payload.get('movies') or []:
        for torrent in movie.get('torrents') or []:
            quality = ' '.join(str(torrent.get(key) or '') for key in ('quality', 'type', 'video_codec')).strip()
            name = f"{movie.get('title_long') or movie.get('title')} [{quality}]"
            row = torrent_row(
                spec, 'Videos', name=name, info_hash=torrent.get('hash'), torrent_url=torrent.get('url') or '',
                size=_int(torrent.get('size_bytes')), seeders=_int(torrent.get('seeds')),
                peers=_int(torrent.get('peers')), details_url=movie.get('url') or '',
                source_id=torrent.get('hash'),
                extra={'imdb_id': movie.get('imdb_code') or '', 'video_subtype': 'movie',
                       'rating': movie.get('rating'), 'genres': movie.get('genres') or []})
            row['title'] = movie.get('title') or row['title']
            if movie.get('year'):
                row['year'] = row['release_year'] = int(movie['year'])
            row['quality'] = quality_label({'resolution': torrent.get('quality'),
                                            'video_source': torrent.get('type'),
                                            'codec': torrent.get('video_codec')})
            rows.append(row)
    total = _int(payload.get('movie_count')) or 0
    return SearchPage(rows, page, page * int(payload.get('limit') or 20) < total, 'direct')


# --------------------------------------------------------------------------- EZTVx
def search_eztvx(spec, query, category, page=1, config=None, opener=None):
    if category not in ('Videos', None):
        return SearchPage([], page, False, 'direct')
    base = config['base']
    imdb = re.fullmatch(r'(?:tt)?(\d{5,9})', query.strip())
    if imdb:
        params = {'imdb_id': imdb.group(1), 'limit': 100, 'page': page}
        body, _url, _headers = request(spec.label, f'{base}/api/get-torrents?{urllib.parse.urlencode(params)}',
                                       opener=opener, headers={'Accept': 'application/json'})
        data = json_body(spec.label, body)
        rows = [torrent_row(spec, 'Videos', name=hit.get('title') or hit.get('filename') or 'Untitled',
                            magnet=hit.get('magnet_url') or '', info_hash=hit.get('hash'),
                            torrent_url=hit.get('torrent_url') or '', size=_int(hit.get('size_bytes')),
                            seeders=_int(hit.get('seeds')), peers=_int(hit.get('peers')),
                            details_url=hit.get('episode_url') or '', source_id=hit.get('id'),
                            extra={'imdb_id': 'tt' + str(hit.get('imdb_id') or '').lstrip('t')})
                for hit in (data.get('torrents') or []) if isinstance(hit, dict)]
        total = _int(data.get('torrents_count')) or 0
        return SearchPage(rows, page, page * 100 < total, 'direct')
    slug = re.sub(r'[^a-z0-9]+', '-', query.casefold()).strip('-')
    body, _url, _headers = request(spec.label, f'{base}/search/{urllib.parse.quote(slug)}', opener=opener)
    tree = parse_html(body)
    rows = []
    for tr in tree.find_all('tr', class_='forum_header_border'):
        title = tr.find('a', class_='epinfo')
        magnet = tr.find('a', class_='magnet')
        if title is None or magnet is None:
            continue
        cells = [td.text() for td in tr.find_all('td', class_='forum_thread_post')]
        seeds = tr.find('td', class_='forum_thread_post_end')
        size = next((parse_size(cell) for cell in cells if parse_size(cell)), None)
        torrent = tr.find('a', class_='download_1')
        rows.append(torrent_row(spec, 'Videos', name=title.get('title') or title.text(), magnet=magnet.get('href'),
                                torrent_url=torrent.get('href') if torrent is not None else '', size=size,
                                seeders=_int(seeds.text() if seeds is not None else None),
                                details_url=urllib.parse.urljoin(base + '/', title.get('href')),
                                source_id=title.get('href')))
    for row in rows:
        if row['magnet']:
            from .releases import info_hash_from_magnet
            row['info_hash'] = info_hash_from_magnet(row['magnet'])
    return SearchPage(rows, page, False, 'direct')


# --------------------------------------------------------------------------- FitGirl Repacks
def search_fitgirl(spec, query, category, page=1, config=None, opener=None):
    if category not in ('Games', None):
        return SearchPage([], page, False, 'direct')
    params = {'search': query, 'page': page, 'per_page': 20, '_fields': 'id,link,title,date,content'}
    url = f"{config['base']}/wp-json/wp/v2/posts?{urllib.parse.urlencode(params)}"
    try:
        body, _url, headers = request(spec.label, url, opener=opener, headers={'Accept': 'application/json'})
    except SourceError as exc:
        if 'HTTP 400' in str(exc) and page > 1:
            return SearchPage([], page, False, 'direct')  # WordPress answers past the last page with 400
        raise
    posts = json_body(spec.label, body)
    if not isinstance(posts, list):
        raise SourceError(spec.label, 'the site search sent an unexpected reply.', 'invalid')
    rows = []
    for post in posts:
        html = str((post.get('content') or {}).get('rendered') or '')
        magnets = re.findall(r'href="(magnet:\?xt=urn:btih:[^"]+)"', html)
        if not magnets:
            continue  # update digests and news posts carry no release
        title = parse_html(str((post.get('title') or {}).get('rendered') or '')).text()
        text = parse_html(html).text()
        repack = re.search(r'Repack Size:\s*(?:from\s*)?([\d.,]+\s*[KMGT]B)', text, re.I)
        original = re.search(r'Original Size:\s*([\d.,]+\s*[KMGT]B)', text, re.I)
        genres = re.search(r'Genres/Tags:\s*(.+?)(?:Compan|Languages|Original Size)', text)
        magnet = magnets[0].replace('&amp;', '&').replace('&#038;', '&')
        from .releases import info_hash_from_magnet
        name = re.sub(r'\s*[–-]\s*v[\d.]+.*$', '', title).strip() or title
        row = torrent_row(spec, 'Games', name=title, magnet=magnet, info_hash=info_hash_from_magnet(magnet),
                          size=parse_size(repack.group(1)) if repack else None, details_url=post.get('link') or '',
                          published=str(post.get('date') or '')[:10], source_id=post.get('id'),
                          extra={'original_size': original.group(1) if original else '',
                                 'genres': [g.strip() for g in genres.group(1).split(',')] if genres else [],
                                 'kind': 'game', 'platform': 'PC'})
        row['title'] = name
        row['quality'] = 'PC repack' + (f" · installs to {original.group(1)}" if original else '')
        row['display_source'] = row['display_source'].replace('seeders unknown', 'repack')
        rows.append(row)
    total_pages = _int(headers.get('X-WP-TotalPages')) or page
    return SearchPage(rows, page, page < total_pages, 'direct')


def _int(value):
    try:
        return int(float(str(value).replace(',', '').strip()))
    except (TypeError, ValueError):
        return None
