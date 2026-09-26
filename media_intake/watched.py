"""Goodreads watch-list metadata and scheduled source searches."""
import datetime as dt
import difflib
import html
import json
import logging
import os
import re
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from .comics import list_comicvine_issues, list_mangadex_chapters, mangadex_id_from_url
from .comic_sources import (resolve_getcomics_post, search_getcomics,
                            search_public_domain_comic_mirror)
from .rights_evidence import (effective_work_rights, load_rights_evidence,
                              source_download_rights)
from .media_catalogs import (discover_novelarchive_chapters, english_release_date,
                             ranobedb_series, resolve_catalog_file_links,
                             search_lnori, search_novelarchive)
from .core import download, enqueue, search_catalog, search_opds
from .fanfiction import story_url_key
from .irc import enqueue_irc, search_irc
from .serial import discover_feed, enqueue_serial
from .sources import resolve_detail, search_openlibrary, search_site

CHECK_INTERVAL = dt.timedelta(days=1)
COMIC_KINDS = {'comic', 'manga', 'webcomic', 'webtoon'}
SERIAL_KINDS = {'webfiction', 'fanfiction'}
logger = logging.getLogger(__name__)


class GoodreadsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.jsonld = []
        self.anchors = []
        self.anchor = None
        self.script_chunks = None
        self.h1_chunks = []
        self.text_chunks = []
        self.in_h1 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            key = attrs.get('property') or attrs.get('name') or attrs.get('itemprop')
            if key and attrs.get('content'):
                self.meta[key.casefold()] = attrs['content'].strip()
        elif tag == 'script' and attrs.get('type', '').split(';')[0].strip().casefold() == 'application/ld+json':
            self.script_chunks = []
        elif tag == 'a':
            self.anchor = {'href': attrs.get('href', ''), 'title': attrs.get('title', ''), 'text': ''}
        elif tag == 'h1':
            self.in_h1 = True

    def handle_data(self, data):
        if self.script_chunks is not None:
            self.script_chunks.append(data)
        else:
            self.text_chunks.append(data)
        if self.anchor is not None:
            self.anchor['text'] += data
        if self.in_h1:
            self.h1_chunks.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.script_chunks is not None:
            self.jsonld.append(''.join(self.script_chunks))
            self.script_chunks = None
        elif tag == 'a' and self.anchor is not None:
            self.anchor['text'] = ' '.join(self.anchor['text'].split())
            self.anchors.append(self.anchor)
            self.anchor = None
        elif tag == 'h1':
            self.in_h1 = False


def _plain(value):
    if isinstance(value, dict):
        return str(value.get('name') or value.get('@id') or '')
    if isinstance(value, list):
        return ', '.join(filter(None, (_plain(part) for part in value)))
    return str(value or '').strip()


def _date(value):
    if isinstance(value, dict):
        value = value.get('@value') or value.get('name') or ''
    text = str(value or '').strip()
    if not text:
        return None
    iso = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', text)
    if iso:
        try:
            return dt.date.fromisoformat(iso.group(1)).isoformat()
        except ValueError:
            return None
    for form in ('%B %d, %Y', '%b %d, %Y', '%B %Y', '%b %Y', '%Y'):
        try:
            parsed = dt.datetime.strptime(text, form).date()
            if form in ('%B %Y', '%b %Y', '%Y'):
                parsed = parsed.replace(day=1)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _visible_release(text):
    labels = r'(?:expected\s+)?(?:publication|release)(?:\s+date)?'
    date = r'([A-Za-z]+\s+\d{1,2},\s+\d{4}|[A-Za-z]+\s+\d{4}|\d{4}-\d{2}-\d{2})'
    match = re.search(labels + r'\s*(?::|-|is)?\s*' + date, text, flags=re.I)
    return _date(match.group(1)) if match else None


def _walk_json(value):
    if isinstance(value, list):
        for part in value:
            yield from _walk_json(part)
    elif isinstance(value, dict):
        yield value
        for key, part in value.items():
            if key not in ('author', 'publisher'):
                yield from _walk_json(part)


def _type_has(value, expected):
    types = value.get('@type', [])
    if isinstance(types, str):
        types = [types]
    return any(str(kind).casefold() == expected.casefold() for kind in types)


def _book_from_node(node, page_url):
    title = _plain(node.get('name') or node.get('headline'))
    author = _plain(node.get('author')) or 'Unknown'
    href = node.get('url') or node.get('@id') or page_url
    if isinstance(href, dict):
        href = href.get('@id') or href.get('url') or page_url
    return {
        'title': title,
        'author': author,
        'release_date': _date(node.get('datePublished') or node.get('dateCreated')),
        'source_url': _canonical_book_url(urllib.parse.urljoin(page_url, str(href))),
    }


def _canonical_book_url(url):
    parsed = urllib.parse.urlsplit(url)
    path = re.sub(r'/+$', '', parsed.path)
    match = re.search(r'(/book/show/[^/]+)', path, flags=re.I)
    if match:
        path = match.group(1)
    host = (parsed.hostname or '').casefold()
    if host == 'goodreads.com' or host.endswith('.goodreads.com'):
        return urllib.parse.urlunsplit(('https', 'www.goodreads.com', path, '', ''))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


def parse_goodreads_page(page_url, content):
    """Extract public book or series metadata from Goodreads HTML/JSON-LD."""
    parser = GoodreadsParser()
    parser.feed(content)
    path = urllib.parse.urlsplit(page_url).path.casefold()
    is_author = '/author/show/' in path or '/author/list/' in path
    is_series = '/series/' in path or bool(re.search(r'/series\b', path))
    nodes = []
    for raw in parser.jsonld:
        try:
            nodes.extend(_walk_json(json.loads(raw)))
        except (ValueError, TypeError):
            continue
    books = [_book_from_node(node, page_url) for node in nodes if _type_has(node, 'Book')]
    series_node = next((node for node in nodes if _type_has(node, 'BookSeries')), {})
    title = _plain(series_node.get('name')) if is_series else ''
    if is_author and parser.h1_chunks:
        title = ' '.join(''.join(parser.h1_chunks).split())
    author = _plain(series_node.get('author')) if is_series else ''
    if not title:
        title = parser.meta.get('og:title', '')
    title = re.sub(r'\s*[|:]\s*goodreads.*$', '', title, flags=re.I).strip()
    if not title and parser.h1_chunks:
        title = ' '.join(''.join(parser.h1_chunks).split())
    if is_author:
        title = re.sub(r'\s*\((?:author of|goodreads author).*\)$', '', title, flags=re.I).strip()
    if not is_series and ' by ' in title:
        title, suffix = title.rsplit(' by ', 1)
        if not author and 'goodreads' not in suffix.casefold():
            author = re.sub(r'\s*[|:].*$', '', suffix).strip()
    author = author or parser.meta.get('book:author') or parser.meta.get('author') or 'Unknown'
    release = None
    if not is_series and not is_author:
        current = next((book for book in books if book.get('title')), {})
        title = title or current.get('title', '')
        author = current.get('author') or author
        release = current.get('release_date')
        if not release:
            release = _date(parser.meta.get('book:release_date') or parser.meta.get('datepublished') or parser.meta.get('datePublished'))
        if not release:
            release = _visible_release(' '.join(parser.text_chunks))

    found_books = {}
    for book in books:
        if book.get('title') and book.get('source_url'):
            key = _canonical_book_url(book['source_url'])
            found_books[key] = book
    for anchor in parser.anchors:
        href = anchor.get('href', '')
        if '/book/show/' not in urllib.parse.urlsplit(href).path:
            continue
        book_url = _canonical_book_url(urllib.parse.urljoin(page_url, href))
        name = anchor.get('title') or anchor.get('text')
        name = re.sub(r'\s*\(.*?\)\s*$', '', name).strip()
        if not name:
            continue
        entry = found_books.get(book_url, {})
        entry.update({'title': entry.get('title') or name,
                      'author': entry.get('author') or 'Unknown',
                      'source_url': book_url})
        found_books[book_url] = entry

    canonical = parser.meta.get('og:url') or parser.meta.get('twitter:url') or page_url
    canonical = urllib.parse.urljoin(page_url, html.unescape(canonical))
    kind = ('author' if is_author else
            'series' if is_series or _type_has(series_node, 'BookSeries') else 'book')
    return {
        'kind': kind,
        'title': title or 'Untitled Goodreads entry',
        'author': author,
        'source_url': canonical,
        'release_date': release,
        'books': list(found_books.values()) if kind in ('series', 'author') else [],
    }


def fetch_goodreads(url):
    from .core import http_get
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or '').casefold()
    if parsed.scheme not in ('http', 'https') or not (host == 'goodreads.com' or host.endswith('.goodreads.com')):
        raise ValueError('Enter a public Goodreads book, series, or author URL')
    if parsed.username or parsed.password:
        raise ValueError('Goodreads URL must not contain login credentials')
    try:
        raw, final_url = http_get(url)
    except Exception as exc:
        raise _goodreads_access_error(exc) from exc
    return parse_goodreads_page(final_url, raw.decode('utf-8', errors='replace'))


def parse_goodreads_html(url, html_path):
    """Parse a Goodreads page the user saved from their regular browser."""
    parsed = urllib.parse.urlsplit(str(url or '').strip())
    host = (parsed.hostname or '').casefold()
    if (parsed.scheme not in ('http', 'https') or
            not (host == 'goodreads.com' or host.endswith('.goodreads.com')) or
            parsed.username or parsed.password):
        raise ValueError('Enter the public Goodreads profile, series, or book URL first')
    path = Path(html_path)
    if not path.is_file() or path.stat().st_size > 10_000_000:
        raise ValueError('Saved Goodreads HTML must be a file smaller than 10 MB')
    return parse_goodreads_page(url, path.read_text(encoding='utf-8', errors='replace'))


def _goodreads_access_error(exc):
    from urllib.error import HTTPError
    if isinstance(exc, HTTPError) and exc.code == 302:
        return ValueError('Goodreads redirected the automated page request repeatedly (HTTP 302). '
                          'Open the Goodreads page in your browser, save it as HTML, then use Import Goodreads HTML.')
    if isinstance(exc, HTTPError) and exc.code in (401, 403, 429):
        return ValueError(f'Goodreads declined the automated page request (HTTP {exc.code}). '
                          'Open the Goodreads page in your browser, save it as HTML, then use Import Goodreads HTML.')
    return ValueError(f'Could not read the Goodreads page: {exc}')


def _goodreads_results_from_html(query, page_url, content, limit):
    parser = GoodreadsParser()
    parser.feed(content)
    results, seen = [], set()
    for anchor in parser.anchors:
        href = anchor.get('href', '')
        path = urllib.parse.urlsplit(href).path.casefold()
        if '/book/show/' in path:
            kind = 'book'
            result_url = _canonical_book_url(urllib.parse.urljoin(page_url, href))
        elif '/author/show/' in path:
            kind = 'author'
            result_url = urllib.parse.urljoin(page_url, href).split('?', 1)[0].split('#', 1)[0]
        elif '/series/' in path and '/series/list/' not in path:
            kind = 'series'
            result_url = urllib.parse.urljoin(page_url, href).split('?', 1)[0].split('#', 1)[0]
        else:
            continue
        title = ' '.join((anchor.get('title') or anchor.get('text') or '').split())
        title = re.sub(r'\s*\([^)]*\)\s*$', '', title).strip()
        key = (kind, result_url)
        if title and key not in seen:
            seen.add(key)
            results.append({'kind': kind, 'title': title, 'source_url': result_url})
            if len(results) >= limit:
                break
    return results


def parse_goodreads_search_html(query, html_path, page_url):
    """Read Goodreads search results saved from the user's normal browser."""
    parsed = urllib.parse.urlsplit(str(page_url or '').strip())
    host = (parsed.hostname or '').casefold()
    if (parsed.scheme not in ('http', 'https') or
            not (host == 'goodreads.com' or host.endswith('.goodreads.com')) or
            parsed.username or parsed.password or parsed.path.casefold() != '/search'):
        raise ValueError('Enter the Goodreads search page URL before importing saved results')
    query = str(query or '').strip()
    if not query or len(query) > 150:
        raise ValueError('Enter the Goodreads search phrase before importing saved results')
    path = Path(html_path)
    if not path.is_file() or path.stat().st_size > 10_000_000:
        raise ValueError('Saved Goodreads HTML must be a file smaller than 10 MB')
    return _goodreads_results_from_html(query, page_url,
                                        path.read_text(encoding='utf-8', errors='replace'), 80)


def search_goodreads(query, limit=80):
    """Search Goodreads book, series, and author result pages for selectable entries."""
    from .core import http_get
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a Goodreads search up to 150 characters')
    results, errors = [], []
    for search_type in ('books', 'authors', 'series'):
        url = 'https://www.goodreads.com/search?' + urllib.parse.urlencode(
            {'q': query, 'search_type': search_type})
        try:
            raw, final_url = http_get(url)
            results.extend(_goodreads_results_from_html(query, final_url,
                                                        raw.decode('utf-8', errors='replace'), limit))
        except Exception as exc:
            errors.append(str(_goodreads_access_error(exc)))
            continue
        deduplicated = {}
        for result in results:
            deduplicated[(result['kind'], result['source_url'])] = result
        results = list(deduplicated.values())[:limit]
        if len(results) >= limit:
            return results
    if not results and errors:
        raise ValueError('Goodreads search failed: ' + '; '.join(errors[-2:]))
    return results


def _now():
    return dt.datetime.now().replace(microsecond=0)


def _next_day(now=None):
    return (_now() if now is None else now) + CHECK_INTERVAL


def _watch_state(release_date, kind):
    if kind in ('series', 'author'):
        return kind
    if release_date:
        try:
            if dt.date.fromisoformat(release_date) > dt.date.today():
                return 'upcoming'
        except ValueError:
            raise ValueError('Release date must be YYYY-MM-DD')
    return 'watching'


def add_watch(db, kind, title, author='Unknown', source_url=None, release_date=None,
              metadata=None, parent_id=None):
    title = str(title or '').strip()
    if not title:
        raise ValueError('Enter a title or series name')
    if kind not in ({'book', 'series', 'author', 'lightnovel', 'comic_issue'} | COMIC_KINDS | SERIAL_KINDS):
        raise ValueError('Watch type must be book, series, author, light novel, comic, manga, webcomic, webtoon, or web serial')
    release_date = str(release_date or '').strip() or None
    if release_date:
        try:
            release_date = dt.date.fromisoformat(release_date).isoformat()
        except ValueError as exc:
            raise ValueError('Release date must be YYYY-MM-DD') from exc
    if source_url:
        parsed = urllib.parse.urlsplit(source_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('Enter a public HTTP(S) source URL without credentials')
    state = _watch_state(release_date, kind)
    now = _now()
    next_check = None
    if state == 'upcoming':
        next_check = f'{release_date}T00:00:00'
    else:
        next_check = now.isoformat(sep='T')
    payload = json.dumps(metadata or {}, ensure_ascii=False)
    if source_url:
        existing = db.execute('SELECT id,state FROM watched_items WHERE source_url=?', (source_url,)).fetchone()
        if existing:
            if existing['state'] == 'downloaded':
                return int(existing['id'])
            db.execute('''UPDATE watched_items SET title=?,author=?,release_date=?,state=?,
                next_check_at=?,
                metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                       (title, author or 'Unknown', release_date, state, next_check,
                        payload, existing['id']))
            db.commit()
            return int(existing['id'])
    cursor = db.execute('''INSERT INTO watched_items
        (kind,title,author,source_url,release_date,state,next_check_at,parent_id,metadata)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (kind, title, author or 'Unknown', source_url, release_date, state, next_check,
         parent_id, payload))
    db.commit()
    return int(cursor.lastrowid)


def add_from_goodreads(db, url, kind=None, title=None, author='Unknown', release_date=None,
                       sources=None):
    parsed = {}
    if url:
        parsed = fetch_goodreads(url)
    return add_from_goodreads_metadata(db, parsed, url, kind, title, author, release_date, sources)


def add_from_goodreads_metadata(db, parsed, url, kind=None, title=None, author='Unknown',
                                release_date=None, sources=None):
    """Add a Goodreads watch from parsed live or user-saved page metadata."""
    detected_kind = kind or parsed.get('kind') or 'book'
    name = title or parsed.get('title')
    if not name or name == 'Untitled Goodreads entry':
        raise ValueError('Could not read a title from Goodreads. Enter the title manually.')
    writer = author if author and author != 'Unknown' else parsed.get('author', 'Unknown')
    if detected_kind == 'author':
        writer = 'Unknown'
    date = release_date or parsed.get('release_date')
    saved = {'sources': sources or {}}
    if detected_kind in ('series', 'author'):
        saved['known_books'] = [item.get('source_url') for item in parsed.get('books', []) if item.get('source_url')]
    watch_id = add_watch(db, detected_kind, name, writer, parsed.get('source_url') or url,
                         date, saved)
    if detected_kind in ('series', 'author'):
        # Established Goodreads pages contain many older books. Add only entries
        # whose release date is explicitly in the future.
        for book in parsed.get('books', []):
            if book.get('release_date') and book['release_date'] > dt.date.today().isoformat():
                book_author = book.get('author') or (name if detected_kind == 'author' else writer)
                add_watch(db, 'book', book['title'], book_author,
                          book.get('source_url'), book['release_date'], {'sources': sources or {}}, watch_id)
    return watch_id, parsed


def list_watches(db):
    return [dict(row) for row in db.execute('''SELECT * FROM watched_items
        ORDER BY CASE state WHEN 'upcoming' THEN 0 WHEN 'watching' THEN 1 ELSE 2 END,
        release_date, title COLLATE NOCASE''')]


def remove_watch(db, watch_id):
    db.execute('DELETE FROM watched_items WHERE parent_id=?', (watch_id,))
    db.execute('DELETE FROM watched_items WHERE id=?', (watch_id,))
    db.commit()


def get_setting(db, key, default=None):
    row = db.execute('SELECT value FROM gui_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(db, key, value):
    db.execute('INSERT INTO gui_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, str(value)))
    db.commit()


def _normalized(value):
    value = unicodedata.normalize('NFKD', str(value or '').casefold())
    value = ''.join(char for char in value if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[a-z0-9]+', value))


def _is_match(candidate, watch):
    title_a, title_b = _normalized(candidate.get('title')), _normalized(watch.get('title'))
    if not title_a or not title_b:
        return False
    ratio = difflib.SequenceMatcher(None, title_a, title_b).ratio()
    a_words, b_words = set(title_a.split()), set(title_b.split())
    overlap = len(a_words & b_words) / max(1, len(b_words))
    if ratio < .78 and not (ratio >= .65 and overlap >= .8):
        return False
    author = _normalized(watch.get('author'))
    candidate_author = _normalized(candidate.get('author'))
    if author and author != 'unknown' and candidate_author and candidate_author != 'unknown':
        author_ratio = difflib.SequenceMatcher(None, author, candidate_author).ratio()
        if author_ratio < .58:
            return False
    return True


def _approved_source_record(options, source_key, watch=None, db=None):
    """Read the one local yes/no record; a work-specific NO wins over a site YES."""
    path = options.get('rights_evidence_path')
    if not path:
        return {'approved_for_download': True, 'status': 'test_or_library_caller'}
    document = load_rights_evidence(path)
    if watch is not None:
        work_record = effective_work_rights(document, watch, db)
        if work_record:
            return work_record if work_record.get('approved_for_download') is True else None
    return source_download_rights(document, source_key)


def _search_candidates(watch, source_overrides=None):
    query = ' '.join(part for part in (watch['title'], watch.get('author')) if part and part != 'Unknown')
    options = dict(json.loads(watch.get('metadata') or '{}').get('sources') or {})
    options.update(source_overrides or {})
    results, errors = [], []

    def add_results(items, source_key, approval=None):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            item.setdefault('source_key', source_key)
            if options.get('rights_evidence_path'):
                item.setdefault('rights_evidence_path', options['rights_evidence_path'])
            if approval and approval.get('rights_work_key'):
                item.setdefault('rights_work_key', approval['rights_work_key'])
            results.append(item)

    if options.get('openlibrary', True) and _approved_source_record(options, 'openlibrary'):
        try:
            add_results(search_openlibrary(query, limit=15), 'openlibrary')
        except Exception as exc:
            errors.append(f'Open Library: {exc}')
    for feed_url in (options.get('opds_feeds', []) if _approved_source_record(
            options, 'opds_feeds') else []):
        try:
            add_results(search_opds(feed_url, query), 'opds_feeds')
        except Exception as exc:
            errors.append(f'OPDS {feed_url}: {exc}')
    for catalog_path in (options.get('catalogs', []) if _approved_source_record(
            options, 'json_catalogs') else []):
        try:
            add_results(search_catalog(catalog_path, query), 'json_catalogs')
        except Exception as exc:
            errors.append(f'Catalog {catalog_path}: {exc}')
    for site in ('libgen', 'annas'):
        source_key = 'annas_archive' if site == 'annas' else 'libgen'
        if (not options.get(site, True) or not _approved_source_record(options, source_key)):
            continue
        try:
            found = search_site(site, query, base=options.get(site + '_base') or None)
            for hit in found[:12]:
                if not hit.get('detail_url') or not _is_match(hit, watch):
                    continue
                try:
                    add_results(resolve_detail(site, hit['detail_url'], hit.get('title') or watch['title'],
                                               hit.get('author') or watch.get('author') or 'Unknown',
                                               hit.get('format')), source_key)
                except Exception as exc:
                    errors.append(f'{site}: {exc}')
        except Exception as exc:
            errors.append(f'{site}: {exc}')
    irc_configs = options.get('irc_configs') or ([options['irc_config']] if options.get('irc_config') else [])
    if not _approved_source_record(options, 'irc'):
        irc_configs = []
    for irc_config_path in irc_configs:
        try:
            config = json.loads(Path(irc_config_path).read_text(encoding='utf-8'))
            add_results(search_irc(query, config, limit=100), 'irc')
        except Exception as exc:
            errors.append(f'IRC: {exc}')
    if options.get('comic_mirror_base') and watch.get('kind') in COMIC_KINDS | {'comic_issue'}:
        try:
            rights = _approved_source_record(options, 'public_domain_comic_mirror', watch)
            if rights:
                add_results(search_public_domain_comic_mirror(
                    query, options['comic_mirror_base'], limit=20, manual_rights=rights),
                    'public_domain_comic_mirror')
        except Exception as exc:
            errors.append(f'Comic mirror / rights evidence: {exc}')
    if watch.get('kind') in COMIC_KINDS | {'comic_issue'}:
        approval = _approved_source_record(options, 'getcomics.org', watch)
        if approval:
            try:
                posts = search_getcomics(query, limit=20)
                for post in posts:
                    add_results(resolve_getcomics_post(post, approval), 'getcomics.org', approval)
            except Exception as exc:
                errors.append(f'GetComics / rights evidence: {exc}')
    if watch.get('kind') in {'book', 'lightnovel'}:
        for source_key, searcher in (('lnori.com', search_lnori),
                                     ('novelarchive.cc', search_novelarchive)):
            approval = _approved_source_record(options, source_key)
            if not approval:
                continue
            try:
                for item in searcher(query, limit=10):
                    add_results(resolve_catalog_file_links(item, approval), source_key, approval)
            except Exception as exc:
                errors.append(f'{source_key} / rights evidence: {exc}')
    if watch.get('kind') in {'webfiction', 'fanfiction'}:
        approval = _approved_source_record(options, 'novelarchive.cc')
        if approval:
            try:
                item = {'provider': 'NovelArchive', 'title': watch['title'],
                        'author': watch.get('author'), 'url': watch.get('source_url')}
                add_results(resolve_catalog_file_links(item, approval), 'novelarchive.cc', approval)
            except Exception as exc:
                errors.append(f'NovelArchive / rights evidence: {exc}')
    return [item for item in results if _is_match(item, watch)], errors


def _search_book(db, watch, library_folder, source_overrides=None):
    results, errors = _search_candidates(watch, source_overrides)
    for error in errors:
        logger.warning('Book watch %s connector error: %s', watch['id'], error)
    for item in results:
        try:
            row = enqueue_irc(db, item['irc']) if item.get('irc') else enqueue(db, item)
            row = download(db, row['id'], Path(library_folder).parent,
                           destination=library_folder)
            db.execute("UPDATE watched_items SET state='downloaded',file_path=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (row.get('file_path'), watch['id']))
            db.commit()
            return True
        except Exception as exc:
            logger.exception('Book download candidate failed for watch %s: %s',
                             watch['id'], item.get('title') or item.get('url'))
            errors.append(str(exc))
    reason = '; '.join(errors[-3:]) if errors else 'No matching downloadable result found.'
    db.execute("UPDATE watched_items SET state='watching',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (reason, watch['id']))
    db.commit()
    return False


def _process_goodreads_collection(db, watch):
    page = fetch_goodreads(watch['source_url'])
    metadata = json.loads(watch.get('metadata') or '{}')
    known = set(metadata.get('known_books') or [])
    current = {book.get('source_url'): book for book in page.get('books', []) if book.get('source_url')}
    sources = metadata.get('sources') or {}
    added = 0
    if metadata.pop('goodreads_baseline_pending', False):
        # When direct access was blocked, establish a baseline on the first
        # successful fetch so old books are not mistaken for new releases.
        for book in current.values():
            if book.get('release_date') and book['release_date'] > dt.date.today().isoformat():
                book_author = book.get('author') or (watch['title'] if watch['kind'] == 'author' else watch.get('author'))
                add_watch(db, 'book', book['title'], book_author,
                          book.get('source_url'), book['release_date'], {'sources': sources}, watch['id'])
        metadata['known_books'] = sorted(current)
        now = _now()
        db.execute("UPDATE watched_items SET metadata=?,last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (json.dumps(metadata, ensure_ascii=False), now.isoformat(sep='T'),
                    _next_day(now).isoformat(sep='T'), watch['id']))
        db.commit()
        return 0
    for url, book in current.items():
        if url in known:
            if book.get('release_date') and book['release_date'] > dt.date.today().isoformat():
                book_author = book.get('author') or (watch['title'] if watch['kind'] == 'author' else watch.get('author'))
                add_watch(db, 'book', book['title'], book_author,
                          url, book['release_date'], {'sources': sources}, watch['id'])
            continue
        try:
            detail = fetch_goodreads(url)
        except Exception as exc:
            logger.info('Goodreads book detail lookup failed for %s; using author-page metadata: %s', url, exc)
            detail = book
        add_watch(db, 'book', detail.get('title') or book['title'],
                  detail.get('author') or book.get('author') or
                  (watch['title'] if watch['kind'] == 'author' else watch.get('author')),
                  detail.get('source_url') or url,
                  detail.get('release_date') or book.get('release_date'),
                  {'sources': sources}, watch['id'])
        added += 1
    metadata['known_books'] = sorted(current)
    now = _now()
    db.execute("UPDATE watched_items SET metadata=?,last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(metadata, ensure_ascii=False), now.isoformat(sep='T'),
                _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    return added


def _release_time(value):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = dt.datetime.combine(dt.date.fromisoformat(text[:10]), dt.time.min)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.replace(tzinfo=None)


def _comic_chapters(watch, comic):
    manga_id = comic.get('mangadex_id') or mangadex_id_from_url(watch.get('source_url'))
    if manga_id:
        return list_mangadex_chapters(manga_id, comic.get('language') or 'en'), manga_id
    if comic.get('comicvine_id'):
        return [], None
    feed_url = comic.get('feed_url') or watch.get('source_url')
    if not feed_url:
        return [], None
    host = (urllib.parse.urlsplit(feed_url).hostname or '').casefold()
    if host in {'getcomics.info', 'www.getcomics.info', 'getcomics.org',
                'www.getcomics.org', 'comicvine.gamespot.com', 'anilist.co',
                'www.anilist.co', 'lnori.com', 'novelarchive.cc'}:
        # These are metadata/release pages, not RSS feeds. Their file or
        # chapter connectors are handled independently by the approved search.
        return [], None
    manifest = discover_feed(feed_url, watch['kind'], limit=200)
    return manifest.get('chapters') or [], None


def _process_comic_issue_watch(db, watch, library_folder, source_overrides=None):
    """Download a due issue through an explicitly approved comic connector."""
    metadata = json.loads(watch.get('metadata') or '{}')
    options = dict(metadata.get('sources') or {})
    options.update(source_overrides or {})
    rights_document = load_rights_evidence(options.get('rights_evidence_path'))
    rights = effective_work_rights(rights_document, watch, db)
    now = _now()
    has_rights_file = bool(options.get('rights_evidence_path'))
    mirror_approval = (rights if rights else
                       source_download_rights(rights_document, 'public_domain_comic_mirror'))
    getcomics_approval = (rights if rights else
                          source_download_rights(rights_document, 'getcomics.org'))
    if not has_rights_file:
        # Keep direct library/test callers usable; the GUI always supplies the
        # local file and therefore always follows its explicit yes/no values.
        mirror_approval = mirror_approval or {'approved_for_download': True}
        getcomics_approval = getcomics_approval or {'approved_for_download': True}
    if (has_rights_file and not any(
            item and item.get('approved_for_download') is True
            for item in (mirror_approval, getcomics_approval))):
        error = 'Local rights record is NO; automatic downloads are paused.'
        db.execute("UPDATE watched_items SET state='rights_hold',last_checked_at=?,next_check_at=NULL,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), error, watch['id']))
        db.commit()
        return 0
    results = []
    connector_errors = []
    mirror_base = options.get('comic_mirror_base')
    if mirror_base and mirror_approval:
        mirror_results = search_public_domain_comic_mirror(
            watch['title'], mirror_base, limit=20,
            manual_rights=mirror_approval if has_rights_file else None)
        for item in mirror_results:
            item.setdefault('source_key', 'public_domain_comic_mirror')
            if has_rights_file:
                item.setdefault('rights_evidence_path', options['rights_evidence_path'])
            if mirror_approval.get('rights_work_key'):
                item.setdefault('rights_work_key', mirror_approval['rights_work_key'])
        results.extend(mirror_results)
    if getcomics_approval:
        comic_candidates, connector_errors = _search_candidates(watch, source_overrides)
        for message in connector_errors:
            logger.warning('Comic issue watch %s connector error: %s', watch['id'], message)
        results.extend(item for item in comic_candidates
                       if item.get('source_key') == 'getcomics.org')
    requested_number = str((metadata.get('comic_issue') or {}).get('issue_number') or '').strip()
    candidate = next((item for item in results
                      if str(item.get('format') or '').casefold() in ('.cbz', '.cbr', '.pdf')
                      and (item.get('site') == 'public_domain_comic_mirror'
                           or item.get('source_key') == 'getcomics.org')
                      and _is_match(item, watch)
                      and _comic_issue_number_matches(item.get('title'), requested_number)), None)
    if candidate is None:
        error = 'No matching downloadable issue file was found from an approved connector.'
        if connector_errors:
            error += ' ' + '; '.join(connector_errors[-2:])
        db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), error, watch['id']))
        db.commit()
        return 0
    if rights.get('rights_work_key'):
        candidate.setdefault('rights_work_key', rights['rights_work_key'])
    if has_rights_file:
        candidate.setdefault('rights_evidence_path', options['rights_evidence_path'])
    row = enqueue(db, candidate)
    saved = download(db, row['id'], Path(library_folder).parent, destination=library_folder)
    db.execute("UPDATE watched_items SET state='downloaded',file_path=?,last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (saved.get('file_path'), now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    return 1


def _comic_issue_number_matches(title, requested_number):
    if not requested_number:
        return True
    match = re.search(r'(?:#|\bissue\s*(?:no\.?\s*)?)(\d+(?:\.\d+)?)\b',
                      str(title or ''), flags=re.I)
    if not match:
        return True
    try:
        return float(match.group(1)) == float(requested_number)
    except ValueError:
        return match.group(1) == requested_number


def _sync_comicvine_issues(db, watch, comic, source_overrides=None):
    """Create upcoming issue watches from Comic Vine store dates."""
    volume_id = str(comic.get('comicvine_id') or '')
    api_key = str((source_overrides or {}).get('comicvine_api_key')
                  or os.environ.get('COMICVINE_API_KEY') or '').strip()
    if not api_key:
        raise ValueError('Set COMICVINE_API_KEY to keep Comic Vine release tracking active')
    issues = list_comicvine_issues(volume_id, api_key, limit=100)
    known = dict(comic.get('known_issues') or {})
    initialized = bool(comic.get('issues_initialized'))
    options = dict(json.loads(watch.get('metadata') or '{}').get('sources') or {})
    options.update(source_overrides or {})
    # API keys are needed only for the current request; never copy them into
    # newly persisted child watch metadata.
    options.pop('comicvine_api_key', None)
    today = _now().date().isoformat()
    for issue in issues:
        issue_id = str(issue.get('comicvine_id') or '')
        if not issue_id:
            continue
        release = _release_time(issue.get('release_date'))
        release_date = release.date().isoformat() if release else None
        previous_date = known.get(issue_id)
        is_new = issue_id not in known
        known[issue_id] = release_date
        changed_date = previous_date != release_date
        if initialized:
            should_watch = is_new or (changed_date and bool(release_date))
        else:
            # First poll does not enqueue old issues; it only schedules announced issues.
            should_watch = bool(release_date and release_date > today)
        if not should_watch or not release_date:
            continue
        number = str(issue.get('number') or '').strip()
        issue_title = f"{watch['title']} #{number}" if number else f"{watch['title']} - {issue.get('title') or 'Issue'}"
        issue_url = issue.get('source_url') or f'https://comicvine.gamespot.com/issue/{issue_id}/'
        child_id = add_watch(db, 'comic_issue', issue_title, watch.get('author') or 'Unknown',
                             issue_url, release_date,
                             {'sources': options, 'comic_issue': {
                                 'comicvine_id': issue_id, 'volume_id': volume_id,
                                 'issue_number': number, 'issue_title': issue.get('title'),
                                 'store_date': issue.get('store_date'),
                             }}, watch['id'])
    comic['known_issues'] = known
    comic['issues_initialized'] = True
    # Due child rows are picked up on the next normal watch scan, which runs
    # shortly after the parent index refresh. This keeps one folder path owner.
    return 0


def _chapter_key(chapter):
    return str(chapter.get('mangadex_id') or chapter.get('url') or '')


def _chapter_serial_manifest(watch, chapter, manga_id, selector, source_key=None,
                             rights_evidence_path=None, rights_work_key=None):
    number = str(chapter.get('number') or '').strip()
    if not number:
        match = re.search(r'\bchapter\s*([0-9]+(?:\.[0-9]+)?)\b',
                          str(chapter.get('title') or ''), flags=re.I)
        number = match.group(1) if match else ''
    try:
        series_index = float(number) if number else None
    except ValueError:
        series_index = None
    title = str(chapter.get('title') or '').strip() or (f'Chapter {number}' if number else 'Latest chapter')
    entry = dict(chapter)
    if manga_id:
        entry['mangadex_id'] = chapter.get('mangadex_id')
    metadata = json.loads(watch.get('metadata') or '{}')
    comic = metadata.get('comic') or {}
    manifest = {
        'kind': watch['kind'], 'title': title, 'author': watch.get('author') or 'Unknown',
        'series': watch['title'], 'series_index': series_index,
        'source': chapter.get('url') or watch.get('source_url') or '',
        'selector': selector or 'article,main', 'chapters': [entry],
        'comic_metadata': comic.get('catalog_metadata') or {},
    }
    if source_key:
        manifest['source_key'] = source_key
    if rights_evidence_path:
        manifest['rights_evidence_path'] = rights_evidence_path
    if rights_work_key:
        manifest['rights_work_key'] = rights_work_key
    return manifest


def _process_comic_watch(db, watch, library_folder, source_overrides=None):
    metadata = json.loads(watch.get('metadata') or '{}')
    comic = dict(metadata.get('comic') or {})
    options = dict(metadata.get('sources') or {})
    options.update(source_overrides or {})
    rights_document = load_rights_evidence(options.get('rights_evidence_path'))
    rights = effective_work_rights(rights_document, watch, db)
    # A per-work NO is authoritative for every connector. With no work-level
    # choice, each source's own yes/no entry gates only that source below.
    restricted = bool(rights) and rights.get('approved_for_download') is not True
    errors = []
    if comic.get('comicvine_id'):
        try:
            _sync_comicvine_issues(db, watch, comic, source_overrides)
        except Exception as exc:
            logger.exception('Comic Vine issue lookup failed for comic watch %s', watch['id'])
            errors.append(f'Comic Vine: {exc}')
    comic_source = ('mangadex' if comic.get('mangadex_id') or
                    mangadex_id_from_url(watch.get('source_url')) else
                    'taddy' if comic.get('taddy_id') or comic.get('sss_url') else 'comic_rss_atom')
    chapter_approval = _approved_source_record(options, comic_source, watch, db)
    if not chapter_approval:
        chapters, manga_id = [], None
        errors.append(f'{comic_source} download source is NO in rights-evidence.json.')
        can_read_chapters = False
    else:
        chapters, manga_id = _comic_chapters(watch, comic)
        can_read_chapters = True
    seen = set(comic.get('known_chapters') or [])
    seen_files = set(comic.get('known_files') or [])
    initial_scan = can_read_chapters and not comic.get('feed_initialized')
    created_at = _release_time(watch.get('created_at'))
    now = _now()
    cutoff = created_at or now
    new_chapters = []
    for chapter in chapters:
        key = _chapter_key(chapter)
        if not key or key in seen:
            continue
        released = _release_time(chapter.get('published'))
        if released and released > now:
            continue
        if initial_scan and (released is None or released <= cutoff):
            # Establish a baseline when a watch is first added. Do not pull a
            # back catalog just because the first polling pass happened later.
            seen.add(key)
            continue
        new_chapters.append((key, chapter))
    if can_read_chapters:
        comic['feed_initialized'] = True

    downloaded = []
    # Keep the existing watch connectors in the comic workflow too. Only CBZ
    # candidates are useful here; EPUB/PDF hits belong to book watches.
    if restricted:
        candidates, connector_errors = [], []
        errors.append('Local rights record is NO; automatic downloads are paused.')
    elif comic.get('comicvine_id') and not comic.get('mangadex_id') and not comic.get('feed_url'):
        # The issue child watches handle date-gated downloads; do not search the
        # series-level query early and accidentally pick up an unreleased issue.
        candidates, connector_errors = [], []
    else:
        candidates, connector_errors = _search_candidates(watch, source_overrides)
    errors.extend(connector_errors)
    for error in connector_errors:
        logger.warning('Comic watch %s connector error: %s', watch['id'], error)
    for item in candidates:
        item_format = str(item.get('format') or '').casefold()
        if item_format not in {'.cbz', '.cbr'} and not (
                item_format == '.pdf' and item.get('source_key') in {
                    'public_domain_comic_mirror', 'getcomics.org'}):
            continue
        key = str(item.get('url') or '')
        if not key or key in seen_files:
            continue
        try:
            row = enqueue_irc(db, item['irc']) if item.get('irc') else enqueue(db, item)
            result = download(db, row['id'], Path(library_folder).parent,
                              destination=library_folder)
            seen_files.add(key)
            downloaded.append(result.get('file_path'))
        except Exception as exc:
            logger.exception('Comic connector result failed for watch %s: %s', watch['id'], item.get('title') or key)
            errors.append(f"{item.get('title') or key}: {exc}")
        if len(downloaded) >= 10:
            break

    for key, chapter in ([] if restricted or not can_read_chapters else new_chapters[:max(0, 10 - len(downloaded))]):
        try:
            manifest = _chapter_serial_manifest(
                watch, chapter, manga_id, comic.get('selector'), comic_source,
                options.get('rights_evidence_path'), chapter_approval.get('rights_work_key'))
            row = enqueue_serial(db, manifest)
            result = download(db, row['id'], Path(library_folder).parent, destination=library_folder)
            seen.add(key)
            downloaded.append(result.get('file_path'))
        except Exception as exc:
            logger.exception('Comic chapter failed for watch %s: %s', watch['id'], chapter.get('title') or key)
            errors.append(f"{chapter.get('title') or key}: {exc}")

    comic['known_chapters'] = sorted(seen)[-5000:]
    comic['known_files'] = sorted(seen_files)[-1000:]
    if manga_id:
        comic['mangadex_id'] = manga_id
    last_path = downloaded[-1] if downloaded else watch.get('file_path')
    last_error = '; '.join(errors[-2:]) if errors else None
    # Keep unprocessed releases visible to the next daily check. Failed entries
    # remain unseen and are retried after their download error is surfaced.
    db.execute('''UPDATE watched_items SET metadata=?,state='watching',file_path=?,
        last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (json.dumps({**metadata, 'comic': comic}, ensure_ascii=False), last_path,
         now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), last_error, watch['id']))
    db.commit()
    return len(downloaded)


def _process_webfiction_watch(db, watch, library_folder, source_overrides=None):
    metadata = json.loads(watch.get('metadata') or '{}')
    serial = dict(metadata.get('serial') or {})
    options = dict(metadata.get('sources') or {})
    options.update(source_overrides or {})
    feed_url = serial.get('feed_url') or watch.get('source_url')
    if not feed_url:
        raise ValueError('Add a public RSS/Atom chapter feed to track this web serial')
    feed_host = (urllib.parse.urlsplit(feed_url).hostname or '').casefold()
    chapter_source_key = 'novelarchive.cc' if feed_host == 'novelarchive.cc' else 'webfiction_rss_atom'
    chapter_approval = _approved_source_record(options, chapter_source_key, watch, db)
    if not chapter_approval:
        now = _now()
        error = f'{chapter_source_key} download source is NO in rights-evidence.json.'
        db.execute("UPDATE watched_items SET state='rights_hold',last_checked_at=?,next_check_at=NULL,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), error, watch['id']))
        db.commit()
        return 0
    if chapter_source_key == 'novelarchive.cc':
        manifest = discover_novelarchive_chapters(feed_url, limit=200)
    else:
        manifest = discover_feed(feed_url, watch['kind'], limit=200)
    chapters = manifest.get('chapters') or []
    seen = set(serial.get('known_chapters') or [])
    seen_files = set(serial.get('known_files') or [])
    initial_scan = not serial.get('feed_initialized')
    cutoff = _release_time(watch.get('created_at')) or _now()
    now = _now()
    new_chapters = []
    for chapter in chapters:
        key = _chapter_key(chapter)
        if not key or key in seen:
            continue
        released = _release_time(chapter.get('published'))
        if released and released > now:
            continue
        if initial_scan and (released is None or released <= cutoff):
            seen.add(key)
            continue
        new_chapters.append((key, chapter))
    serial['feed_initialized'] = True

    downloaded = []
    errors = []
    # Search the configured ebook connectors as well as the chapter feed.
    # Only text-book formats are relevant to this watch type.
    candidates, connector_errors = _search_candidates(watch, source_overrides)
    errors.extend(connector_errors)
    for error in connector_errors:
        logger.warning('Web fiction watch %s connector error: %s', watch['id'], error)
    for item in candidates:
        if str(item.get('format') or '').casefold() not in {'.epub', '.pdf', '.mobi', '.azw3'}:
            continue
        key = str(item.get('url') or '')
        if not key or key in seen_files:
            continue
        try:
            row = enqueue_irc(db, item['irc']) if item.get('irc') else enqueue(db, item)
            result = download(db, row['id'], Path(library_folder).parent,
                              destination=library_folder)
            seen_files.add(key)
            downloaded.append(result.get('file_path'))
        except Exception as exc:
            logger.exception('Web fiction connector result failed for watch %s: %s', watch['id'], item.get('title') or key)
            errors.append(f"{item.get('title') or key}: {exc}")
        if len(downloaded) >= 10:
            break

    for key, chapter in new_chapters[:max(0, 10 - len(downloaded))]:
        try:
            item_manifest = _chapter_serial_manifest(
                watch, chapter, None, serial.get('selector'), chapter_source_key,
                options.get('rights_evidence_path'), chapter_approval.get('rights_work_key'))
            row = enqueue_serial(db, item_manifest)
            result = download(db, row['id'], Path(library_folder).parent,
                              destination=library_folder)
            seen.add(key)
            downloaded.append(result.get('file_path'))
        except Exception as exc:
            logger.exception('Web fiction chapter failed for watch %s: %s', watch['id'], chapter.get('title') or key)
            errors.append(f"{chapter.get('title') or key}: {exc}")

    serial['known_chapters'] = sorted(seen)[-5000:]
    serial['known_files'] = sorted(seen_files)[-1000:]
    last_path = downloaded[-1] if downloaded else watch.get('file_path')
    last_error = '; '.join(errors[-2:]) if errors else None
    db.execute('''UPDATE watched_items SET metadata=?,state='watching',file_path=?,
        last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (json.dumps({**metadata, 'serial': serial}, ensure_ascii=False), last_path,
         now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), last_error, watch['id']))
    db.commit()
    return len(downloaded)


def _process_fanfiction_watch(db, watch, library_folder):
    """Download a whole fic once, then update its existing EPUB in place."""
    metadata = json.loads(watch.get('metadata') or '{}')
    fiction = dict(metadata.get('fanfiction') or {})
    story_url = fiction.get('story_url') or watch.get('source_url')
    if not story_url:
        raise ValueError('Enter a public story URL for this fanfiction watch')
    item = {
        'title': watch['title'],
        'author': watch.get('author') or 'Unknown',
        'format': '.epub',
        'url': story_url_key(story_url),
        'source': story_url,
        'story_url': story_url,
        'kind': 'fanfiction',
    }
    row = enqueue(db, item)
    result = download(db, row['id'], Path(library_folder).parent,
                      destination=library_folder)
    saved = json.loads(result['metadata'])
    fiction.update({'story_url': story_url, 'item_id': result['id']})
    metadata['fanfiction'] = fiction
    now = _now()
    db.execute('''UPDATE watched_items SET title=?,author=?,metadata=?,state='watching',file_path=?,
        last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (saved.get('title') or watch['title'], saved.get('author') or watch.get('author') or 'Unknown',
         json.dumps(metadata, ensure_ascii=False), result.get('file_path'),
         now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    return 1


def _process_lightnovel_watch(db, watch, library_folder, source_overrides=None):
    """Track RanobeDB volume dates and schedule searches only for English editions.

    Existing English backlist is used as a baseline when the watch is created;
    only upcoming or subsequently added English releases become child watches.
    """
    metadata = json.loads(watch.get('metadata') or '{}')
    profile = dict(metadata.get('lightnovel') or {})
    series_id = profile.get('ranobedb_id')
    if not series_id:
        match = re.search(r'/series/(\d+)(?:/|$)', urllib.parse.urlsplit(
            str(watch.get('source_url') or '')).path)
        series_id = match.group(1) if match else None
    if not series_id:
        # LNORI and NovelArchive can be watched directly when the user has a
        # source URL but no RanobeDB release-date record.
        return int(_search_book(db, watch, library_folder, source_overrides))
    series = ranobedb_series(series_id)
    books = series.get('books') or []
    seen = dict(profile.get('known_volumes') or {})
    initialized = bool(profile.get('series_initialized'))
    created_at = _release_time(watch.get('created_at')) or _now()
    today = _now().date()
    known = {}
    new_release_ids = []
    upcoming_count = 0
    available_count = 0
    for book in books:
        if not isinstance(book, dict) or book.get('id') is None:
            continue
        book_id = str(book['id'])
        release_dates = book.get('c_release_dates') or {}
        english_date = english_release_date(release_dates.get('en'))
        old = seen.get(book_id)
        known[book_id] = english_date
        if english_date:
            release = dt.date.fromisoformat(english_date)
            if release > today:
                upcoming_count += 1
            else:
                available_count += 1
            # The first read establishes the old catalog as a baseline. Keep
            # future English dates so they can start searching on release day.
            is_new_release = initialized and old != english_date
            if not initialized and release > today:
                is_new_release = True
            if is_new_release:
                new_release_ids.append((book, english_date))

    profile['ranobedb_id'] = str(series_id)
    profile['known_volumes'] = known
    profile['series_initialized'] = True
    if upcoming_count:
        profile['english_release_status'] = 'upcoming'
    elif available_count:
        profile['english_release_status'] = 'available'
    else:
        profile['english_release_status'] = 'not_listed'
    profile['english_available_volumes'] = available_count
    profile['english_upcoming_volumes'] = upcoming_count
    sources = metadata.get('sources') or {}
    downloaded = 0
    for book, release_date in new_release_ids:
        title = str(book.get('title') or f"{watch['title']} volume")
        book_url = f"https://ranobedb.org/book/{book['id']}"
        child_id = add_watch(
            db, 'book', title, watch.get('author') or 'Unknown', book_url,
            release_date, {'sources': sources, 'lightnovel_release': {
                'ranobedb_series_id': str(series_id), 'ranobedb_book_id': str(book['id']),
                'english_official': True,
            }}, watch['id'])
        child = db.execute('SELECT * FROM watched_items WHERE id=?', (child_id,)).fetchone()
        if child and child['state'] != 'downloaded' and release_date <= today.isoformat():
            downloaded += int(bool(_search_book(db, dict(child), library_folder, source_overrides)))

    now = _now()
    db.execute('''UPDATE watched_items SET metadata=?,state='watching',last_checked_at=?,
        next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (json.dumps({**metadata, 'lightnovel': profile}, ensure_ascii=False),
         now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    return downloaded


def process_due_watches(db, library_folder, force=False, source_options=None):
    """Check due watches. Future books stay visible and begin searching on release day."""
    now = _now()
    today = now.date().isoformat()
    rows = [dict(row) for row in db.execute("SELECT * FROM watched_items WHERE state NOT IN ('downloaded') ORDER BY id")]
    checked = 0
    for watch in rows:
        if watch['kind'] not in ('series', 'author') and watch.get('release_date') and watch['release_date'] > today:
            due_at = f"{watch['release_date']}T00:00:00"
            if watch['state'] != 'upcoming' or watch.get('next_check_at') != due_at:
                db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (due_at, watch['id']))
                db.commit()
            continue
        next_check = watch.get('next_check_at')
        if not force and next_check and next_check > now.isoformat(sep='T'):
            continue
        checked += 1
        if watch['kind'] == 'lightnovel':
            try:
                _process_lightnovel_watch(db, watch, library_folder, source_options)
            except Exception as exc:
                logger.exception('Light-novel watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] in ('series', 'author'):
            try:
                _process_goodreads_collection(db, watch)
            except Exception as exc:
                logger.exception('Goodreads watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] in SERIAL_KINDS:
            try:
                watch_metadata = json.loads(watch.get('metadata') or '{}')
                if watch['kind'] == 'fanfiction' and watch_metadata.get('fanfiction'):
                    _process_fanfiction_watch(db, watch, library_folder)
                else:
                    _process_webfiction_watch(db, watch, library_folder, source_options)
            except Exception as exc:
                logger.exception('Fanfiction watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] in COMIC_KINDS:
            try:
                _process_comic_watch(db, watch, library_folder, source_options)
            except Exception as exc:
                logger.exception('Comic watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] == 'comic_issue':
            try:
                metadata = json.loads(watch.get('metadata') or '{}')
                options = dict(metadata.get('sources') or {})
                options.update(source_options or {})
                rights_document = load_rights_evidence(options.get('rights_evidence_path'))
                rights = effective_work_rights(rights_document, watch, db)
                if not force and watch['state'] == 'rights_hold' and (
                        rights.get('status') == 'restricted' or rights.get('approved_for_download') is False):
                    continue
                _process_comic_issue_watch(db, watch, library_folder, source_options)
            except Exception as exc:
                logger.exception('Comic issue watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        db.execute("UPDATE watched_items SET state='searching',last_checked_at=?,next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
        db.commit()
        _search_book(db, watch, library_folder, source_options)
    return checked
