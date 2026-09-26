"""Goodreads watch-list metadata and scheduled source searches."""
import datetime as dt
import difflib
import html
import json
import re
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from .core import download, enqueue, search_catalog, search_opds
from .irc import enqueue_irc, search_irc
from .sources import resolve_detail, search_openlibrary, search_site

CHECK_INTERVAL = dt.timedelta(days=1)


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
    raw, final_url = http_get(url)
    return parse_goodreads_page(final_url, raw.decode('utf-8', errors='replace'))


def search_goodreads(query, limit=80):
    """Search Goodreads book, series, and author result pages for selectable entries."""
    from .core import http_get
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a Goodreads search up to 150 characters')
    results, seen, errors = [], set(), []
    for search_type in ('books', 'authors', 'series'):
        url = 'https://www.goodreads.com/search?' + urllib.parse.urlencode(
            {'q': query, 'search_type': search_type})
        try:
            raw, final_url = http_get(url)
            parser = GoodreadsParser()
            parser.feed(raw.decode('utf-8', errors='replace'))
        except Exception as exc:
            errors.append(str(exc))
            continue
        for anchor in parser.anchors:
            href = anchor.get('href', '')
            path = urllib.parse.urlsplit(href).path.casefold()
            if '/book/show/' in path:
                kind = 'book'
                result_url = _canonical_book_url(urllib.parse.urljoin(final_url, href))
            elif '/author/show/' in path:
                kind = 'author'
                result_url = urllib.parse.urljoin(final_url, href).split('?', 1)[0].split('#', 1)[0]
            elif '/series/' in path and '/series/list/' not in path:
                kind = 'series'
                result_url = urllib.parse.urljoin(final_url, href).split('?', 1)[0].split('#', 1)[0]
            else:
                continue
            title = ' '.join((anchor.get('title') or anchor.get('text') or '').split())
            title = re.sub(r'\s*\([^)]*\)\s*$', '', title).strip()
            key = (kind, result_url)
            if not title or key in seen:
                continue
            seen.add(key)
            results.append({'kind': kind, 'title': title, 'source_url': result_url})
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
    if kind not in ('book', 'series', 'author'):
        raise ValueError('Watch type must be book, series, or author')
    release_date = str(release_date or '').strip() or None
    if release_date:
        try:
            release_date = dt.date.fromisoformat(release_date).isoformat()
        except ValueError as exc:
            raise ValueError('Release date must be YYYY-MM-DD') from exc
    if source_url:
        parsed = urllib.parse.urlsplit(source_url)
        host = (parsed.hostname or '').casefold()
        if parsed.scheme not in ('http', 'https') or not (host == 'goodreads.com' or host.endswith('.goodreads.com')):
            raise ValueError('Enter a public Goodreads URL')
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


def _search_candidates(watch, source_overrides=None):
    query = ' '.join(part for part in (watch['title'], watch.get('author')) if part and part != 'Unknown')
    options = dict(json.loads(watch.get('metadata') or '{}').get('sources') or {})
    options.update(source_overrides or {})
    results, errors = [], []
    if options.get('openlibrary', True):
        try:
            results.extend(search_openlibrary(query, limit=15))
        except Exception as exc:
            errors.append(f'Open Library: {exc}')
    for feed_url in options.get('opds_feeds', []):
        try:
            results.extend(search_opds(feed_url, query))
        except Exception as exc:
            errors.append(f'OPDS {feed_url}: {exc}')
    for catalog_path in options.get('catalogs', []):
        try:
            results.extend(search_catalog(catalog_path, query))
        except Exception as exc:
            errors.append(f'Catalog {catalog_path}: {exc}')
    for site in ('libgen', 'annas'):
        if not options.get(site, True):
            continue
        try:
            found = search_site(site, query, base=options.get(site + '_base') or None)
            for hit in found[:12]:
                if not hit.get('detail_url') or not _is_match(hit, watch):
                    continue
                try:
                    results.extend(resolve_detail(site, hit['detail_url'], hit.get('title') or watch['title'],
                                                  hit.get('author') or watch.get('author') or 'Unknown',
                                                  hit.get('format')))
                except Exception as exc:
                    errors.append(f'{site}: {exc}')
        except Exception as exc:
            errors.append(f'{site}: {exc}')
    irc_configs = options.get('irc_configs') or ([options['irc_config']] if options.get('irc_config') else [])
    for irc_config_path in irc_configs:
        try:
            config = json.loads(Path(irc_config_path).read_text(encoding='utf-8'))
            results.extend(search_irc(query, config, limit=100))
        except Exception as exc:
            errors.append(f'IRC: {exc}')
    return [item for item in results if _is_match(item, watch)], errors


def _search_book(db, watch, library_folder, source_overrides=None):
    results, errors = _search_candidates(watch, source_overrides)
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
    for url, book in current.items():
        if url in known:
            if book.get('release_date') and book['release_date'] > dt.date.today().isoformat():
                book_author = book.get('author') or (watch['title'] if watch['kind'] == 'author' else watch.get('author'))
                add_watch(db, 'book', book['title'], book_author,
                          url, book['release_date'], {'sources': sources}, watch['id'])
            continue
        try:
            detail = fetch_goodreads(url)
        except Exception:
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


def process_due_watches(db, library_folder, force=False, source_options=None):
    """Check due watches. Future books stay visible and begin searching on release day."""
    now = _now()
    today = now.date().isoformat()
    rows = [dict(row) for row in db.execute("SELECT * FROM watched_items WHERE state NOT IN ('downloaded') ORDER BY id")]
    checked = 0
    for watch in rows:
        if watch['kind'] == 'book' and watch.get('release_date') and watch['release_date'] > today:
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
        if watch['kind'] in ('series', 'author'):
            try:
                _process_goodreads_collection(db, watch)
            except Exception as exc:
                db.execute("UPDATE watched_items SET last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        db.execute("UPDATE watched_items SET state='searching',last_checked_at=?,next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
        db.commit()
        _search_book(db, watch, library_folder, source_options)
    return checked
