"""Issue-first comic catalog search and conservative edition reconciliation.

Metadata-only: no download approvals, source files, or credentials are persisted.
Provider identifiers distinguish series from issues, including Comic Vine's
historical "volume" resource (which is a series, not necessarily a trade).
"""
import copy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import difflib
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .webcomics.http import NoRedirect, retry_seconds

COLLECTION = re.compile(r'\b(?:TPB|trade\s*paperback|hardcover|paperback|omnibus|collected\s+(?:editions?|series)|'
    r'complete\s+collection|deluxe\s+edition|absolute\s+edition|HC)\b', re.I)
ISSUE = re.compile(r'(?:#|\bissue\s*(?:no\.?\s*)?)(\d+(?:\.\d+)?[a-z]?)\b', re.I)
ISSUE_RANGE = re.compile(r'#\d+(?:\.\d+)?\s*(?:[-–—&]|to)\s*#?\d+|#\d+.*?\b(?:complete\s+series|full\s+run)\b', re.I)
YEAR = re.compile(r'\((18\d{2}|19\d{2}|20\d{2})\)')
_GUARD = threading.Lock()
_HOSTS, _CACHE = {}, {}


def key(value):
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in text if not unicodedata.combining(c))))


def issue_number(value):
    value = str(value or '').strip().lstrip('#')
    try:
        return format(Decimal(value).normalize(), 'f') if value else ''
    except InvalidOperation:
        return value.casefold()


@dataclass(frozen=True)
class ComicQuery:
    title: str
    number: str = ''
    year: str = ''
    collections: bool = False


def parse_query(value):
    value = str(value or '').strip()
    if not value or len(value) > 150 or any(ord(c) < 32 for c in value):
        raise ValueError('Enter a comic title up to 150 characters')
    number = ISSUE.search(value)
    year = YEAR.search(value)
    title = ISSUE.sub('', YEAR.sub('', value)).strip(' -:')
    return ComicQuery(title or value, issue_number(number.group(1)) if number else '',
                      year.group(1) if year else '', bool(COLLECTION.search(value)))


def _memo(provider, identity, loader, interval=1):
    """Bounded session cache and source serialization; secrets become hashed keys."""
    cache_key = (provider, hashlib.sha256(str(identity).encode()).hexdigest())
    with _GUARD:
        state = _HOSTS.setdefault(provider, {'lock': threading.Lock(), 'last': 0, 'blocked': 0})
    with state['lock']:
        with _GUARD:
            cached = _CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        if state['blocked'] > time.monotonic():
            minutes = max(1, round((state['blocked'] - time.monotonic()) / 60))
            raise ValueError(f'{provider} is rate limited for about {minutes} more minute(s)')
        delay = state['last'] + interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            value = loader()
            with _GUARD:
                _CACHE[cache_key] = (time.monotonic() + 300, copy.deepcopy(value))
                while len(_CACHE) > 128:
                    del _CACHE[next(iter(_CACHE))]
            return value
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 429:
                wait = retry_seconds((exc.headers or {}).get('Retry-After')) or 60
                state['blocked'] = time.monotonic() + wait
                exc.close()
                raise ValueError(f'{provider} is rate limited for about {max(1, round(wait / 60))} minute(s)') from None
            exc.close()
            # HTTPError embeds the full Comic Vine query URL, including API key.
            raise ValueError(f'{provider} returned HTTP {code}; check credentials or retry later') from None
        finally:
            state['last'] = time.monotonic()


def _json(provider, url, headers=None, interval=1):
    parts = urllib.parse.urlsplit(url)
    expected = {'GCD': 'www.comics.org', 'Metron': 'metron.cloud'}[provider]
    if parts.scheme != 'https' or parts.hostname != expected or parts.username or parts.password:
        raise ValueError(f'{provider} pagination must stay on its official HTTPS API')
    headers = dict(headers or {})
    def fetch():
        request = urllib.request.Request(url, headers={'Accept': 'application/json',
            'User-Agent': 'MediaArchiveImporter/0.3 (comic metadata)', **headers})
        # Never forward an authenticated header through automatic redirects.
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError(f'{provider} metadata response exceeds size limit')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError(f'{provider} returned a non-JSON page; public API may be unavailable') from None
    return _memo(provider, (url, headers), fetch, interval)


def _pages(provider, url, headers=None, limit=100, max_pages=2, interval=1):
    result, seen = [], set()
    for _ in range(max_pages):
        if url in seen:
            raise ValueError(f'{provider} pagination loop')
        seen.add(url)
        data = _json(provider, url, headers, interval)
        rows = data.get('results') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError(f'{provider} did not return catalog records')
        result.extend(row for row in rows if isinstance(row, dict))
        if len(result) >= limit or not isinstance(data, dict) or not data.get('next'):
            break
        url = urllib.parse.urljoin(url, data['next'])
    return result[:limit]


def _id(provider, value, url=None):
    return {'provider': provider, 'external_id': str(value), 'source_url': url} if value not in (None, '') else None


def _catalog(provider, title, source, **fields):
    return {'provider': provider, 'title': title, 'source_url': source, 'source': source,
            'format': 'catalog', 'discovery_only': True, 'author': 'Unknown', **fields}


def _name(value):
    name = str(value.get('name') or '') if isinstance(value, dict) else str(value or '')
    return '' if name.startswith(('https://', 'http://')) else name


def search_comicvine_metadata(query, api_key, limit=100):
    from .comics import search_comicvine, list_comicvine_issues
    parsed = parse_query(query)
    series = _memo('Comic Vine', ('search', parsed.title, api_key),
                   lambda: search_comicvine(parsed.title, api_key, limit=20), interval=1.2)
    for row in series:
        row.update(comic_entity='series', series_title=row['title'], series_start_year=str(row.get('start_year') or ''))
        row['external_ids'] = [_id('comicvine', row['comicvine_id'], row['source_url'])]
    ranked = sorted(series, key=lambda row: (-difflib.SequenceMatcher(None, key(parsed.title), key(row['title'])).ratio(),
                    str(row.get('start_year') or '')))
    if parsed.year:
        ranked = [row for row in ranked if str(row.get('start_year')) == parsed.year]
    issues, errors = [], []
    for volume in ranked[:3]:
        # Avoid expanding unrelated search hits or explicit collected editions.
        if COLLECTION.search(volume['title']) or difflib.SequenceMatcher(None, key(parsed.title), key(volume['title'])).ratio() < .65:
            continue
        try:
            entries = _memo('Comic Vine', ('issues', volume['comicvine_id'], parsed.number, api_key),
                lambda volume=volume: list_comicvine_issues(volume['comicvine_id'], api_key,
                    limit=100, **({'number': parsed.number} if parsed.number else {})), interval=1.2)
            for entry in entries:
                number = issue_number(entry.get('number'))
                if parsed.number and number != parsed.number:
                    continue
                title = f"{volume['title']} #{entry['number']}" if number else volume['title'] + ' — ' + entry['title']
                issues.append(_catalog('Comic Vine', title, entry['source_url'], kind='comic_issue', comic_entity='issue',
                    issue_number=number, issue_title=entry['title'], series_title=volume['title'],
                    series_start_year=str(volume.get('start_year') or ''), publisher=volume.get('publisher'),
                    comicvine_id=entry['comicvine_id'], comicvine_volume_id=volume['comicvine_id'],
                    release_date=entry.get('release_date'), store_date=entry.get('store_date'), cover_date=entry.get('cover_date'),
                    cover=entry.get('cover'), external_ids=[_id('comicvine_issue', entry['comicvine_id'], entry['source_url'])],
                    series_external_ids=[_id('comicvine', volume['comicvine_id'], volume['source_url'])]))
        except Exception as exc:
            errors.append(f'Comic Vine issues: {exc}')
    return issues[:max(1, min(limit, 100))] + ranked, errors


def search_metron(query, token, limit=100):
    parsed = parse_query(query)
    token = str(token or '').strip()
    if not token or len(token) > 500 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError('Configure a Metron API token')
    headers = {'Authorization': 'Bearer ' + token}
    params = {'series_name': parsed.title}
    if parsed.number:
        params['number'] = parsed.number
    if parsed.year:
        params['series_year_began'] = parsed.year
    # Year in this UI is the series run year, not the issue cover year.
    series_url = 'https://metron.cloud/api/series/?' + urllib.parse.urlencode(
        {'name': parsed.title, **({'year_began': parsed.year} if parsed.year else {})})
    series = _pages('Metron', series_url, headers, limit=20, max_pages=1, interval=3.1)
    series_by_id, output = {}, []
    for row in series:
        name = row.get('name') or YEAR.sub('', str(row.get('series') or '')).strip()
        if not name or row.get('id') is None:
            continue
        series_by_id[str(row['id'])] = row
        source = f"https://metron.cloud/series/{row['id']}/"
        ids = [value for value in (_id('metron_series', row['id'], source), _id('comicvine', row.get('cv_id')),
                                  _id('gcd_series', row.get('gcd_id'))) if value]
        output.append(_catalog('Metron', name, source, kind='comic', comic_entity='series',
            series_title=name, series_start_year=str(row.get('year_began') or ''), metron_series_id=str(row['id']),
            series_type=_name(row.get('series_type')), publisher=_name(row.get('publisher')),
            language=row.get('language'), external_ids=ids, issue_count=row.get('issue_count')))
    try:
        rows = _pages('Metron', 'https://metron.cloud/api/issue/?' + urllib.parse.urlencode(params),
                      headers, limit=limit, max_pages=2, interval=3.1)
    except Exception as exc:
        return output, [f'Issue search: {exc}']
    for row in rows:
        series_row = row.get('series') or {}
        if not isinstance(series_row, dict):
            continue
        full = series_by_id.get(str(series_row.get('id')), series_row)
        year = str(full.get('year_began') or series_row.get('year_began') or '')
        if parsed.year and year != parsed.year:
            continue
        name = series_row.get('name') or full.get('name') or YEAR.sub('', str(full.get('series') or '')).strip()
        if not name or row.get('id') is None:
            continue
        source = row.get('resource_url') or f"https://metron.cloud/issue/{row['id']}/"
        ids = [value for value in (_id('metron_issue', row['id'], source), _id('comicvine_issue', row.get('cv_id')),
                                  _id('gcd_issue', row.get('gcd_id'))) if value]
        output.append(_catalog('Metron', f"{name} #{row.get('number') or '?'}", source,
            kind='comic_issue', comic_entity='issue', series_title=name, issue_number=issue_number(row.get('number')),
            series_start_year=year, publisher=_name(full.get('publisher')), language=series_row.get('language'),
            series_type=_name(full.get('series_type')), metron_issue_id=str(row['id']),
            comicvine_id=str(row['cv_id']) if row.get('cv_id') else None,
            comicvine_volume_id=str(full['cv_id']) if full.get('cv_id') else None,
            release_date=row.get('store_date') or row.get('cover_date'), cover_date=row.get('cover_date'),
            cover=row.get('image'), external_ids=ids, series_external_ids=[
                value for value in (_id('metron_series', series_row.get('id')), _id('comicvine', full.get('cv_id')),
                                    _id('gcd_series', full.get('gcd_id'))) if value]))
    return output, []


def _bare_title(value):
    return re.sub(r'^(?:the|a|an)\s+', '', key(value))


_SORT_ARTICLE = re.compile(r'^(?:the|a|an|la|le|les|l|el|los|las|die|der|das|het|il|lo|gli|o|os)\s+')


def _gcd_sort_key(name):
    """Approximate GCD's series sort name: no accents, case or leading article."""
    text = unicodedata.normalize('NFKD', str(name or '').casefold())
    text = ''.join(c for c in text if not unicodedata.combining(c)).replace("'", ' ')
    return _SORT_ARTICLE.sub('', ' '.join(re.findall(r'[^\W_]+', text)))


def _gcd_exact_series(series_url, wanted, max_requests=6):
    """Binary-search GCD's name-ordered result pages for runs named exactly ``wanted``."""
    first = _json('GCD', series_url, interval=2)
    rows = first.get('results') if isinstance(first, dict) else None
    if not rows or not first.get('next'):
        return []
    pages = -(-int(first.get('count') or 0) // len(rows))
    target = _gcd_sort_key(wanted)
    separator = '&' if '?' in series_url else '?'
    def page(number):
        data = _json('GCD', f'{series_url}{separator}page={number}', interval=2)
        return [row for row in (data.get('results') or []) if isinstance(row, dict)]
    low, high, requests, found = 2, pages, 0, None
    while low <= high and requests < max_requests:
        middle = (low + high) // 2
        rows, requests = page(middle), requests + 1
        if not rows:
            high = middle - 1
        elif target < _gcd_sort_key(rows[0].get('name')):
            high = middle - 1
        elif target > _gcd_sort_key(rows[-1].get('name')):
            low = middle + 1
        else:
            found = middle
            break
    if found is None:
        return []
    matches = [row for row in rows if _bare_title(row.get('name')) == wanted]
    # Same-named runs can straddle a page boundary.
    for neighbour, edge in ((found - 1, rows[0]), (found + 1, rows[-1])):
        if 2 <= neighbour <= pages and _bare_title(edge.get('name')) == wanted:
            matches += [row for row in page(neighbour) if _bare_title(row.get('name')) == wanted]
    return matches


def search_gcd(query, limit=100):
    parsed = parse_query(query)
    # GCD files many runs without the article ("Sandman", 1989) and its name
    # search matches "The Sandman" from "Sandman" too, so search without it.
    name = re.sub(r'^(?:the|a|an)\s+', '', parsed.title, flags=re.I) or parsed.title
    base = 'https://www.comics.org/api/series/name/' + urllib.parse.quote(name, safe='') + '/'
    series_url = base + (f'year/{parsed.year}/' if parsed.year else '')
    series = _pages('GCD', series_url, limit=50, max_pages=1, interval=2)
    wanted = _bare_title(parsed.title)
    if wanted and not any(_bare_title(row.get('name')) == wanted for row in series):
        # A short, common name ("Saga") matches hundreds of runs listed in name
        # order, so the run itself is pages in. Find its page instead of giving up.
        series = _gcd_exact_series(series_url, wanted) + series
    output = []
    for row in series:
        source = str(row.get('api_url') or '')
        match = re.search(r'/series/(\d+)/?', source)
        if not match or not row.get('name'):
            continue
        source = f'https://www.comics.org/series/{match.group(1)}/'
        output.append(_catalog('GCD', row['name'], source, kind='comic', comic_entity='series',
            series_title=row['name'], series_start_year=str(row.get('year_began') or ''),
            publisher=_name(row.get('publisher')), language=row.get('language'),
            series_type=str(row.get('publishing_format') or ''),
            issue_count=len(row.get('active_issues') or []) or None,
            external_ids=[_id('gcd_series', match.group(1), source)], gcd_series_id=match.group(1),
            catalog_metadata={'license': 'CC-BY-SA-4.0', 'attribution': 'Grand Comics Database',
                              'cover_rights': 'Cover images retain their respective copyright', 'source_url': source}))
    # Prefer the queried series over substring matches when expanding archives,
    # then English runs, then the longest run (Gaiman's 75-issue Sandman over
    # the 7-issue 1974 one).
    issue_counts = {f"https://www.comics.org/series/{m.group(1)}/": len(r.get('active_issues') or [])
                    for r in series for m in [re.search(r'/series/(\d+)/?', str(r.get('api_url') or ''))] if m}
    wanted = _bare_title(parsed.title)
    output.sort(key=lambda row: (-difflib.SequenceMatcher(None, wanted, _bare_title(row['title'])).ratio(),
                                 key(row.get('language')) not in ('', 'en'), -issue_counts.get(row['source_url'], 0)))
    # Foreign-language runs are mostly translated reprints ("Biblioteca Sandman",
    # "Den absoluta Sandman"); show them only when nothing in English matched.
    english = [row for row in output if key(row.get('language')) in ('', 'en')]
    output = english or output
    issue_rows, errors = [], []
    if parsed.number:
        # GCD issue year filter means issue publication year. Match run year
        # against the series records instead, rather than excluding later issues.
        try:
            issue_rows = _pages('GCD', base + 'issue/' + urllib.parse.quote(parsed.number, safe='') + '/',
                                limit=limit, max_pages=2, interval=2)
        except Exception as exc:
            errors.append(f'Issue search: {exc}')
    else:
        # Bounded overview gives individual issues without one request per issue.
        for row in output[:2]:
            try:
                issue_rows.extend({**entry, 'series_name': row['title'], 'series': row['source_url'],
                    'series_start_year': row['series_start_year'], 'publisher': row['publisher'], 'language': row['language']}
                    for entry in _pages('GCD', f"https://www.comics.org/api/series/{row['gcd_series_id']}/overview/",
                                         limit=50, max_pages=1, interval=2))
            except Exception as exc:
                errors.append(f"{row['title']} overview: {exc}")
    for row in issue_rows:
        issue_id = str(row.get('issue_id') or '')
        if not issue_id:
            match = re.search(r'/issue/(\d+)', str(row.get('api_url') or ''))
            issue_id = match.group(1) if match else ''
        series_id = re.search(r'/series/(\d+)', str(row.get('series') or ''))
        parent = next((p for p in output if series_id and p.get('gcd_series_id') == series_id.group(1)), None)
        if parsed.year and not parent:
            continue
        name = (parent or {}).get('title') or YEAR.sub('', str(row.get('series_name') or '')).strip()
        match = ISSUE.search(str(row.get('descriptor') or ''))
        number = issue_number(row.get('number') or (match.group(1) if match else ''))
        if not issue_id or not name or not number:
            continue
        source = f'https://www.comics.org/issue/{issue_id}/'
        # A "collected editions" run numbers its trade volumes 1, 2, 3...; they are
        # books collecting the issues, not a second copy of issues #1, #2, #3.
        collected = bool(COLLECTION.search((parent or {}).get('series_type') or ''))
        output.append(_catalog('GCD', f'{name} Vol. {number}' if collected else f'{name} #{number}', source,
            kind='comic_issue', comic_entity='collection' if collected else 'issue',
            series_type=(parent or {}).get('series_type') or '', volume_number=number if collected else None,
            series_title=name, issue_number=number, series_start_year=(parent or row).get('series_start_year'),
            publisher=(parent or row).get('publisher'), language=(parent or row).get('language'),
            gcd_issue_id=issue_id, variant_of=row.get('variant_of'),
            release_date=row.get('on_sale_date'), publication_date=row.get('publication_date'),
            cover=row.get('cover') or row.get('cover_url'),
            external_ids=[_id('gcd_issue', issue_id, source)],
            series_external_ids=(parent or {}).get('external_ids') or [],
            catalog_metadata={'license': 'CC-BY-SA-4.0', 'attribution': 'Grand Comics Database', 'source_url': source}))
    return output, errors


def metadata_tasks(query, credentials=None):
    credentials = credentials or {}
    tasks = [('Grand Comics Database', lambda: search_gcd(query))]
    cv = str(credentials.get('comicvine_api_key') or os.environ.get('COMICVINE_API_KEY') or '').strip()
    metron = str(credentials.get('metron_api_token') or os.environ.get('METRON_API_TOKEN') or '').strip()
    if cv:
        tasks.insert(0, ('Comic Vine', lambda: search_comicvine_metadata(query, cv)))
    if metron:
        tasks.append(('Metron', lambda: search_metron(query, metron)))
    return tasks


def normalize(item):
    item = copy.deepcopy(item)
    catalog = item.get('catalog_metadata') or item.get('metadata') or {}
    title = str(item.get('title') or '')
    match = ISSUE.search(title)
    entity = item.get('comic_entity') or catalog.get('comic_entity')
    number = issue_number(item.get('issue_number') or catalog.get('issue_number') or
                           (match.group(1) if match else ''))
    explicit_type = _name(item.get('series_type') or item.get('edition_format') or catalog.get('edition_format'))
    if COLLECTION.search(title + ' ' + explicit_type) or ISSUE_RANGE.search(title):
        entity = 'collection'
    elif not entity:
        entity = 'issue' if number or item.get('kind') == 'comic_issue' else 'series'
    item['comic_entity'] = entity
    item['issue_number'] = number if entity == 'issue' else ''
    item['series_title'] = item.get('series_title') or catalog.get('series_title') or (
        YEAR.sub('', title[:match.start()] if match else title).strip(' -:'))
    year = YEAR.search(title)
    item['series_start_year'] = str(item.get('series_start_year') or item.get('start_year') or
                                   catalog.get('series_start_year') or (year.group(1) if year else ''))
    for field in ('publisher', 'language', 'external_ids', 'series_external_ids'):
        item[field] = item.get(field) or catalog.get(field) or ([] if field.endswith('_ids') else '')
    item['publisher'] = _name(item['publisher'])
    item['kind'] = 'comic_issue' if entity == 'issue' else (
        'comic' if item.get('kind') == 'comic_issue' else item.get('kind') or 'comic')
    # Cover variants are separate editions of one story issue, not repeated results.
    if item.get('variant_of'):
        match = re.search(r'/issue/(\d+)', str(item['variant_of']))
        if match:
            item['external_ids'].append(_id('gcd_issue', match.group(1)))
    return item


def _ids(item, field='external_ids'):
    return {(str(i.get('provider') or '').casefold(), str(i.get('external_id') or ''))
            for i in item.get(field) or [] if isinstance(i, dict) and i.get('external_id')}


def _same(left, right):
    if left['comic_entity'] != right['comic_entity']:
        return False
    if left.get('language') and right.get('language') and key(left['language']) != key(right['language']):
        return False
    ids_a, ids_b = _ids(left), _ids(right)
    if ids_a & ids_b:
        return True
    # Conflicting provider IDs identify separate runs/editions even with same text.
    for provider in {p for p, _i in ids_a} & {p for p, _i in ids_b}:
        if {i for p, i in ids_a if p == provider}.isdisjoint({i for p, i in ids_b if p == provider}):
            return False
    if key(left['series_title']) != key(right['series_title']) or left['issue_number'] != right['issue_number']:
        return False
    for field in ('series_start_year', 'publisher', 'language'):
        if left.get(field) and right.get(field) and key(left[field]) != key(right[field]):
            return False
    parents_a, parents_b = _ids(left, 'series_external_ids'), _ids(right, 'series_external_ids')
    for provider in {p for p, _i in parents_a} & {p for p, _i in parents_b}:
        if {i for p, i in parents_a if p == provider}.isdisjoint({i for p, i in parents_b if p == provider}):
            return False
    if parents_a and parents_b and parents_a & parents_b:
        return True
    if left['comic_entity'] == 'collection' and key(left['title']) != key(right['title']):
        return False
    # A numbered issue needs run evidence. An undated release can join only one
    # candidate group below; ambiguous reboots stay separate instead of guessing.
    return True


def group_comics(results):
    rows = [normalize(row) for row in results]
    # Specific records first means an undated #1 sees both reboot candidates.
    rows.sort(key=lambda row: (0 if row['series_start_year'] else 1,
                               0 if _ids(row) else 1, key(row['title'])))
    groups = []
    for row in rows:
        matches = [group for group in groups if any(_same(row, other) for other in group)
                   and all(_same(row, other) for other in group)]
        if len(matches) == 1:
            matches[0].append(row)
        else:
            groups.append([row])
    return groups


def finish_group(chosen, rows):
    metadata = [row for row in rows if row.get('source_role') != 'download']
    preferred = sorted(metadata or rows, key=lambda row: (
        {'Comic Vine': 0, 'Metron': 1, 'GCD': 2}.get(row.get('provider'), 3),
        -sum(bool(row.get(field)) for field in ('series_start_year', 'publisher', 'cover', 'issue_number'))))[0]
    if metadata:
        chosen['metadata_url'] = preferred.get('source_url') or preferred.get('url')
        chosen['metadata_provider'] = preferred.get('provider')
        chosen['source_url'] = chosen['metadata_url']
        if chosen.get('source_role') != 'download':
            chosen.update(provider=preferred.get('provider'), url=chosen['metadata_url'])
    for field in ('comic_entity', 'issue_number', 'series_title', 'series_start_year', 'kind', 'publisher', 'language',
                  'comicvine_id', 'comicvine_volume_id', 'gcd_issue_id', 'gcd_series_id', 'metron_issue_id', 'metron_series_id',
                  'release_date', 'issue_title', 'cover'):
        evidence = [row for row in [preferred, *rows] if row.get(field) not in (None, '', [])]
        if evidence:
            chosen[field] = evidence[0][field]
    chosen['external_ids'] = []
    chosen['series_external_ids'] = []
    for field in ('external_ids', 'series_external_ids'):
        seen = set()
        for row in rows:
            for identifier in row.get(field) or []:
                identity = (identifier['provider'], str(identifier['external_id']))
                if identity not in seen:
                    seen.add(identity); chosen[field].append(identifier)
    chosen['comic_metadata_provenance'] = {field: [
        {'value': row[field], 'provider': row.get('provider'), 'source_url': row.get('source_url')}
        for row in rows if row.get(field) not in (None, '', [])]
        for field in ('series_title', 'issue_number', 'series_start_year', 'publisher', 'language', 'release_date', 'cover')}
    chosen['edition_variants'] = [dict(row) for row in rows if row.get('variant_of') or row.get('variant_name')]
    chosen['comic_issue'] = {field: chosen.get(field) for field in ('issue_number', 'issue_title', 'series_title',
        'series_start_year', 'comicvine_id', 'comicvine_volume_id', 'gcd_issue_id', 'metron_issue_id')}
    chosen['comic_issue']['volume_id'] = chosen.get('comicvine_volume_id')
    if chosen.get('comic_entity') == 'issue' and chosen.get('series_start_year') and chosen.get('issue_number'):
        chosen['title'] = f"{chosen['series_title']} ({chosen['series_start_year']}) #{chosen['issue_number']}"
    return chosen


def sort_comics(rows, query=''):
    parsed = parse_query(query) if query else ComicQuery('')
    entity_order = {'issue': 0, 'series': 1, 'collection': 2}
    if parsed.collections:
        entity_order = {'collection': 0, 'issue': 1, 'series': 2}
    def rank(row):
        number = row.get('issue_number') or ''
        try:
            order = (0, Decimal(number))
        except InvalidOperation:
            order = (1, number)
        # A plain series-name search ("Bone") leads with that series; issues of it
        # follow in the usual order instead of hiding it below dozens of rows.
        named_series = (parsed.title and not parsed.number and not parsed.collections and
                        row.get('comic_entity') == 'series' and
                        _bare_title(row.get('series_title') or row.get('title')) == _bare_title(parsed.title))
        return (0 if named_series else 1, -int(row.get('issue_count') or 0) if named_series else 0,
                entity_order.get(row.get('comic_entity'), 3),
                0 if not parsed.number or number == parsed.number else 1,
                -difflib.SequenceMatcher(None, key(parsed.title), key(row.get('series_title'))).ratio() if parsed.title else 0,
                key(row.get('series_title')), str(row.get('series_start_year') or ''), order)
    return sorted(rows, key=rank)


def add_issue_watch(db, item, sources=None, profile_id=None):
    from .watched import add_watch, assign_watch_profile
    item = normalize(item)
    if item['comic_entity'] != 'issue' or not item['issue_number']:
        raise ValueError('Choose an individual numbered comic issue')
    source = item.get('metadata_url') or item.get('source_url') or item.get('url')
    saved_sources = {name: value for name, value in (sources or {}).items() if name not in {
        'comicvine_api_key', 'metron_api_token', 'taddy_api_key', 'taddy_user_id', 'tmdb_api_key',
        'youtube_api_key', 'igdb_client_id', 'igdb_client_secret', 'hardcover_api_token'}}
    metadata = {'sources': saved_sources, 'comic_issue': item.get('comic_issue') or {
        'issue_number': item['issue_number'], 'series_title': item['series_title'],
        'comicvine_id': item.get('comicvine_id'), 'volume_id': item.get('comicvine_volume_id')},
        'external_ids': item.get('external_ids') or [], 'catalog_metadata': item,
        'comic_metadata_provenance': item.get('comic_metadata_provenance') or {}}
    watch_id = add_watch(db, 'comic_issue', item['title'], item.get('author') or 'Unknown', source,
        str(item.get('release_date') or '')[:10] or None, metadata)
    if profile_id:
        assign_watch_profile(db, watch_id, profile_id)
    return watch_id
