"""Goodreads watch-list metadata and scheduled source searches."""
import datetime as dt
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
import hashlib
import html
import json
import logging
import queue
import os
import re
import shutil
import sqlite3
import unicodedata
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from .book_identifiers import book_isbns, book_work_title_key, collect_isbns
from .comics import list_comicvine_issues, list_mangadex_chapters, mangadex_id_from_url
from .comics import fetch_anilist_media, anilist_video_subtype
from .comic_sources import (resolve_getcomics_post, search_getcomics,
                            search_public_domain_comic_mirror)
from .rights_evidence import (effective_work_rights, load_rights_evidence,
                              source_download_rights)
from .media_catalogs import (discover_novelarchive_chapters, english_release_date,
                             ranobedb_series, resolve_catalog_file_links,
                             search_ranobedb,
                             search_lnori, search_novelarchive,
                             fetch_hardcover_list)
from .kindle import get_local_secret
from .core import (AUDIO_FORMATS, BOOK_FORMATS, COMIC_FORMATS, EXTENSIONS,
                   GAME_FORMATS, GAME_PACKAGE_FORMATS, SOFTWARE_FORMATS,
                   VIDEO_FORMATS, connect, download, enqueue,
                   enqueue_for_watch,
                   http_get, queue_watch_search, search_catalog, search_opds,
                   update_watch_search)
from .media_graph import (add_media_relationship, queue_graph_review, sync_watch_entity,
                          effective_watch_policy, upsert_media_entity,
                          get_watch_policy, relationship_watch_action,
                          continuation_watch_policy_key,
                          _watch_policy_has_override,
                          upsert_media_release, capture_graph_snapshot,
                          record_catalog_metadata_variants)
from .media_providers import (fetch_igdb_game, fetch_steam_game, fetch_tmdb_collection,
                              fetch_tmdb_media, fetch_tmdb_release_dates,
                              fetch_tmdb_season,
                              fetch_youtube_playlist_items,
                              fetch_tvmaze_show, list_tvmaze_episodes,
                              list_tvmaze_seasons)
from .series_continuity import (is_unscheduled_announced_member,
                                series_member_review_reason)
from .fanfiction import story_url_key
from .download_connectors import (ANIME_DOWNLOAD_ADAPTERS,
                                  GENERAL_DOWNLOAD_ADAPTERS,
                                  resolve_meawfy_downloads,
                                  resolve_site_downloads,
                                  resolve_softarchive_downloads,
                                  formats_for_category, search_internet_archive, search_meawfy,
                                  search_github_foss, search_softarchive,
                                  search_registered_download_site,
                                  search_sourceforge_foss)
from .irc import enqueue_irc, has_configured_source, search_configured_sources
from .serial import discover_feed, enqueue_serial
from .sources import (resolve_detail, search_google_books_metadata,
                      search_openlibrary_metadata, search_site)
from .wikidata import import_wikidata_entity, search_wikidata, search_wikipedia_entities

CHECK_INTERVAL = dt.timedelta(days=1)
_BOOK_SERIES_DESCRIPTION = re.compile(
    r'\b(?:books?|novels?|novellas?|short stories|literary works?)\s+'
    r'(?:series|saga|cycle|trilogy|sequence)\b|'
    r'\b(?:series|saga|cycle|duology|trilogy|tetralogy|pentalogy|hexalogy|heptalogy|'
    r'quartet|quintet|sequence)\s+of\b.{0,80}'
    r'\b(?:books?|novels?|novellas?|short stories|literary works?)\b|'
    r"\b(?:children's|young adult|fantasy|science fiction|adventure|mystery|"
    r'romance|horror|historical fiction)\b.{0,60}\b(?:series|saga|cycle|trilogy)\b', re.I)
_BOOK_MEMBER_TYPE = re.compile(
    r'\b(?:books?|light novels?|web novels?|novels?|novellas?|short stories|'
    r'literary works?|written works?|poetry collections?)\b', re.I)
_NON_BOOK_MEMBER_TYPE = re.compile(
    r'\b(?:films?|movies?|television|tv series|episodes?|video games?|computer games?|'
    r'songs?|albums?|podcasts?|comics?|graphic novels?|manga|manhwa|manhua|anime)\b', re.I)
COMIC_KINDS = {'comic', 'manga', 'webcomic', 'webtoon'}
SERIAL_KINDS = {'webfiction', 'fanfiction'}
_IRC_FORMATS_BY_KIND = {
    'book': BOOK_FORMATS | AUDIO_FORMATS,
    'series': BOOK_FORMATS | AUDIO_FORMATS,
    'lightnovel': BOOK_FORMATS | AUDIO_FORMATS,
    'comic': COMIC_FORMATS | {'.pdf'},
    'comic_issue': COMIC_FORMATS | {'.pdf'},
    'manga': COMIC_FORMATS | {'.pdf'},
    'webcomic': COMIC_FORMATS | {'.pdf'},
    'webtoon': COMIC_FORMATS | {'.pdf'},
    'anime': VIDEO_FORMATS,
    'video': VIDEO_FORMATS,
    'video_episode': VIDEO_FORMATS,
    'game': GAME_PACKAGE_FORMATS,
    'software': SOFTWARE_FORMATS,
    'webfiction': BOOK_FORMATS,
    'fanfiction': BOOK_FORMATS,
}
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
            self.anchor = {
                'href': attrs.get('href', ''), 'title': attrs.get('title', ''), 'text': '',
                'series_index': (attrs.get('data-series-position') or
                                 attrs.get('data-series-index') or
                                 attrs.get('data-position')),
            }
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
    iso = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', text)
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


def _series_index(value):
    """Return a numeric series position when a provider supplies one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number) if number.is_integer() else number
    match = re.search(r'(?<!\d)(\d+(?:\.\d+)?)', str(value).strip())
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def _infer_publication_order(books):
    """Place unnumbered, dated companion works between numbered books."""
    numbered = [book for book in books
                if book.get('series_index') is not None and book.get('release_date')]
    numbered.sort(key=lambda book: (book['release_date'], book['series_index']))
    for book in books:
        if book.get('series_index') is not None or not book.get('release_date'):
            continue
        before = [candidate for candidate in numbered
                  if candidate['release_date'] <= book['release_date']]
        after = [candidate for candidate in numbered
                 if candidate['release_date'] > book['release_date']]
        left = before[-1] if before else None
        right = after[0] if after else None
        if left and right and right['series_index'] - left['series_index'] >= 1:
            book['series_index'] = (left['series_index'] + right['series_index']) / 2
        elif left and not right:
            book['series_index'] = left['series_index'] + 0.5
        elif right and not left:
            book['series_index'] = right['series_index'] - 0.5
        else:
            continue
        book['series_position_source'] = 'Publication date between numbered books'
    return sorted(books, key=lambda book: (
        book.get('series_index') is None,
        book.get('series_index') if book.get('series_index') is not None else float('inf'),
        book.get('release_date') or '9999-99-99',
        _normalized(book.get('title'))))


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
    position = node.get('seriesPosition') or node.get('position')
    book = {
        'title': title,
        'author': author,
        'release_date': _date(node.get('datePublished') or node.get('dateCreated')),
        'series_index': _series_index(position),
        'series_position_source': 'Goodreads metadata' if _series_index(position) is not None else None,
        'source_url': _canonical_book_url(urllib.parse.urljoin(page_url, str(href))),
    }
    isbns = collect_isbns(node.get('isbn'), node.get('identifier'))
    if isbns:
        book['catalog_metadata'] = {'isbns': isbns}
    return book


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
    is_book_list = ('/list/show/' in path or '/review/list/' in path or
                    '/user/show/' in path and bool(urllib.parse.parse_qs(
                        urllib.parse.urlsplit(page_url).query).get('shelf')))
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
        position = _series_index(anchor.get('series_index'))
        if position is not None and entry.get('series_index') is None:
            entry['series_index'] = position
            entry['series_position_source'] = 'Goodreads series listing'
        found_books[book_url] = entry

    canonical = parser.meta.get('og:url') or parser.meta.get('twitter:url') or page_url
    canonical = urllib.parse.urljoin(page_url, html.unescape(canonical))
    kind = ('book_list' if is_book_list else
            'author' if is_author else
            'series' if is_series or _type_has(series_node, 'BookSeries') else 'book')
    return {
        'kind': kind,
        'title': title or 'Untitled Goodreads entry',
        'author': author,
        'source_url': canonical,
        'release_date': release,
        'books': list(found_books.values()) if kind in ('series', 'author', 'book_list') else [],
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
    parsed_page = parse_goodreads_page(final_url, raw.decode('utf-8', errors='replace'))
    if parsed_page.get('kind') not in ('series', 'author', 'book_list'):
        return parsed_page

    # Goodreads splits long series and author bibliographies over pages. Follow
    # them until a page adds no new books, with a generous cap for very large lists.
    collected = {book['source_url']: book for book in parsed_page.get('books', [])
                 if book.get('source_url')}
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    try:
        start_page = max(1, int((query.get('page') or ['1'])[0]))
    except (TypeError, ValueError):
        start_page = 1
    page_url = urllib.parse.urlsplit(url)
    for page_number in range(start_page + 1, start_page + 101):
        page_query = urllib.parse.parse_qs(page_url.query, keep_blank_values=True)
        page_query['page'] = [str(page_number)]
        next_url = urllib.parse.urlunsplit((
            page_url.scheme, page_url.netloc, page_url.path,
            urllib.parse.urlencode(page_query, doseq=True), page_url.fragment))
        try:
            page_raw, final = http_get(next_url)
            page = parse_goodreads_page(final, page_raw.decode('utf-8', errors='replace'))
        except Exception as exc:
            parsed_page['collection_error'] = str(_goodreads_access_error(exc))
            logger.info('Goodreads collection pagination stopped after %d page(s): %s',
                        page_number - start_page, parsed_page['collection_error'])
            break
        if page.get('kind') not in ('series', 'author', 'book_list'):
            break
        before = len(collected)
        for book in page.get('books', []):
            if book.get('source_url'):
                collected.setdefault(book['source_url'], book)
        if len(collected) == before:
            break
    parsed_page['books'] = list(collected.values())
    parsed_page['collection_pages'] = page_number - start_page if 'page_number' in locals() else 1
    return parsed_page


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
    limit = max(1, min(int(limit), 80))
    results, errors = [], []
    per_type_limit = max(1, (limit + 2) // 3)
    for search_type in ('books', 'authors', 'series'):
        url = 'https://www.goodreads.com/search?' + urllib.parse.urlencode(
        {'q': query, 'search_type': search_type})
        try:
            raw, final_url = http_get(url)
            if not raw:
                raise ValueError('Goodreads returned an empty search response, likely because its redirect was blocked')
            page = raw.decode('utf-8', errors='replace')
            parsed = _goodreads_results_from_html(
                query, final_url, page, per_type_limit)
            if not parsed:
                parser = GoodreadsParser()
                parser.feed(page)
                logger.warning('Goodreads %s search returned no recognized results for %r '
                               '(final URL %s, %d links)', search_type, query, final_url,
                               len(parser.anchors))
        except Exception as exc:
            errors.append(str(_goodreads_access_error(exc)))
            continue
        results.extend(parsed)
    deduplicated = {}
    for result in results:
        deduplicated[(result['kind'], result['source_url'])] = result
    results = list(deduplicated.values())
    # Reserve space for all three Goodreads result kinds. Previously a full
    # books page returned early and prevented the series query from running.
    kind_order = {'series': 0, 'author': 1, 'book': 2}
    results.sort(key=lambda item: (kind_order.get(item.get('kind'), 3),
                                   _normalized(item.get('title')),
                                   item.get('source_url') or ''))
    results = results[:limit]
    unique_errors = list(dict.fromkeys(errors))
    if not results and unique_errors:
        raise ValueError('Goodreads search failed: ' + '; '.join(unique_errors[-2:]))
    if results and unique_errors:
        logger.warning('Goodreads search for %r returned %d partial result(s); %d request(s) failed',
                       query, len(results), len(unique_errors))
    return results


def search_goodreads_with_fallback(query, limit=80):
    """Search Goodreads first and add an alternate series match when it lacks one."""
    goodreads_error = None
    goodreads_results = []
    try:
        goodreads_results = search_goodreads(query, limit)
        if any(result.get('kind') == 'series' for result in goodreads_results):
            for result in goodreads_results:
                result.setdefault('provider', 'Goodreads')
            return goodreads_results
    except Exception as exc:
        goodreads_error = exc
        logger.warning('Goodreads search unavailable for %r; trying alternate catalogs: %s',
                       query, exc)
    alternate_results = []
    for provider, searcher in (
            ('Open Library', search_openlibrary_metadata),
            ('Google Books', search_google_books_metadata)):
        try:
            alternate_results.extend(searcher(query, limit=min(limit, 30)))
        except Exception as exc:
            logger.warning('%s metadata fallback failed for %r: %s', provider, query, exc)
    if goodreads_error and not goodreads_results and not alternate_results:
        logger.info('Goodreads and alternate catalog searches failed for %r', query)
    series_results = [item for item in alternate_results if item.get('kind') == 'series']
    if not series_results and not any(item.get('kind') == 'series' for item in goodreads_results):
        try:
            series_results = _search_wikidata_series(query)
        except Exception as exc:
            logger.warning('Wikidata series fallback failed for %r: %s', query, exc)
    if goodreads_results:
        known = {_normalized(item.get('title')) for item in goodreads_results
                 if item.get('kind') == 'series'}
        fallback = [item for item in series_results
                    if _normalized(item.get('title')) not in known]
        for result in goodreads_results:
            result.setdefault('provider', 'Goodreads')
        if fallback:
            logger.info('Added %d alternate series suggestion(s) for %r', len(fallback), query)
        return goodreads_results + fallback
    if alternate_results:
        logger.info('Alternate catalogs supplied %d metadata suggestion(s) for %r',
                    len(alternate_results), query)
        return alternate_results + [item for item in series_results
                                    if item not in alternate_results]
    if series_results:
        return series_results
    if goodreads_error:
        raise ValueError(f'{goodreads_error}; alternate catalogs found no metadata matches')
    return []


_BARE_QID = re.compile(r'Q[1-9]\d{0,11}')
_GENERIC_SERIES_TAIL = re.compile(
    r'(?:\s+(?:series|saga|cycle|trilogy|duology|quartet|chronicles|sequence|books|novels|collection))+\s*$', re.I)


def _is_book_series(description):
    # "comic book series" and "manga series" are found in the Comics and Manga categories.
    return bool(_BOOK_SERIES_DESCRIPTION.search(description)) and not re.search(
        r'\b(?:comic|comics|manga|manhwa|graphic novels?)\b', description, re.I)


def _series_words_removed(value):
    """Normalized series name without a leading article or a generic trailing word."""
    text = re.sub(r'^(?:the|a|an)\s+', '', _normalized(value))
    return _GENERIC_SERIES_TAIL.sub('', text).strip() or text
_EDITION_MEMBER = re.compile(r'\b(?:translation|edition|reprint|omnibus|box(?:ed)? set)\s+of\b|^(?:version, edition|edition|translation)\b', re.I)


def _wikidata_series_members(qid, limit=5000):
    """Read explicitly linked member works from Wikidata for a series fallback."""
    qid = str(qid or '').strip().upper()
    if not re.fullmatch(r'Q[1-9]\d{0,11}', qid):
        raise ValueError('Invalid Wikidata series ID')
    limit = max(1, min(int(limit), 5000))
    query = f'''SELECT ?work ?workLabel ?englishTitle ?workDescription ?authorLabel ?date ?seriesNumber
      ?workTypeLabel WHERE {{
      {{ ?work p:P179 ?membership.
         ?membership ps:P179 wd:{qid}.
         ?membership wikibase:rank ?rank.
         FILTER(?rank != wikibase:DeprecatedRank)
         OPTIONAL {{ ?membership pq:P1545 ?seriesNumber. }} }}
      UNION
      {{ ?work p:P361 ?partOf.
         ?partOf ps:P361 wd:{qid}.
         ?partOf wikibase:rank ?rank.
         FILTER(?rank != wikibase:DeprecatedRank)
         OPTIONAL {{ ?partOf pq:P1545 ?seriesNumber. }} }}
      OPTIONAL {{ ?work rdfs:label ?englishTitle. FILTER(LANG(?englishTitle) = "en") }}
      OPTIONAL {{ ?work wdt:P50 ?author. }}
      OPTIONAL {{ ?work wdt:P577 ?date. }}
      OPTIONAL {{ ?work wdt:P31 ?workType. }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en,mul". }}
    }} ORDER BY ?seriesNumber ?date ?workLabel LIMIT {limit}'''
    url = 'https://query.wikidata.org/sparql?' + urllib.parse.urlencode(
        {'query': query, 'format': 'json'})
    raw, final = http_get(url, max_bytes=8_000_000)
    if (urllib.parse.urlsplit(final).hostname or '').casefold() not in {
            'query.wikidata.org', 'www.wikidata.org', 'wikidata.org'}:
        raise ValueError('Wikidata series lookup redirected away from Wikidata')
    payload = json.loads(raw.decode('utf-8'))
    books = {}
    for binding in (payload.get('results') or {}).get('bindings', []):
        work_url = str((binding.get('work') or {}).get('value') or '')
        match = re.fullmatch(r'https?://www\.wikidata\.org/entity/(Q[1-9]\d{0,11})', work_url)
        # Prefer the English label: the multilingual fallback can name a work in
        # another language, and a bare QID means the work has no usable label.
        title = str((binding.get('englishTitle') or binding.get('workLabel') or {}).get('value') or '').strip()
        if not match or not title or _BARE_QID.fullmatch(title):
            continue
        work_id = match.group(1)
        release = _date((binding.get('date') or {}).get('value'))
        position_text = str((binding.get('seriesNumber') or {}).get('value') or '').strip()
        position = _series_index(position_text)
        book = books.setdefault(work_id, {
            'title': title, 'author': 'Unknown', 'source_url': work_url,
            'release_date': release,
            'series_index': position,
            'series_position': position_text or None,
            'series_position_source': 'Wikidata P1545' if position is not None else None,
            'description': str((binding.get('workDescription') or {}).get('value') or ''),
            'work_type_labels': [],
            'catalog_metadata': {'catalog_provider': 'Wikidata', 'catalog_id': work_id,
                                 'series_position': position_text or None},
        })
        type_label = str((binding.get('workTypeLabel') or {}).get('value') or '').strip()
        if type_label and type_label not in book['work_type_labels']:
            book['work_type_labels'].append(type_label)
        if position is not None and book.get('series_index') is None:
            book['series_index'] = position
            book['series_position'] = position_text
            book['series_position_source'] = 'Wikidata P1545'
            book['catalog_metadata']['series_position'] = position_text
        author = str((binding.get('authorLabel') or {}).get('value') or '').strip()
        if author and not _BARE_QID.fullmatch(author) and book['author'] == 'Unknown':
            book['author'] = author
        if release and (not book.get('release_date') or release < book['release_date']):
            book['release_date'] = release
    members = list(books.values())
    eligible_members = []
    for book in members:
        type_text = ' '.join(book.get('work_type_labels') or [])
        description = str(book.get('description') or '')
        book['catalog_metadata']['work_type_labels'] = sorted(book['work_type_labels'])
        if _EDITION_MEMBER.search(description) or _EDITION_MEMBER.search(type_text):
            continue  # a translation or edition of a member, not another book
        if _NON_BOOK_MEMBER_TYPE.search(type_text):
            # Keep adaptations and other media in the graph via the entity
            # relationship importer, but don't present them as book members.
            continue
        if not (_BOOK_MEMBER_TYPE.search(type_text) or
                _BOOK_MEMBER_TYPE.search(description)):
            book['needs_review'] = True
            book['review_reason'] = (
                'Wikidata links this item to the series, but does not identify it '
                'as a book, novel, or written work.')
            eligible_members.append(book)
            continue
        # A relationship to a series alone is weak evidence: Wikidata can
        # contain placeholder or non-book parts. Require either an explicit
        # series position or a publication date before automatic queueing.
        if book.get('series_index') is not None:
            book['member_evidence'] = 'explicit Wikidata series position'
        elif book.get('release_date'):
            book['member_evidence'] = 'Wikidata publication date'
        else:
            book['needs_review'] = True
            book['review_reason'] = (
                'Wikidata links this item to the series but provides no series '
                'number or publication date.')
        eligible_members.append(book)
    return _infer_publication_order(eligible_members)


def _search_wikidata_series(query):
    """Offer series candidates from Wikidata and read their explicitly linked works."""
    found = []
    queries = [str(query or '').strip()]
    corrected = re.sub(r'([a-z])\1+', r'\1', queries[0], flags=re.I)
    if corrected and corrected.casefold() != queries[0].casefold():
        queries.append(corrected)
    by_qid = {}
    for search_query in queries:
        for entity in search_wikidata(search_query, limit=20):
            description = str(entity.get('description') or '')
            if _is_book_series(description):
                by_qid.setdefault(entity.get('qid'), (entity, search_query))
    if not by_qid:
        # A common word ("Foundation") ranks a foundation, a film and a company
        # above Asimov's books, and a descriptive word ("Dune Chronicles", "The
        # Silo Series") hides the series' own label; ask for "<name> series".
        base = re.sub(r'^(?:the|a|an)\s+', '', _GENERIC_SERIES_TAIL.sub('', queries[0]).strip(), flags=re.I)
        for entity in search_wikidata((base or queries[0]) + ' series', limit=10):
            if _is_book_series(str(entity.get('description') or '')):
                by_qid.setdefault(entity.get('qid'), (entity, queries[0]))
    if not by_qid:
        # Wikidata's search matches labels only; Wikipedia searches the articles,
        # so "The Millennium Trilogy" finds the Millennium novel series.
        try:
            for entity in search_wikipedia_entities(queries[0], limit=10):
                if _is_book_series(str(entity.get('description') or '')):
                    by_qid.setdefault(entity.get('qid'), (entity, queries[0]))
        except Exception as exc:
            logger.info('Wikipedia series search failed for %r: %s', queries[0], exc)
    entities = list(by_qid.values())
    def score(entity):
        title_key = _series_words_removed(entity.get('title'))
        return difflib.SequenceMatcher(None, _series_words_removed(query), title_key).ratio()
    entities = [pair for pair in entities if score(pair[0]) >= 0.5]
    entities.sort(key=lambda pair: (-score(pair[0]), _normalized(pair[0].get('title'))))
    # Each candidate costs a SPARQL query; Wikimedia throttles bursts, and the
    # best three cover a series and its likely alternates.
    for entity, matched_query in entities[:3]:
        description = str(entity.get('description') or '')
        item = dict(entity)
        item['kind'] = 'series'
        item['author'] = 'Unknown'
        item['catalog_metadata'] = {'catalog_id': entity.get('qid'),
                                    'description': description}
        if matched_query != queries[0]:
            item['query_correction'] = {'from': queries[0], 'to': matched_query}
        try:
            members = _wikidata_series_members(entity.get('qid'), limit=5000)
            item['books'] = [member for member in members if not member.get('needs_review')]
            item['review_members'] = [member for member in members if member.get('needs_review')]
            item['catalog_metadata']['member_count'] = len(item['books'])
            item['catalog_metadata']['review_member_count'] = len(item['review_members'])
        except Exception as exc:
            item['books'] = []
            item['review_members'] = []
            item['catalog_metadata']['member_lookup_error'] = str(exc)
            logger.warning('Wikidata series member lookup failed for %s: %s',
                           entity.get('qid'), exc)
        found.append(item)
    return found


def search_book_metadata(query, limit=40):
    """Search book and series metadata providers together, returning selectable entries."""
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a book, series, or author search up to 150 characters')
    limit = max(1, min(int(limit), 60))
    searches = (
        ('Goodreads', lambda: search_goodreads(query, limit=30)),
        ('Open Library', lambda: search_openlibrary_metadata(query, limit=20)),
        ('Google Books', lambda: search_google_books_metadata(query, limit=20)),
        ('NovelArchive', lambda: search_novelarchive(query, limit=12)),
        ('LNORI', lambda: search_lnori(query, limit=12)),
        ('RanobeDB', lambda: search_ranobedb(query, limit=12)),
        ('Wikidata', lambda: _search_wikidata_series(query)),
    )
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=len(searches)) as pool:
        futures = [(provider, pool.submit(searcher)) for provider, searcher in searches]
        for provider, future in futures:
            try:
                found = future.result()
            except Exception as exc:
                errors.append(f'{provider}: {exc}')
                logger.warning('%s metadata lookup failed for %r: %s', provider, query, exc)
                continue
            for raw in found or []:
                item = dict(raw)
                item['provider'] = str(item.get('provider') or provider)
                item['source_url'] = item.get('source_url') or item.get('url') or ''
                item['url'] = item.get('url') or item['source_url']
                if item.get('metadata') and not item.get('catalog_metadata'):
                    item['catalog_metadata'] = item['metadata']
                for book in item.get('books') or []:
                    if isinstance(book, dict):
                        book.setdefault('provider', item['provider'])
                name = item['provider'].casefold()
                if name == 'novelarchive':
                    item['kind'] = 'webfiction'
                elif name in {'lnori', 'ranobedb'}:
                    item['kind'] = 'lightnovel'
                else:
                    item.setdefault('kind', 'book')
                if item['source_url'] and item.get('title'):
                    results.append(item)
    deduplicated = {}
    for item in results:
        deduplicated[(item['provider'], item['kind'], item['source_url'])] = item
    found = []
    series_results = {}
    for item in deduplicated.values():
        if item.get('kind') not in ('series', 'author'):
            found.append(item)
            continue
        normalized_title = _normalized(item.get('title'))
        key = (item.get('kind'), normalized_title) if normalized_title else None
        if not key:
            found.append(item)
            continue
        primary = series_results.get(key)
        if primary is None:
            primary = dict(item)
            primary['books'] = list(item.get('books') or []) if item.get('kind') == 'series' else []
            provider = str(item.get('provider') or 'Metadata catalog')
            if item.get('source_url'):
                primary.setdefault('catalog_sources', {}).setdefault(provider, []).append(
                    item['source_url'])
            catalog_id = str((item.get('catalog_metadata') or {}).get('catalog_id') or '')
            if catalog_id:
                primary.setdefault('catalog_ids', {}).setdefault(provider, []).append(catalog_id)
            series_results[key] = primary
            found.append(primary)
        provider = str(item.get('provider') or 'Metadata catalog')
        source_map = primary.setdefault('catalog_sources', {})
        source_urls = source_map.setdefault(provider, [])
        if item.get('source_url') and item['source_url'] not in source_urls:
            source_urls.append(item['source_url'])
        catalog_id = str((item.get('catalog_metadata') or {}).get('catalog_id') or '')
        if catalog_id:
            id_map = primary.setdefault('catalog_ids', {})
            ids = id_map.setdefault(provider, [])
            if catalog_id not in ids:
                ids.append(catalog_id)
        if item.get('kind') == 'series':
            books_by_url = {book.get('source_url'): book for book in primary['books']
                            if book.get('source_url')}
            for book in item.get('books') or []:
                source_url = book.get('source_url')
                if source_url and source_url not in books_by_url:
                    primary['books'].append(book)
                    books_by_url[source_url] = book
        else:
            provider = str(item.get('provider') or 'Metadata catalog')
            source_map = primary.setdefault('catalog_sources', {})
            urls = source_map.setdefault(provider, [])
            if item.get('source_url') and item['source_url'] not in urls:
                urls.append(item['source_url'])
    book_index = {}
    compact = []
    for item in found:
        if item.get('kind') != 'book':
            compact.append(item)
            continue
        title_key = _normalized(item.get('title'))
        author_key = _normalized(item.get('author'))
        key = (title_key, author_key) if title_key and author_key and author_key != 'unknown' else None
        primary = book_index.get(key) if key else None
        if primary is None:
            compact.append(item)
            if key:
                book_index[key] = item
                provider = str(item.get('provider') or 'Metadata catalog')
                if item.get('source_url'):
                    item.setdefault('catalog_sources', {}).setdefault(provider, []).append(
                        item['source_url'])
                catalog_id = str((item.get('catalog_metadata') or {}).get('catalog_id') or '')
                if catalog_id:
                    item.setdefault('catalog_ids', {}).setdefault(provider, []).append(catalog_id)
            continue
        source_map = primary.setdefault('catalog_sources', {})
        provider = str(item.get('provider') or 'Metadata catalog')
        urls = source_map.setdefault(provider, [])
        if item.get('source_url') and item['source_url'] not in urls:
            urls.append(item['source_url'])
        ids = primary.setdefault('catalog_ids', {})
        catalog_id = str((item.get('catalog_metadata') or {}).get('catalog_id') or '')
        if catalog_id:
            ids.setdefault(provider, [])
            if catalog_id not in ids[provider]:
                ids[provider].append(catalog_id)
        if not primary.get('release_date') and item.get('release_date'):
            primary['release_date'] = item['release_date']
    compact.sort(key=lambda item: 0 if item.get('kind') == 'series' else 1)
    found = compact[:limit]
    if not found and errors:
        raise ValueError('Book metadata search failed: ' + '; '.join(errors))
    return found


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
    if kind not in ({'book', 'series', 'author', 'book_list', 'lightnovel', 'comic_issue', 'video', 'video_episode', 'game', 'software'} | COMIC_KINDS | SERIAL_KINDS):
        raise ValueError('Unsupported watch type')
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
    if kind in {'video', 'game'}:
        # Keep refreshing catalog data while a video/game is upcoming. Release
        # dates and lifecycle status are often revised well before launch.
        next_check = _next_day(now).isoformat(sep='T')
    elif state == 'upcoming':
        next_check = f'{release_date}T00:00:00'
    else:
        next_check = now.isoformat(sep='T')
    metadata_value = dict(metadata or {})
    if parent_id:
        parent = db.execute('SELECT metadata FROM watched_items WHERE id=?',
                            (int(parent_id),)).fetchone()
        if parent:
            try:
                parent_metadata = json.loads(parent['metadata'] or '{}')
            except (TypeError, ValueError):
                parent_metadata = {}
            inherited_ids = parent_metadata.get('library_profile_ids') or []
            if not isinstance(inherited_ids, list):
                inherited_ids = [inherited_ids]
            current_ids = metadata_value.get('library_profile_ids') or []
            if not isinstance(current_ids, list):
                current_ids = [current_ids]
            profile_ids = list(dict.fromkeys(str(value).strip()
                                             for value in [*current_ids, *inherited_ids]
                                             if str(value).strip()))
            if profile_ids:
                metadata_value['library_profile_ids'] = profile_ids
                _apply_profile_delivery_settings(
                    metadata_value, profile_ids, _library_profiles(db))
    payload = json.dumps(metadata_value, ensure_ascii=False)

    def merge_saved_refresh_state(watch_id):
        nonlocal payload
        old = db.execute('SELECT metadata FROM watched_items WHERE id=?',
                         (int(watch_id),)).fetchone()
        if not old:
            return
        try:
            old_metadata = json.loads(old['metadata'] or '{}')
            next_metadata = json.loads(payload or '{}')
        except (TypeError, ValueError):
            return
        for key in ('announcement_feed_state', 'announcement_feed_last_checked_at'):
            if key not in next_metadata and key in old_metadata:
                next_metadata[key] = old_metadata[key]
        old_profile_ids = old_metadata.get('library_profile_ids') or []
        new_profile_ids = next_metadata.get('library_profile_ids') or []
        if not isinstance(old_profile_ids, list):
            old_profile_ids = [old_profile_ids]
        if not isinstance(new_profile_ids, list):
            new_profile_ids = [new_profile_ids]
        profile_ids = list(dict.fromkeys(str(value).strip()
                                         for value in [*old_profile_ids, *new_profile_ids]
                                         if str(value).strip()))
        if profile_ids:
            next_metadata['library_profile_ids'] = profile_ids
            _apply_profile_delivery_settings(
                next_metadata, profile_ids, _library_profiles(db))
        if isinstance(old_metadata.get('announcement_feed_state'), dict):
            merged_state = dict(old_metadata['announcement_feed_state'])
            merged_state.update(next_metadata.get('announcement_feed_state') or {})
            next_metadata['announcement_feed_state'] = merged_state
        for key in ('video_feed_url', 'video_feed_source_url', 'video_feed_baselined_at',
                    'video_feed_last_checked_at', 'video_feed_membership',
                    'video_feed_title', 'video_feed_author', 'video_feed_provider_metadata',
                    'youtube_playlist_api_error', 'youtube_playlist_state',
                    'youtube_playlist_last_partial_scan_at'):
            if not next_metadata.get(key) and old_metadata.get(key):
                next_metadata[key] = old_metadata[key]
        for key in ('video_feed_known_episodes', 'video_feed_watched_episodes'):
            old_values = old_metadata.get(key)
            new_values = next_metadata.get(key)
            if isinstance(old_values, list) or isinstance(new_values, list):
                next_metadata[key] = list(dict.fromkeys(
                    [*(old_values if isinstance(old_values, list) else []),
                     *(new_values if isinstance(new_values, list) else [])]))[-5000:]
        old_seen = old_metadata.get('video_feed_seen_at')
        new_seen = next_metadata.get('video_feed_seen_at')
        if isinstance(old_seen, dict) or isinstance(new_seen, dict):
            merged_seen = dict(old_seen) if isinstance(old_seen, dict) else {}
            merged_seen.update(new_seen if isinstance(new_seen, dict) else {})
            next_metadata['video_feed_seen_at'] = dict(list(merged_seen.items())[-5000:])
        old_item_metadata = old_metadata.get('video_feed_item_metadata')
        new_item_metadata = next_metadata.get('video_feed_item_metadata')
        if isinstance(old_item_metadata, dict) or isinstance(new_item_metadata, dict):
            merged_items = dict(old_item_metadata) if isinstance(old_item_metadata, dict) else {}
            merged_items.update(new_item_metadata if isinstance(new_item_metadata, dict) else {})
            next_metadata['video_feed_item_metadata'] = dict(list(merged_items.items())[-5000:])
        payload = json.dumps(next_metadata, ensure_ascii=False)

    def find_by_source():
        if not source_url:
            return None
        # Prefer the canonical row over any legacy duplicate kept from older data.
        return db.execute('''SELECT id,state FROM watched_items WHERE source_url=?
            ORDER BY legacy_duplicate, id LIMIT 1''', (source_url,)).fetchone()

    def refresh_existing(existing):
        if existing['state'] == 'downloaded':
            return int(existing['id'])
        merge_saved_refresh_state(existing['id'])
        db.execute('''UPDATE watched_items SET title=?,author=?,release_date=?,state=?,
            next_check_at=?,metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   (title, author or 'Unknown', release_date, state, next_check,
                    payload, existing['id']))
        db.commit()
        sync_watch_entity(db, int(existing['id']))
        db.commit()
        return int(existing['id'])

    existing = find_by_source()
    if existing:
        return refresh_existing(existing)

    catalog_value = metadata_value.get('catalog_metadata') or {}
    external_ids = list(metadata_value.get('external_ids') or [])
    if isinstance(catalog_value, dict):
        external_ids.extend(catalog_value.get('external_ids') or [])
    linked_watch_ids = set()
    for external_id in external_ids:
        if not isinstance(external_id, dict):
            continue
        provider = str(external_id.get('provider') or '').casefold().strip()
        identifier = str(external_id.get('external_id') or '').strip()
        if not provider or not identifier:
            continue
        linked = db.execute('''SELECT w.id FROM media_external_ids AS x
            JOIN watched_items AS w ON w.media_entity_id=x.entity_id
            WHERE x.provider=? AND x.external_id=?''', (provider, identifier)).fetchall()
        linked_watch_ids.update(int(row['id']) for row in linked)
    if len(linked_watch_ids) > 1:
        raise ValueError('The supplied catalog IDs point to multiple watched items; resolve the identity before adding')
    if linked_watch_ids:
        watch_id = next(iter(linked_watch_ids))
        existing = db.execute('SELECT id,state FROM watched_items WHERE id=?',
                              (watch_id,)).fetchone()
        if existing:
            if existing['state'] == 'downloaded':
                return watch_id
            merge_saved_refresh_state(watch_id)
            db.execute('''UPDATE watched_items SET title=?,author=?,release_date=?,state=?,
                next_check_at=?,metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                       (title, author or 'Unknown', release_date, state, next_check,
                        payload, watch_id))
            db.commit()
            sync_watch_entity(db, watch_id)
            db.commit()
            return watch_id
    try:
        cursor = db.execute('''INSERT INTO watched_items
            (kind,title,author,source_url,release_date,state,next_check_at,parent_id,metadata)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (kind, title, author or 'Unknown', source_url, release_date, state, next_check,
             parent_id, payload))
    except sqlite3.IntegrityError:
        # Another process added this source between the lookup and the insert.
        db.rollback()
        existing = find_by_source()
        if not existing:
            raise
        return refresh_existing(existing)
    db.commit()
    watch_id = int(cursor.lastrowid)
    sync_watch_entity(db, watch_id)
    db.commit()
    return watch_id


def set_watch_release_date(db, watch_id, release_date):
    """Set or clear a book watch's release date and reschedule its search."""
    row = db.execute('SELECT * FROM watched_items WHERE id=?', (int(watch_id),)).fetchone()
    if not row:
        raise ValueError('The selected watched item no longer exists')
    watch = dict(row)
    if watch.get('kind') != 'book':
        raise ValueError('Set a release date on a book watch')
    text = str(release_date or '').strip() or None
    if text:
        try:
            text = dt.date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValueError('Release date must be YYYY-MM-DD') from exc
    metadata = json.loads(watch.get('metadata') or '{}')
    if text:
        metadata['release_date_source'] = 'manual'
    else:
        metadata.pop('release_date_source', None)
    now = _now()
    awaiting_date = is_unscheduled_announced_member(metadata) and not text
    if text and text > now.date().isoformat():
        state, next_check, upcoming = 'upcoming', f'{text}T00:00:00', True
    elif awaiting_date:
        state, next_check, upcoming = 'upcoming', _next_day(now).isoformat(sep='T'), True
    else:
        state, next_check, upcoming = 'watching', now.isoformat(sep='T'), False
    db.execute('''UPDATE watched_items SET release_date=?,state=?,next_check_at=?,metadata=?,
        last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
               (text, state, next_check, json.dumps(metadata, ensure_ascii=False), watch_id))
    queue_watch_search(db, watch_id, watch['title'], watch.get('author'), text,
                       upcoming=upcoming, series=metadata.get('series'),
                       series_index=metadata.get('series_index'),
                       publication_status=metadata.get('publication_status'))
    sync_watch_entity(db, watch_id)
    db.commit()
    return dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())


def add_from_goodreads(db, url, kind=None, title=None, author='Unknown', release_date=None,
                       sources=None):
    parsed = {}
    if url:
        parsed = fetch_goodreads(url)
    return add_from_goodreads_metadata(db, parsed, url, kind, title, author, release_date, sources)


def add_from_goodreads_metadata(db, parsed, url, kind=None, title=None, author='Unknown',
                                release_date=None, sources=None):
    """Add a catalog watch and create one queued search request per discovered book."""
    detected_kind = kind or parsed.get('kind') or 'book'
    name = title or parsed.get('title')
    if not name or name == 'Untitled Goodreads entry':
        raise ValueError('Could not read a title from Goodreads. Enter the title manually.')
    writer = author if author and author != 'Unknown' else parsed.get('author', 'Unknown')
    if detected_kind == 'author':
        writer = 'Unknown'
    date = release_date or parsed.get('release_date')
    saved = {'sources': sources or {}}
    if parsed.get('provider'):
        saved['catalog_provider'] = parsed['provider']
    if parsed.get('catalog_metadata'):
        saved['catalog_metadata'] = parsed['catalog_metadata']
    for key in ('catalog_sources', 'catalog_ids'):
        if parsed.get(key):
            saved[key] = parsed[key]
    if parsed.get('series_plan'):
        saved['series_plan'] = parsed['series_plan']
    if parsed.get('series'):
        saved['series'] = str(parsed['series']).strip()
    if parsed.get('series_index') is not None:
        saved['series_index'] = parsed['series_index']
    review_members = []
    if detected_kind == 'series':
        review_members = [dict(item) for item in parsed.get('review_members', [])
                          if isinstance(item, dict) and item.get('title')]
        if review_members:
            saved['series_review_candidates'] = review_members
            parsed['series_review_candidates'] = review_members
    if detected_kind in ('series', 'author'):
        saved['known_books'] = [item.get('source_url') for item in parsed.get('books', []) if item.get('source_url')]
    if detected_kind == 'author' and parsed.get('provider') == 'Open Library' and not parsed.get('books'):
        saved['openlibrary_baseline_pending'] = True
    elif detected_kind == 'author' and not parsed.get('books'):
        saved['goodreads_baseline_pending'] = True
    watch_id = add_watch(db, detected_kind, name, writer, parsed.get('source_url') or url,
                         date, saved)
    if detected_kind in ('series', 'author'):
        watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
        _sync_series_members_graph(db, watch, parsed.get('books', []))
        if review_members:
            series_entity_id = watch.get('media_entity_id') or sync_watch_entity(db, watch_id)
            for candidate in review_members:
                catalog = candidate.get('catalog_metadata') or {}
                candidate_id = str(catalog.get('catalog_id') or '').strip()
                queue_graph_review(
                    db, 'uncertain_series_member',
                    f"Review possible member of {name}: {candidate['title']}",
                    {'series': name, 'candidate': candidate,
                     'reason': candidate.get('review_reason')},
                    entity_id=series_entity_id,
                    dedupe_key=f'series_member_candidate:{watch_id}:{candidate_id or candidate["title"]}')
            db.commit()
    if detected_kind in ('series', 'author'):
        for book in parsed.get('books', []):
            if not book.get('title'):
                continue
            fallback_author = name if detected_kind == 'author' else writer
            book_author = _catalog_author(book.get('author'), fallback_author)
            _add_catalog_book_watch(db, book, book_author, sources, watch_id,
                                    parsed.get('provider') or 'Goodreads')
    return watch_id, parsed


def _sync_series_members_graph(db, watch, books):
    """Save explicit series membership or creator credit in the media graph."""
    watch_kind = str(watch.get('kind') or '')
    if watch_kind not in {'series', 'author'}:
        return 0
    parent_entity_id = watch.get('media_entity_id') or sync_watch_entity(db, watch['id'])
    saved = 0
    provider = str((json.loads(watch.get('metadata') or '{}')).get('catalog_provider') or 'Goodreads')
    for book in books or []:
        title = str(book.get('title') or '').strip()
        source_url = str(book.get('source_url') or '').strip()
        if not title or not source_url:
            continue
        book_provider = str(book.get('provider') or provider)
        path = urllib.parse.urlsplit(source_url).path
        match = re.search(r'/book/show/(\d+)', path, flags=re.I)
        wikidata_match = re.search(r'/entity/(Q[1-9]\d{0,11})$', path, flags=re.I)
        openlibrary_match = re.search(r'/(?:works|books)/(OL\d+[WM])(?:/|$)', path, flags=re.I)
        catalog = book.get('catalog_metadata') or book.get('metadata') or {}
        external_id = str(catalog.get('catalog_id') or catalog.get('qid') or '').strip()
        if not external_id:
            external_id = (match.group(1) if match else
                           wikidata_match.group(1) if wikidata_match else
                           openlibrary_match.group(1) if openlibrary_match else source_url)
        external_provider = book_provider.casefold().replace(' ', '')
        matched_watch = db.execute('''SELECT w.media_entity_id FROM watched_items w
            WHERE w.kind='book' AND w.parent_id=? AND (
                w.source_url=? OR EXISTS (SELECT 1 FROM media_external_ids x
                    WHERE x.entity_id=w.media_entity_id AND x.provider=? AND x.external_id=?))
            LIMIT 1''', (int(watch['id']), source_url, external_provider,
                         str(external_id))).fetchone()
        review_reason = (series_member_review_reason(watch.get('title'), book)
                         if watch_kind == 'series' else None)
        if review_reason and not matched_watch:
            _queue_series_member_review(db, watch['id'], book, review_reason)
            continue
        canonical_title = title
        if matched_watch and matched_watch['media_entity_id']:
            canonical = db.execute('SELECT title FROM media_entities WHERE id=?',
                                   (int(matched_watch['media_entity_id']),)).fetchone()
            if canonical and canonical['title']:
                canonical_title = canonical['title']
        external_ids = []
        catalog_sources = book.get('catalog_sources') or {}
        catalog_ids = book.get('catalog_ids') or {}
        for source_name, ids in catalog_ids.items():
            urls = catalog_sources.get(source_name) or []
            for index, source_id in enumerate(ids if isinstance(ids, list) else [ids]):
                if source_id:
                    external_ids.append({
                        'provider': str(source_name).casefold().replace(' ', ''),
                        'external_id': str(source_id),
                        'source_url': urls[index] if index < len(urls) else None,
                    })
        if not any(value['provider'] == external_provider and
                   value['external_id'] == str(external_id) for value in external_ids):
            external_ids.append({'provider': external_provider, 'external_id': external_id,
                                 'source_url': source_url})
        member_id = upsert_media_entity(
            db, 'work', canonical_title, media_type='book', canonical_url=source_url,
            release_date=book.get('release_date'),
            metadata={'catalog_provider': book_provider},
            aliases=([title] if title != canonical_title else None),
            external_ids=external_ids, provider=book_provider)
        if watch_kind == 'author':
            existing_membership = db.execute('''SELECT source_entity_id,target_entity_id,
                    relationship_type FROM media_relationships
                WHERE source_entity_id=? AND target_entity_id=?
                  AND relationship_type='created_by'
                ORDER BY id LIMIT 1''',
                (int(member_id), int(parent_entity_id))).fetchone()
            relationship_source, relationship_target = int(member_id), int(parent_entity_id)
            relationship_type = 'created_by'
        else:
            existing_membership = db.execute('''SELECT source_entity_id,target_entity_id,
                    relationship_type FROM media_relationships
                WHERE (source_entity_id=? AND target_entity_id=?
                       AND relationship_type IN ('contains','includes_work'))
                   OR (source_entity_id=? AND target_entity_id=?
                       AND relationship_type IN ('part_of','parent_series'))
                ORDER BY CASE relationship_type WHEN 'contains' THEN 0
                    WHEN 'part_of' THEN 1 ELSE 2 END, id LIMIT 1''',
                (int(parent_entity_id), int(member_id), int(member_id),
                 int(parent_entity_id))).fetchone()
            if existing_membership:
                relationship_source = int(existing_membership['source_entity_id'])
                relationship_target = int(existing_membership['target_entity_id'])
                relationship_type = existing_membership['relationship_type']
            else:
                relationship_source, relationship_target = int(parent_entity_id), int(member_id)
                relationship_type = 'contains'
        add_media_relationship(
            db, relationship_source, relationship_target, relationship_type, confidence=0.9,
            inferred=False, status='accepted', provider=book_provider,
            provider_entity_id=external_id,
            raw_relationship_type=('author bibliography' if watch_kind == 'author'
                                   else 'series listing'),
            source_url=watch.get('source_url'),
            evidence={'title': title, 'member_url': source_url,
                      'release_date': book.get('release_date'),
                      'series_index': book.get('series_index'),
                      'series_position_source': book.get('series_position_source'),
                      'author': watch.get('title') if watch_kind == 'author' else None},
            is_explicit=True)
        saved += 1
    db.commit()
    return saved


def _queue_series_member_review(db, parent_watch_id, book, reason):
    parent = db.execute('SELECT * FROM watched_items WHERE id=?',
                        (int(parent_watch_id),)).fetchone()
    if not parent or parent['kind'] != 'series':
        return None
    entity_id = parent['media_entity_id'] or sync_watch_entity(db, int(parent_watch_id))
    candidate = {key: book.get(key) for key in (
        'title', 'author', 'provider', 'entity_type', 'source_url', 'release_date', 'series_index',
        'series_position', 'series_position_source', 'catalog_metadata')
        if book.get(key) is not None}
    candidate['review_reason'] = reason
    identity = str(book.get('source_url') or book.get('title') or '').casefold()
    dedupe_key = f'series-member-review:{parent_watch_id}:{identity}'
    previous = db.execute('SELECT id FROM media_review_queue WHERE dedupe_key=?',
                           (dedupe_key,)).fetchone()
    if previous:
        return int(previous['id'])
    return queue_graph_review(
        db, 'uncertain_series_member',
        f"Review possible non-work entry in {parent['title']}: {book.get('title')}",
        {'series': parent['title'], 'candidate': candidate, 'reason': reason},
        entity_id=entity_id,
        dedupe_key=dedupe_key)


def list_watches(db):
    return [dict(row) for row in db.execute('''SELECT * FROM watched_items
        ORDER BY CASE state WHEN 'upcoming' THEN 0 WHEN 'watching' THEN 1 ELSE 2 END,
        release_date, title COLLATE NOCASE''')]


def remove_watch(db, watch_id):
    child_ids = [int(row['id']) for row in db.execute(
        'SELECT id FROM watched_items WHERE parent_id=?', (watch_id,))]
    for queued_watch_id in child_ids + [int(watch_id)]:
        db.execute('DELETE FROM items WHERE url=?',
                   (f'watch-search://{queued_watch_id}',))
        db.execute('UPDATE webcomic_sources SET watch_id=NULL WHERE watch_id=?',
                   (queued_watch_id,))
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


def _catalog_author(value, fallback='Unknown'):
    value = str(value or '').strip()
    if value and _normalized(value) != 'unknown':
        return value
    fallback = str(fallback or '').strip()
    return fallback if fallback and _normalized(fallback) != 'unknown' else 'Unknown'


def _add_catalog_book_watch(db, book, author, sources, parent_id, provider,
                            force_add=False):
    """Create or merge one work watch by stable catalog URL or exact title/author."""
    title = str(book.get('title') or '').strip()
    if not title:
        return None, False
    author = _catalog_author(book.get('author'), author)
    source_url = str(book.get('source_url') or '').strip() or None
    catalog = book.get('catalog_metadata') or book.get('metadata') or {}
    catalog_id = str(catalog.get('catalog_id') or catalog.get('qid') or '').strip()
    source_provider = str(book.get('provider') or provider or 'Goodreads').strip()
    source_provider_key = source_provider.casefold().replace(' ', '')
    if not catalog_id and source_url:
        parts = urllib.parse.urlsplit(source_url)
        if (parts.hostname or '').casefold().endswith('goodreads.com'):
            match = re.search(r'/book/show/(\d+)', parts.path, flags=re.I)
            catalog_id = match.group(1) if match else ''
        elif (parts.hostname or '').casefold().endswith('openlibrary.org'):
            match = re.search(r'/(?:works|books)/(OL\d+[WM])(?:/|$)', parts.path, flags=re.I)
            catalog_id = match.group(1) if match else ''
        elif (parts.hostname or '').casefold().endswith('wikidata.org'):
            match = re.search(r'/entity/(Q[1-9]\d{0,11})$', parts.path, flags=re.I)
            catalog_id = match.group(1) if match else ''
    review_reason = None
    collection_label = None
    inherited_profile_ids = []
    if parent_id:
        parent = db.execute('SELECT kind,title,metadata FROM watched_items WHERE id=?',
                            (int(parent_id),)).fetchone()
        review_reason = (series_member_review_reason(parent['title'], book)
                         if parent and parent['kind'] == 'series' else None)
        if parent and parent['kind'] == 'book_list':
            collection_label = str(parent['title'] or '').strip()
        if parent:
            try:
                inherited = json.loads(parent['metadata'] or '{}')
            except (TypeError, ValueError):
                inherited = {}
            inherited_profile_ids = inherited.get('library_profile_ids') or []
            if not isinstance(inherited_profile_ids, list):
                inherited_profile_ids = [inherited_profile_ids]
    existing = None
    if source_url:
        existing = db.execute('''SELECT * FROM watched_items WHERE kind='book' AND source_url=?
            ORDER BY id LIMIT 1''', (source_url,)).fetchone()
    if not existing and catalog_id:
        linked = db.execute('''SELECT w.* FROM media_external_ids AS x
            JOIN watched_items AS w ON w.media_entity_id=x.entity_id
            WHERE x.provider=? AND x.external_id=? AND w.kind='book'
            ORDER BY w.id LIMIT 1''', (source_provider_key, catalog_id)).fetchone()
        existing = linked
    if not existing:
        title_key, author_key = book_work_title_key(title), _normalized(author)
        incoming_isbns = set(book_isbns(book))
        if title_key:
            for row in db.execute("SELECT * FROM watched_items WHERE kind='book'"):
                row_author = _normalized(row['author'])
                exact_author = author_key != 'unknown' and row_author == author_key
                author_compatible = (exact_author or author_key == 'unknown' or
                                     row_author == 'unknown')
                same_parent_with_missing_author = (
                    parent_id is not None and row['parent_id'] == int(parent_id) and
                    (author_key == 'unknown' or row_author == 'unknown'))
                same_title = (book_work_title_key(row['title']) == title_key and
                              (exact_author or same_parent_with_missing_author))
                same_isbn = False
                if not same_title and incoming_isbns and author_compatible:
                    try:
                        row_metadata = json.loads(row['metadata'] or '{}')
                    except (TypeError, ValueError):
                        row_metadata = {}
                    same_isbn = bool(incoming_isbns.intersection(book_isbns(row_metadata)))
                if same_title or same_isbn:
                    existing = row
                    break
    if review_reason and not force_add and not existing:
        _queue_series_member_review(db, parent_id, book, review_reason)
        db.commit()
        return None, False

    def record_catalog_identity(metadata):
        if not catalog_id:
            return
        ids = metadata.setdefault('catalog_ids', {})
        if not isinstance(ids, dict):
            ids = metadata['catalog_ids'] = {}
        values = ids.setdefault(source_provider, [])
        if not isinstance(values, list):
            values = ids[source_provider] = [values]
        if catalog_id not in values:
            values.append(catalog_id)
        generic = metadata.setdefault('external_ids', [])
        if not isinstance(generic, list):
            generic = metadata['external_ids'] = []
        if not any(isinstance(value, dict) and
                   str(value.get('provider') or '').casefold() == source_provider_key and
                   str(value.get('external_id') or '') == catalog_id for value in generic):
            generic.append({'provider': source_provider_key, 'external_id': catalog_id,
                            'source_url': source_url})

    def record_collection_label(metadata):
        if not collection_label:
            return
        labels = metadata.setdefault('book_lists', [])
        if not isinstance(labels, list):
            labels = metadata['book_lists'] = [labels] if labels else []
        if collection_label not in labels:
            labels.append(collection_label)

    def record_profile_membership(metadata):
        profile_ids = metadata.get('library_profile_ids') or []
        if not isinstance(profile_ids, list):
            profile_ids = [profile_ids]
        profile_ids = list(dict.fromkeys(
            str(value).strip() for value in [*profile_ids, *inherited_profile_ids]
            if str(value).strip()))
        if profile_ids:
            metadata['library_profile_ids'] = profile_ids
            _apply_profile_delivery_settings(metadata, profile_ids, _library_profiles(db))

    def record_isbn_identities(metadata):
        generic = metadata.setdefault('external_ids', [])
        if not isinstance(generic, list):
            generic = metadata['external_ids'] = [generic]
        for isbn in book_isbns(book):
            if not any(isinstance(value, dict) and
                       str(value.get('provider') or '').casefold() == 'isbn' and
                       str(value.get('external_id') or '') == isbn for value in generic):
                generic.append({'provider': 'isbn', 'external_id': isbn,
                                'source_url': source_url})

    def merge_catalog_provenance(metadata):
        for key in ('catalog_sources', 'catalog_ids'):
            incoming = book.get(key) or {}
            if not isinstance(incoming, dict):
                continue
            target = metadata.setdefault(key, {})
            if not isinstance(target, dict):
                target = metadata[key] = {}
            for provider_name, values in incoming.items():
                values = values if isinstance(values, list) else [values]
                saved = target.setdefault(str(provider_name), [])
                if not isinstance(saved, list):
                    saved = target[str(provider_name)] = [saved]
                for value in values:
                    if value is not None and str(value).strip() and value not in saved:
                        saved.append(value)

    def catalog_variant_evidence():
        variants = book.get('metadata_variants') or []
        if variants:
            return variants
        return [{
            'provider': source_provider, 'title': title, 'author': author,
            'source_url': source_url, 'release_date': book.get('release_date'),
            'publication_status': book.get('publication_status'),
            'series_index': book.get('series_index'),
            'series_position': book.get('series_position'),
            'series_position_source': book.get('series_position_source'),
            'catalog_metadata': dict(catalog) if isinstance(catalog, dict) else {},
        }]

    def merge_member_evidence(metadata):
        for key in ('catalog_metadata_variants', 'catalog_metadata_conflicts'):
            incoming = (catalog_variant_evidence() if key.endswith('variants')
                        else book.get('metadata_conflicts') or [])
            if not incoming:
                continue
            saved = metadata.setdefault(key, [])
            if not isinstance(saved, list):
                saved = metadata[key] = [saved]
            saved_signatures = {json.dumps(value, sort_keys=True, ensure_ascii=False)
                                for value in saved}
            for value in incoming:
                signature = json.dumps(value, sort_keys=True, ensure_ascii=False)
                if signature not in saved_signatures:
                    saved.append(value)
                    saved_signatures.add(signature)

    series_name = str(book.get('series') or '').strip()
    series_index = book.get('series_index')
    series_position_source = book.get('series_position_source')
    if existing:
        watch_id = int(existing['id'])
        metadata = json.loads(existing['metadata'] or '{}')
        record_collection_label(metadata)
        record_profile_membership(metadata)
        record_catalog_identity(metadata)
        record_isbn_identities(metadata)
        merge_catalog_provenance(metadata)
        source_map = metadata.setdefault('catalog_sources', {})
        if not isinstance(source_map, dict):
            source_map = metadata['catalog_sources'] = {}
        urls = source_map.setdefault(source_provider, [])
        if source_url and source_url not in urls:
            urls.append(source_url)
        if catalog_id:
            id_map = metadata.setdefault('catalog_ids', {})
            if not isinstance(id_map, dict):
                id_map = metadata['catalog_ids'] = {}
            ids = id_map.setdefault(source_provider, [])
            if catalog_id not in ids:
                ids.append(catalog_id)
        saved_sources = metadata.setdefault('sources', {})
        if isinstance(saved_sources, dict):
            for key, value in (sources or {}).items():
                saved_sources.setdefault(key, value)
        next_author = existing['author']
        if (not next_author or _normalized(next_author) == 'unknown') and author != 'Unknown':
            next_author = author
        release = existing['release_date'] or book.get('release_date')
        release_policy = book.get('release_date_policy') or metadata.get('release_date_policy')
        manual_release = (existing['release_date']
                          if metadata.get('release_date_source') == 'manual' else None)
        if release_policy == 'official_or_manual_only':
            release = manual_release
        next_parent = existing['parent_id'] or parent_id
        if series_name:
            metadata.setdefault('series', series_name)
        reviewed_position = (metadata.get('series_position_source') == 'Media Intake review')
        if (series_index is not None and metadata.get('series_index') is None and
                not reviewed_position):
            metadata['series_index'] = series_index
            metadata['series_position_source'] = series_position_source
        elif (not reviewed_position and series_position_source == 'Wikidata P1545' and
              metadata.get('series_position_source') != 'Wikidata P1545'):
            metadata['series_index'] = series_index
            metadata['series_position_source'] = series_position_source
        for key in ('publication_status', 'search_after_release_date',
                    'release_date_policy', 'continuity_status'):
            if key in book:
                metadata[key] = book[key]
        merge_member_evidence(metadata)
        if catalog.get('evidence'):
            metadata['series_member_evidence'] = catalog['evidence']
        metadata.setdefault('catalog_provider', source_provider)
        if release and not metadata.get('release_date_source'):
            metadata['release_date_source'] = source_provider
        db.execute('''UPDATE watched_items SET author=?,release_date=?,parent_id=?,metadata=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   (next_author or 'Unknown', release, next_parent,
                    json.dumps(metadata, ensure_ascii=False), watch_id))
        db.commit()
        sync_watch_entity(db, watch_id)
        created = False
    else:
        metadata = {'sources': sources or {}, 'catalog_sources': {}, 'catalog_ids': {}}
        record_collection_label(metadata)
        record_profile_membership(metadata)
        if source_url:
            metadata['catalog_sources'][source_provider] = [source_url]
        if catalog_id:
            metadata['catalog_ids'][source_provider] = [catalog_id]
        record_catalog_identity(metadata)
        record_isbn_identities(metadata)
        merge_catalog_provenance(metadata)
        metadata.update({key: value for key, value in catalog.items()
                         if key not in ('catalog_id', 'qid')})
        if series_name:
            metadata['series'] = series_name
        if series_index is not None:
            metadata['series_index'] = series_index
        if series_position_source:
            metadata['series_position_source'] = series_position_source
        for key in ('publication_status', 'search_after_release_date',
                    'release_date_policy', 'continuity_status'):
            if key in book:
                metadata[key] = book[key]
        merge_member_evidence(metadata)
        metadata['series_member_evidence'] = catalog.get('evidence') or []
        metadata.setdefault('catalog_provider', source_provider)
        if book.get('release_date'):
            metadata['release_date_source'] = source_provider
        watch_id = add_watch(db, 'book', title, author, source_url,
                             book.get('release_date'), metadata, parent_id)
        created = True

    current = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
    member_variants = catalog_variant_evidence()
    if current.get('media_entity_id'):
        record_catalog_metadata_variants(
            db, int(current['media_entity_id']), member_variants)
    if current.get('state') != 'downloaded':
        metadata = json.loads(current.get('metadata') or '{}')
        awaiting_date = is_unscheduled_announced_member(metadata) and not current.get('release_date')
        upcoming = (awaiting_date or bool(current.get('release_date') and
                    current['release_date'] > dt.date.today().isoformat()))
        queue_watch_search(db, watch_id, current['title'], current['author'],
                           current.get('release_date'), upcoming=upcoming,
                           series=metadata.get('series'),
                           series_index=metadata.get('series_index'),
                           publication_status=metadata.get('publication_status'))
        if awaiting_date:
            db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (_next_day().isoformat(sep='T'), watch_id))
            db.commit()
    return watch_id, created


def import_goodreads_list(db, books, list_name, sources=None, kindle_target=None,
                          profile_id=None):
    """Add a Goodreads CSV shelf as individual book watches and library tags."""
    label = str(list_name or '').strip()
    if not label:
        raise ValueError('Enter a name for this Goodreads library divider.')
    added = 0
    existing = 0
    scheduled = 0

    def attach_to_library_items(watch_id, watch, label):
        watch_metadata = json.loads(watch.get('metadata') or '{}')
        labels = watch_metadata.get('goodreads_lists')
        if not isinstance(labels, list):
            labels = []
        if label not in labels:
            labels.append(label)
        watch_metadata['goodreads_lists'] = labels
        list_targets = watch_metadata.get('goodreads_list_kindle_targets')
        if not isinstance(list_targets, dict):
            list_targets = {}
        if isinstance(kindle_target, dict) and kindle_target.get('email'):
            list_targets[label] = {
                'name': str(kindle_target.get('name') or '').strip(),
                'email': str(kindle_target.get('email') or '').strip(),
            }
        else:
            list_targets.pop(label, None)
        watch_metadata['goodreads_list_kindle_targets'] = list_targets
        db.execute('UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (json.dumps(watch_metadata, ensure_ascii=False), watch_id))

        for item_row in db.execute('SELECT id,metadata,file_path FROM items').fetchall():
            try:
                item_metadata = json.loads(item_row['metadata'] or '{}')
            except (TypeError, ValueError):
                continue
            linked_watch = item_metadata.get('watch_request_id') or item_metadata.get('watch_id')
            linked = str(linked_watch or '') == str(watch_id)
            if not linked and watch.get('file_path'):
                linked = str(item_row['file_path'] or '') == str(watch['file_path'])
            if not linked:
                continue
            item_labels = item_metadata.get('goodreads_lists')
            if not isinstance(item_labels, list):
                item_labels = []
            if label not in item_labels:
                item_labels.append(label)
                item_metadata['goodreads_lists'] = item_labels
            targets = list_targets.values()
            targets = [dict(value) for value in targets if isinstance(value, dict) and
                       str(value.get('email') or '').strip()]
            if watch_metadata.get('profile_kindle_auto_send'):
                targets.extend(dict(value) for value in
                               watch_metadata.get('profile_kindle_email_targets') or []
                               if isinstance(value, dict) and
                               str(value.get('email') or '').strip())
            unique_targets = []
            seen_targets = set()
            for target in targets:
                email = str(target.get('email') or '').strip()
                if email and email.casefold() not in seen_targets:
                    seen_targets.add(email.casefold())
                    unique_targets.append({'name': str(target.get('name') or '').strip(),
                                           'email': email})
            if unique_targets:
                item_metadata['kindle_email_auto_send'] = True
                item_metadata['kindle_email_targets_override'] = unique_targets
            else:
                item_metadata.pop('kindle_email_auto_send', None)
                item_metadata.pop('kindle_email_targets_override', None)
            profile_ids = watch_metadata.get('library_profile_ids') or []
            if not isinstance(profile_ids, list):
                profile_ids = [profile_ids]
            if profile_ids:
                item_metadata['library_profile_ids'] = list(dict.fromkeys(
                    str(value).strip() for value in profile_ids if str(value).strip()))
            db.execute('UPDATE items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (json.dumps(item_metadata, ensure_ascii=False), item_row['id']))

    for book in books:
        if not isinstance(book, dict) or not str(book.get('title') or '').strip():
            continue
        book = dict(book)
        author = _catalog_author(book.get('author'))
        watch_id, created = _add_catalog_book_watch(
            db, book, author, sources or {}, None, 'Goodreads')
        if not watch_id:
            continue
        if profile_id:
            assign_watch_profile(db, int(watch_id), profile_id)
        watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?',
                                (int(watch_id),)).fetchone())
        attach_to_library_items(int(watch_id), watch, label)
        if created:
            added += 1
        else:
            existing += 1
        if watch.get('state') != 'downloaded':
            scheduled += 1
    db.commit()


def _library_profiles(db):
    try:
        profiles = json.loads(get_setting(db, 'library_profiles', '[]') or '[]')
    except (TypeError, ValueError):
        profiles = []
    return [item for item in profiles if isinstance(item, dict) and item.get('id')]


def _apply_profile_delivery_settings(metadata, profile_ids, profiles):
    by_id = {str(profile.get('id')): profile for profile in profiles}
    assigned = [by_id[profile_id] for profile_id in profile_ids if profile_id in by_id]
    auto_profiles = [profile for profile in assigned if profile.get('auto_email')]
    targets, seen = [], set()
    for profile in auto_profiles:
        raw_targets = profile.get('kindle_targets') or []
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        for value in raw_targets:
            if isinstance(value, str):
                target = {'name': '', 'email': value.strip()}
            elif isinstance(value, dict):
                target = {'name': str(value.get('name') or '').strip(),
                          'email': str(value.get('email') or '').strip()}
            else:
                continue
            key = target['email'].casefold()
            if key and key not in seen:
                seen.add(key)
                targets.append(target)
    if auto_profiles and targets:
        metadata['profile_kindle_auto_send'] = True
        metadata['profile_kindle_email_targets'] = targets
    else:
        metadata.pop('profile_kindle_auto_send', None)
        metadata.pop('profile_kindle_email_targets', None)


def _sync_watch_delivery_to_queue(db, watch_id, watch_metadata):
    """Keep pending downloads aligned with the watch's current profile targets."""
    profile_ids = watch_metadata.get('library_profile_ids') or []
    if not isinstance(profile_ids, list):
        profile_ids = [profile_ids]
    profile_ids = list(dict.fromkeys(str(value).strip() for value in profile_ids
                                     if str(value).strip()))
    targets = []
    if watch_metadata.get('profile_kindle_auto_send'):
        targets.extend(watch_metadata.get('profile_kindle_email_targets') or [])
    list_targets = watch_metadata.get('goodreads_list_kindle_targets') or {}
    if isinstance(list_targets, dict):
        targets.extend(list_targets.values())
    unique_targets, seen = [], set()
    for value in targets:
        if isinstance(value, str):
            target = {'name': '', 'email': value.strip()}
        elif isinstance(value, dict):
            target = {'name': str(value.get('name') or '').strip(),
                      'email': str(value.get('email') or '').strip()}
        else:
            continue
        key = target['email'].casefold()
        if key and key not in seen:
            seen.add(key)
            unique_targets.append(target)

    for row in db.execute('SELECT id,metadata FROM items').fetchall():
        try:
            item_metadata = json.loads(row['metadata'] or '{}')
        except (TypeError, ValueError):
            continue
        linked_id = item_metadata.get('watch_request_id') or item_metadata.get('watch_id')
        if str(linked_id or '') != str(watch_id):
            continue
        if profile_ids:
            item_metadata['library_profile_ids'] = profile_ids
        else:
            item_metadata.pop('library_profile_ids', None)
        if unique_targets:
            item_metadata['kindle_email_auto_send'] = True
            item_metadata['kindle_email_targets_override'] = unique_targets
        else:
            item_metadata.pop('kindle_email_auto_send', None)
            item_metadata.pop('kindle_email_targets_override', None)
        db.execute('UPDATE items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (json.dumps(item_metadata, ensure_ascii=False), row['id']))


def assign_watch_profile(db, watch_id, profile_id):
    """Attach a watch and its existing members to a named library profile."""
    profile_id = str(profile_id or '').strip()
    if not profile_id:
        return
    profiles = _library_profiles(db)
    if not any(str(profile.get('id')) == profile_id for profile in profiles):
        raise ValueError('The selected library profile no longer exists.')
    rows = db.execute('SELECT id,metadata FROM watched_items WHERE id=? OR parent_id=?',
                      (int(watch_id), int(watch_id))).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row['metadata'] or '{}')
        except (TypeError, ValueError):
            metadata = {}
        ids = metadata.get('library_profile_ids') or []
        if not isinstance(ids, list):
            ids = [ids]
        ids = list(dict.fromkeys(str(value) for value in ids if str(value).strip()))
        if profile_id not in ids:
            ids.append(profile_id)
        metadata['library_profile_ids'] = ids
        _apply_profile_delivery_settings(metadata, ids, profiles)
        db.execute('UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (json.dumps(metadata, ensure_ascii=False), row['id']))
        _sync_watch_delivery_to_queue(db, int(row['id']), metadata)
    db.commit()
    for row in rows:
        _copy_watch_to_profile_folders(db, int(row['id']))


def sync_watch_profile_settings(db):
    """Refresh saved Kindle delivery overrides after a profile is edited."""
    profiles = _library_profiles(db)
    rows = db.execute('SELECT id,metadata FROM watched_items').fetchall()
    for row in rows:
        try:
            metadata = json.loads(row['metadata'] or '{}')
        except (TypeError, ValueError):
            continue
        ids = metadata.get('library_profile_ids') or []
        if not isinstance(ids, list):
            ids = [ids]
        _apply_profile_delivery_settings(metadata, ids, profiles)
        db.execute('UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (json.dumps(metadata, ensure_ascii=False), row['id']))
        _sync_watch_delivery_to_queue(db, int(row['id']), metadata)
    db.commit()
    for row in rows:
        _copy_watch_to_profile_folders(db, int(row['id']))


def _profile_library_folders(db, watch, default_folder=None):
    try:
        metadata = json.loads(watch.get('metadata') or '{}')
    except (TypeError, ValueError):
        metadata = {}
    profile_ids = metadata.get('library_profile_ids') or []
    if not isinstance(profile_ids, list):
        profile_ids = [profile_ids]
    by_id = {str(profile.get('id')): profile for profile in _library_profiles(db)}
    folders = []
    for profile_id in profile_ids:
        profile = by_id.get(str(profile_id))
        folder = str((profile or {}).get('library_folder') or '').strip()
        if folder and folder not in folders:
            folders.append(folder)
    if not folders and default_folder:
        folders.append(str(default_folder))
    return folders


def _copy_watch_to_profile_folders(db, watch_id, default_folder=None):
    row = db.execute('SELECT * FROM watched_items WHERE id=?', (int(watch_id),)).fetchone()
    if not row or not row['file_path'] or not Path(row['file_path']).is_file():
        return
    folders = _profile_library_folders(db, dict(row), default_folder)
    source = Path(row['file_path'])
    for folder in folders:
        target = Path(folder) / source.name
        try:
            if source.resolve() != target.resolve() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        except OSError:
            logger.exception('Could not copy watched download %s to profile folder %s',
                             source, folder)
    return


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
    if '_resolved_approvals' in options:
        return options['_resolved_approvals'].get(source_key)
    path = options.get('rights_evidence_path')
    if not path:
        # These newer archive connectors must never become implicitly enabled
        # just because a caller omitted the local file. The GUI supplies this
        # path; callers without it fail closed for these sources.
        if source_key in {
                'internet_archive', 'meawfy.com', 'softarchive.download',
                'animetosho.org', 'tokyoinsider.com', 'animeout.com',
                'noobsubs.com', 'chauthanh.info', 'sourceforge.net',
                'github_foss', 'mediafiretrend.com', 'xdcc.eu',
                'warezomen.com', 'myabandonware.com', 'oldgamesdownload.com',
                'abandonwaregames.net', 'gog-rev.com', 'ankergames.net'}:
            return None
        return {'approved_for_download': True, 'status': 'test_or_library_caller'}
    document = load_rights_evidence(path)
    if watch is not None:
        work_record = effective_work_rights(document, watch, db)
        if work_record:
            return work_record if work_record.get('approved_for_download') is True else None
    return source_download_rights(document, source_key)


_DOWNLOAD_SEARCH_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix='source-search')


def _download_search_plan(watch, options):
    """Share connector applicability between searching and issue watch status."""
    try:
        metadata = json.loads(watch.get('metadata') or '{}')
    except (TypeError, ValueError):
        metadata = {}
    kind = watch.get('kind')
    category = {
        'book': 'Books', 'series': 'Books', 'lightnovel': 'Light novels',
        'comic': 'Comics', 'comic_issue': 'Comics', 'webcomic': 'Comics',
        'manga': 'Manga', 'webtoon': 'Manga', 'anime': 'Anime',
        'video': 'Videos', 'video_episode': 'Videos', 'game': 'Games',
        'software': 'Software', 'webfiction': 'Web serials', 'fanfiction': 'Fanfiction',
    }.get(kind)
    if kind == 'video' and str(metadata.get('video_subtype') or '').startswith('anime'):
        category = 'Anime'
    keys = []
    if kind in {'book', 'series', 'lightnovel'}:
        if options.get('opds_feeds'):
            keys.append('opds_feeds')
        if options.get('catalogs'):
            keys.append('json_catalogs')
        keys.extend(key for flag, key in (('libgen', 'libgen'), ('annas', 'annas_archive'))
                    if options.get(flag, True))
    if has_configured_source(options, category):
        keys.append('irc')
    if options.get('comic_mirror_base') and kind in COMIC_KINDS | {'comic_issue'}:
        keys.append('public_domain_comic_mirror')
    if kind in {'comic', 'comic_issue'}:
        keys.append('getcomics.org')
    if kind in {'book', 'series', 'lightnovel', 'webfiction'}:
        keys.extend(('lnori.com', 'novelarchive.cc'))
    if category and category != 'Software':
        keys.extend(('internet_archive', 'meawfy.com', 'softarchive.download'))
    if category == 'Anime':
        keys.extend(adapter[0] for adapter in ANIME_DOWNLOAD_ADAPTERS)
    if category == 'Software':
        keys.extend(('github_foss', 'sourceforge.net'))
    keys.extend(key for key, _provider, categories in GENERAL_DOWNLOAD_ADAPTERS
                if category in categories)
    return category, list(dict.fromkeys(keys))


def _search_candidates(watch, source_overrides=None, db=None, progress_callback=None):
    """Search applicable approved sources concurrently; rank after every source finishes.

    SQLite/rights decisions and UI callbacks stay on the caller thread. Each worker
    runs one existing connector, so it keeps that connector's parsing and policy.
    The process-wide pool bounds searches across concurrent watched books too.
    """
    try:
        metadata = json.loads(watch.get('metadata') or '{}')
    except (TypeError, ValueError):
        metadata = {}
    options = dict(metadata.get('sources') or {})
    options.update(source_overrides or {})
    # These are internal worker controls, never accepted from persisted settings.
    options.pop('_resolved_approvals', None)
    category, keys = _download_search_plan(watch, options)
    if db is not None and category and not options.get('download_priority'):
        from .discovery import get_download_priority
        options['download_priority'] = get_download_priority(db, category, options)
    approvals = [(key, _approved_source_record(options, key, watch, db))
                 for key in dict.fromkeys(keys)]
    approvals = [(key, approval) for key, approval in approvals if approval]
    messages = queue.Queue()
    def report_messages():
        while True:
            try:
                message = messages.get_nowait()
            except queue.Empty:
                break
            if progress_callback:
                try:
                    progress_callback(message)
                except Exception:
                    logger.debug('Could not report connector progress', exc_info=True)
    futures = []
    for key, approval in approvals:
        worker_options = dict(options, _resolved_approvals={key: approval})
        futures.append(_DOWNLOAD_SEARCH_POOL.submit(
            _search_candidates_sequential, watch, worker_options, None, messages.put))
    pending = set(futures)
    while pending:
        _done, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
        report_messages()
    report_messages()
    results, errors, seen = [], [], set()
    from . import source_health
    for (key, _approval), future in zip(approvals, futures):
        try:
            items, source_errors = future.result()
            errors.extend(source_errors)
        except Exception as exc:
            errors.append(f'{key}: {exc}')
            if db is not None:
                source_health.record(db, key, 'search', False, detail=exc)
            continue
        if db is not None:
            # A search that found nothing is not a failure; one that only errored is.
            failed = bool(source_errors) and not items
            source_health.record(db, key, 'search', not failed,
                                 detail=source_errors[0] if failed else f'{len(items)} result(s)')
        for item in items:
            url_key = item.get('url') or json.dumps(item.get('irc') or {}, sort_keys=True)
            identity = (url_key, str(item.get('format') or '').casefold())
            if identity not in seen:
                seen.add(identity)
                results.append(item)
    from .discovery import _download_sort_key
    priority = options.get('download_priority') or []
    requested_title = _normalized(watch.get('title'))
    failing = set()
    if db is not None:
        from .rights_evidence import source_key_for_item
        failing = {row['source_key'] for row in source_health.summaries(db) if row['status'] == 'failing'}
    def rank(item):
        candidate_title = _normalized(item.get('title'))
        exact = candidate_title == requested_title
        similarity = difflib.SequenceMatcher(None, requested_title, candidate_title).ratio()
        # Exact work identity wins over format/source preferences. Between equally
        # good matches, a source that keeps failing is tried last. Failed top
        # candidates still fall through the established watch download loop.
        unhealthy = bool(failing) and source_key_for_item(item) in failing
        return (0 if exact else 1, -round(similarity, 3), unhealthy,
                _download_sort_key(item, category, priority),
                str(item.get('url') or ''))
    results.sort(key=rank)
    if results:
        errors = [error for error in errors if 'app search is unavailable' not in error]
    return results, errors


def _search_candidates_sequential(watch, source_overrides=None, db=None,
                       progress_callback=None):
    query = ' '.join(part for part in (watch['title'], watch.get('author')) if part and part != 'Unknown')
    try:
        metadata = json.loads(watch.get('metadata') or '{}')
    except (TypeError, ValueError):
        metadata = {}
    options = dict(metadata.get('sources') or {})
    options.update(source_overrides or {})
    results, errors, browser_search_notes = [], [], []

    def report_source(name):
        if progress_callback is None:
            return
        try:
            progress_callback(str(name))
        except Exception:
            logger.debug('Could not update watch search progress for %s', name,
                         exc_info=True)

    def add_results(items, source_key, approval=None):
        for item in items or []:
            if not isinstance(item, dict):
                continue
            item.setdefault('source_key', source_key)
            if options.get('rights_evidence_path'):
                item.setdefault('rights_evidence_path', options['rights_evidence_path'])
                if item.get('irc') is not None:
                    item['irc'].setdefault('source_key', source_key)
                    item['irc'].setdefault('rights_evidence_path', options['rights_evidence_path'])
            if approval and approval.get('rights_work_key'):
                item.setdefault('rights_work_key', approval['rights_work_key'])
            results.append(item)

    book_kind = watch.get('kind') in {'book', 'series', 'lightnovel'}
    for feed_url in (options.get('opds_feeds', []) if book_kind and _approved_source_record(
            options, 'opds_feeds') else []):
        try:
            report_source(f'OPDS catalog {feed_url}')
            add_results(search_opds(feed_url, query), 'opds_feeds')
        except Exception as exc:
            errors.append(f'OPDS {feed_url}: {exc}')
    for catalog_path in (options.get('catalogs', []) if book_kind and _approved_source_record(
            options, 'json_catalogs') else []):
        try:
            report_source(f'JSON catalog {catalog_path}')
            add_results(search_catalog(catalog_path, query), 'json_catalogs')
        except Exception as exc:
            errors.append(f'Catalog {catalog_path}: {exc}')
    for site in (('libgen', 'annas') if book_kind else ()):
        source_key = 'annas_archive' if site == 'annas' else 'libgen'
        if (not options.get(site, True) or not _approved_source_record(options, source_key)):
            continue
        try:
            report_source("Anna's Archive" if site == 'annas' else 'LibGen')
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
    irc_category = {
        'book': 'Books', 'series': 'Books', 'lightnovel': 'Light novels',
        'comic': 'Comics', 'comic_issue': 'Comics', 'webcomic': 'Comics',
        'manga': 'Manga', 'webtoon': 'Manga', 'anime': 'Anime',
        'video': 'Videos', 'video_episode': 'Videos', 'game': 'Games',
        'software': 'Software', 'webfiction': 'Web serials',
        'fanfiction': 'Fanfiction',
    }.get(watch.get('kind'))
    if (_approved_source_record(options, 'irc')
            and has_configured_source(options, irc_category)):
        allowed_formats = _IRC_FORMATS_BY_KIND.get(watch.get('kind'), EXTENSIONS)
        irc_priority = options.get('download_priority')
        if not isinstance(irc_priority, (list, tuple)):
            irc_priority = (options.get('download_priorities') or {}).get(irc_category)
        if not irc_priority and db is not None and irc_category:
            from .discovery import get_download_priority
            irc_priority = get_download_priority(db, irc_category, options)
        irc_items, irc_errors = search_configured_sources(
            query, options, irc_category, allowed_formats, limit=100,
            priority=irc_priority, progress_callback=report_source)
        errors.extend(f'IRC: {error}' for error in irc_errors)
        add_results(irc_items, 'irc')
    if options.get('comic_mirror_base') and watch.get('kind') in COMIC_KINDS | {'comic_issue'}:
        try:
            rights = _approved_source_record(options, 'public_domain_comic_mirror', watch)
            if rights:
                report_source(f"Public-domain comic mirror {options['comic_mirror_base']}")
                add_results(search_public_domain_comic_mirror(
                    query, options['comic_mirror_base'], limit=20, manual_rights=rights),
                    'public_domain_comic_mirror')
        except Exception as exc:
            errors.append(f'Comic mirror / rights evidence: {exc}')
    if watch.get('kind') in {'comic', 'comic_issue'}:
        approval = _approved_source_record(options, 'getcomics.org', watch)
        if approval:
            try:
                report_source('GetComics')
                # Release listings usually do not carry creator credits. Adding
                # the creator to WordPress search can hide an otherwise exact issue.
                posts = search_getcomics(watch['title'], limit=20)
                for post in posts:
                    try:
                        add_results(resolve_getcomics_post(post, approval), 'getcomics.org', approval)
                        errors.extend(f'GetComics: {warning}' for warning in post.get('connector_warnings', []))
                    except Exception as exc:
                        errors.append(f"GetComics post {post.get('url')}: {exc}")
            except Exception as exc:
                errors.append(f'GetComics / rights evidence: {exc}')
    if watch.get('kind') in {'book', 'series', 'lightnovel'}:
        for source_key, searcher in (('lnori.com', search_lnori),
                                     ('novelarchive.cc', search_novelarchive)):
            approval = _approved_source_record(options, source_key)
            if not approval:
                continue
            try:
                report_source('LNORI' if source_key == 'lnori.com' else 'NovelArchive')
                for item in searcher(query, limit=10):
                    add_results(resolve_catalog_file_links(item, approval), source_key, approval)
            except Exception as exc:
                errors.append(f'{source_key} / rights evidence: {exc}')
    if watch.get('kind') == 'webfiction':
        for source_key, searcher in (('lnori.com', search_lnori),
                                     ('novelarchive.cc', search_novelarchive)):
            approval = _approved_source_record(options, source_key)
            if not approval:
                continue
            try:
                report_source('LNORI' if source_key == 'lnori.com' else 'NovelArchive')
                for item in searcher(query, limit=10):
                    add_results(resolve_catalog_file_links(item, approval), source_key, approval)
            except Exception as exc:
                errors.append(f'{source_key} / rights evidence: {exc}')
    archive_category = {
        'book': 'Books', 'series': 'Books', 'lightnovel': 'Light novels',
        'webfiction': 'Web serials', 'fanfiction': 'Fanfiction',
        'comic': 'Comics', 'comic_issue': 'Comics', 'webcomic': 'Comics',
        'manga': 'Manga', 'webtoon': 'Manga', 'anime': 'Anime',
        'video': 'Videos', 'video_episode': 'Videos', 'game': 'Games',
        'software': 'Software',
    }.get(watch.get('kind'))
    watch_metadata = metadata
    if watch.get('kind') == 'video':
        subtype = str(watch_metadata.get('video_subtype') or '').casefold()
        if subtype.startswith('anime'):
            archive_category = 'Anime'
    if archive_category and archive_category != 'Software':
        approval = _approved_source_record(options, 'internet_archive', watch, db)
        if approval:
            try:
                report_source('Internet Archive')
                add_results(search_internet_archive(
                    query, category=archive_category, limit=20),
                    'internet_archive', approval)
            except Exception as exc:
                errors.append(f'Internet Archive / rights evidence: {exc}')

    if archive_category and archive_category != 'Software':
        for source_key, provider, searcher, resolver in (
                ('meawfy.com', 'Meawfy', search_meawfy, resolve_meawfy_downloads),
                ('softarchive.download', 'SoftArchive', search_softarchive,
                 resolve_softarchive_downloads)):
            approval = _approved_source_record(options, source_key, watch, db)
            if not approval:
                continue
            try:
                report_source(provider)
                for hit in searcher(query):
                    add_results(resolver(hit, allowed_formats=formats_for_category(archive_category)),
                                source_key, approval)
            except Exception as exc:
                errors.append(f'{provider} / rights evidence: {exc}')
    if archive_category == 'Anime':
        for source_key, provider, searcher, resolver in ANIME_DOWNLOAD_ADAPTERS:
            approval = _approved_source_record(options, source_key, watch, db)
            if not approval:
                continue
            try:
                report_source(provider)
                for hit in searcher(query):
                    add_results(resolver(
                        hit, allowed_formats=formats_for_category('Anime')),
                        source_key, approval)
            except Exception as exc:
                errors.append(f'{provider} / rights evidence: {exc}')
    if archive_category == 'Software':
        for source_key, searcher in (
                ('github_foss', search_github_foss),
                ('sourceforge.net', search_sourceforge_foss)):
            approval = _approved_source_record(options, source_key, watch, db)
            if not approval:
                continue
            try:
                report_source('GitHub (FOSS)' if source_key == 'github_foss'
                              else 'SourceForge')
                for item in searcher(query):
                    add_results([item], source_key, approval)
            except Exception as exc:
                errors.append(f'{source_key} / rights evidence: {exc}')
    if archive_category:
        for source_key, provider, categories in GENERAL_DOWNLOAD_ADAPTERS:
            if archive_category not in categories:
                continue
            approval = _approved_source_record(options, source_key, watch, db)
            if not approval:
                continue
            try:
                report_source(provider)
                hits = search_registered_download_site(
                    query, source_key, limit=30, category=archive_category,
                    allowed_formats=formats_for_category(archive_category))
                for hit in hits:
                    if hit.get('discovery_only') and not hit.get('detail_url'):
                        browser_search_notes.append(
                            f'{provider}: app search is unavailable; open its browser search page manually.')
                        continue
                    items = ([hit] if source_key == 'xdcc.eu' else
                             resolve_site_downloads(
                                 hit, source_key,
                                 allowed_formats=formats_for_category(archive_category)))
                    add_results(items, source_key, approval)
            except Exception as exc:
                errors.append(f'{provider} / rights evidence: {exc}')
    matched = [item for item in results if _is_match(item, watch)]
    # Honor the configured source priority by giving IRC first choice while
    # preserving the established ebook format hierarchy (EPUB before PDF, etc.).
    original_order = {id(item): index for index, item in enumerate(matched)}
    format_order = {'.epub': 0, '.pdf': 1, '.azw3': 2, '.mobi': 3, '.txt': 4,
                    '.m4b': 5, '.mp3': 6}
    matched.sort(key=lambda item: (
        format_order.get(str(item.get('format') or '').casefold(), 99)
        if book_kind else 0,
        0 if item.get('irc') or item.get('source_key') == 'irc' else 1,
        original_order[id(item)]))
    if not matched:
        errors.extend(browser_search_notes)
    return matched, errors


def _process_software_watch(db, watch, library_folder, source_overrides=None):
    """Check approved FOSS repositories for a new release and save one package."""
    watch = dict(watch)
    metadata = json.loads(watch.get('metadata') or '{}')
    options = dict(metadata.get('sources') or {})
    options.update(source_overrides or {})
    now = _now()
    keys = ('github_foss', 'sourceforge.net')
    if not any(_approved_source_record(options, key, watch, db) for key in keys):
        message = 'Both FOSS download sources are NO in the local rights-evidence.json file.'
        update_watch_search(db, watch['id'], 'rights_hold', message)
        db.execute("""UPDATE watched_items SET state='rights_hold',last_checked_at=?,
            next_check_at=NULL,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                   (now.isoformat(sep='T'), message, watch['id']))
        db.commit()
        return False

    queue_watch_search(db, watch['id'], watch['title'], watch.get('author'), upcoming=False)
    candidates, errors = _search_candidates(
        watch, source_overrides, db,
        progress_callback=lambda name: update_watch_search(
            db, watch['id'], 'searching', progress_message=f'Searching {name}…'))
    software = dict(metadata.get('software') or {})
    known_urls = set(software.get('known_download_urls') or [])
    files = [item for item in candidates
             if item.get('source_key') in keys and
             str(item.get('format') or '').casefold() in SOFTWARE_FORMATS and
             item.get('url') not in known_urls]
    for item in files:
        try:
            candidate = dict(item)
            row = enqueue_for_watch(db, candidate, watch['id'])
            saved = download(db, row['id'], Path(library_folder).parent,
                             destination=library_folder)
            known_urls.add(str(candidate.get('url') or ''))
            software['known_download_urls'] = sorted(known_urls)[-5000:]
            software['last_downloaded_project'] = str(candidate.get('software_project') or '')
            software['last_downloaded_version'] = str(candidate.get('software_version') or '')
            metadata['software'] = software
            db.execute("""UPDATE watched_items SET metadata=?,state='watching',file_path=?,
                last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
                       (json.dumps(metadata, ensure_ascii=False), saved.get('file_path'),
                        now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
            db.commit()
            return True
        except Exception as exc:
            logger.exception('FOSS software download candidate failed for watch %s: %s',
                             watch['id'], item.get('title') or item.get('url'))
            errors.append(str(exc))
    message = '; '.join(errors[-3:]) if errors else 'No new matching FOSS release package was found.'
    update_watch_search(db, watch['id'], 'searching', message,
                        progress_message='Search complete; next check scheduled')
    db.execute("""UPDATE watched_items SET state='watching',last_checked_at=?,
        next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
               (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), message, watch['id']))
    db.commit()
    return False


def _search_book(db, watch, library_folder, source_overrides=None):
    watch_id = int(watch['id'])
    metadata = json.loads(watch.get('metadata') or '{}')
    graph_entity = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                              (watch_id,)).fetchone()
    graph_entity_id = (int(graph_entity['media_entity_id']) if graph_entity and
                       graph_entity['media_entity_id'] else None)
    if graph_entity_id:
        wikidata_ids = [row['external_id'] for row in db.execute('''SELECT external_id FROM media_external_ids
            WHERE entity_id=? AND provider='wikidata' ''', (graph_entity_id,))]
        for qid in wikidata_ids:
            try:
                imported = import_wikidata_entity(db, qid,
                                                  watch_entity_id=graph_entity_id)
                if int(imported.get('root_entity_id') or 0) == graph_entity_id:
                    _apply_wikidata_watch_policy(db, watch, imported)
                    capture_graph_snapshot(db, graph_entity_id)
                    db.commit()
            except Exception as exc:
                logger.info('Wikidata graph refresh failed for watched item %s: %s', watch_id, exc)
    series_name = str(metadata.get('series') or '').strip()
    if not series_name and watch.get('parent_id'):
        parent = db.execute('SELECT title FROM watched_items WHERE id=?',
                            (watch['parent_id'],)).fetchone()
        series_name = str(parent['title'] or '').strip() if parent else ''
    series_index = metadata.get('series_index')
    if is_unscheduled_announced_member(metadata) and not watch.get('release_date'):
        queue_watch_search(db, watch_id, watch['title'], watch.get('author'),
                           upcoming=True, series=series_name,
                           series_index=series_index,
                           publication_status=metadata.get('publication_status'))
        db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (_next_day().isoformat(sep='T'), watch_id))
        db.commit()
        return False
    if watch.get('release_date') and watch['release_date'] > dt.date.today().isoformat():
        queue_watch_search(db, watch_id, watch['title'], watch.get('author'),
                           watch.get('release_date'), upcoming=True,
                           series=series_name, series_index=series_index)
        db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (f"{watch['release_date']}T00:00:00", watch_id))
        db.commit()
        return False
    queue_watch_search(db, watch_id, watch['title'], watch.get('author'),
                       watch.get('release_date'), upcoming=False,
                       series=series_name, series_index=series_index)
    results, errors = _search_candidates(
        watch, source_overrides, db,
        progress_callback=lambda name: update_watch_search(
            db, watch_id, 'searching', progress_message=f'Searching {name}…'))
    for error in errors:
        logger.warning('Book watch %s connector error: %s', watch['id'], error)
    for item in results:
        try:
            candidate = dict(item)
            if series_name:
                candidate.setdefault('series', series_name)
            if series_index is not None:
                candidate.setdefault('series_index', series_index)
            goodreads_lists = metadata.get('goodreads_lists')
            if isinstance(goodreads_lists, list) and goodreads_lists:
                candidate['goodreads_lists'] = list(dict.fromkeys(
                    str(value).strip() for value in goodreads_lists if str(value).strip()))
            book_lists = metadata.get('book_lists')
            if isinstance(book_lists, list) and book_lists:
                candidate['book_lists'] = list(dict.fromkeys(
                    str(value).strip() for value in book_lists if str(value).strip()))
            profile_ids = metadata.get('library_profile_ids') or []
            if not isinstance(profile_ids, list):
                profile_ids = [profile_ids]
            if profile_ids:
                candidate['library_profile_ids'] = list(dict.fromkeys(
                    str(value).strip() for value in profile_ids if str(value).strip()))
            target_values = [value for value in (candidate.get('kindle_email_targets_override') or [])
                             if isinstance(value, dict)]
            profile_targets = metadata.get('profile_kindle_email_targets') or []
            if metadata.get('profile_kindle_auto_send'):
                candidate['kindle_email_auto_send'] = True
                if isinstance(profile_targets, list):
                    target_values.extend(value for value in profile_targets
                                         if isinstance(value, dict))
            collection_targets = metadata.get('goodreads_list_kindle_targets')
            if isinstance(collection_targets, dict):
                for value in collection_targets.values():
                    if (isinstance(value, dict) and str(value.get('email') or '').strip() and
                            not any(str(existing.get('email') or '').casefold() ==
                                    str(value.get('email')).casefold()
                                    for existing in target_values)):
                        target_values.append({'name': str(value.get('name') or '').strip(),
                                              'email': str(value.get('email')).strip()})
                if collection_targets:
                    candidate['kindle_email_auto_send'] = True
            if target_values:
                unique_targets = []
                seen_target_emails = set()
                for target in target_values:
                    email = str(target.get('email') or '').strip()
                    if email and email.casefold() not in seen_target_emails:
                        seen_target_emails.add(email.casefold())
                        unique_targets.append({'name': str(target.get('name') or '').strip(),
                                               'email': email})
                candidate['kindle_email_targets_override'] = unique_targets
            if candidate.get('irc'):
                irc_config = dict(candidate['irc'])
                if series_name:
                    irc_config.setdefault('series', series_name)
                if series_index is not None:
                    irc_config.setdefault('series_index', series_index)
                for key in ('kindle_email_auto_send', 'kindle_email_targets_override',
                            'library_profile_ids', 'book_lists', 'goodreads_lists'):
                    if candidate.get(key):
                        irc_config[key] = candidate[key]
                row = enqueue_irc(db, irc_config, watch_id=watch_id)
            else:
                row = enqueue_for_watch(db, candidate, watch_id)
            if ((isinstance(goodreads_lists, list) and goodreads_lists) or
                    (isinstance(book_lists, list) and book_lists) or profile_ids or
                    candidate.get('kindle_email_auto_send')):
                try:
                    queued_metadata = json.loads(row.get('metadata') or '{}')
                except (TypeError, ValueError):
                    queued_metadata = {}
                queued_labels = queued_metadata.get('goodreads_lists')
                if not isinstance(queued_labels, list):
                    queued_labels = [queued_labels] if queued_labels else []
                queued_metadata['goodreads_lists'] = list(dict.fromkeys(
                    [*queued_labels, *candidate.get('goodreads_lists', [])]))
                queued_book_lists = queued_metadata.get('book_lists')
                if not isinstance(queued_book_lists, list):
                    queued_book_lists = [queued_book_lists] if queued_book_lists else []
                queued_metadata['book_lists'] = list(dict.fromkeys(
                    [*queued_book_lists, *candidate.get('book_lists', [])]))
                queued_profile_ids = queued_metadata.get('library_profile_ids')
                if not isinstance(queued_profile_ids, list):
                    queued_profile_ids = [queued_profile_ids] if queued_profile_ids else []
                queued_metadata['library_profile_ids'] = list(dict.fromkeys(
                    [*queued_profile_ids, *candidate.get('library_profile_ids', [])]))
                if candidate.get('kindle_email_auto_send'):
                    queued_metadata['kindle_email_auto_send'] = True
                    queued_targets = queued_metadata.get('kindle_email_targets_override')
                    if not isinstance(queued_targets, list):
                        queued_targets = []
                    for target in candidate.get('kindle_email_targets_override', []):
                        if not any(str(existing.get('email') or '').casefold() ==
                                   str(target.get('email') or '').casefold()
                                   for existing in queued_targets if isinstance(existing, dict)):
                            queued_targets.append(target)
                    queued_metadata['kindle_email_targets_override'] = queued_targets
                db.execute('UPDATE items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                           (json.dumps(queued_metadata, ensure_ascii=False), row['id']))
                db.commit()
                row['metadata'] = json.dumps(queued_metadata, ensure_ascii=False)
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
    update_watch_search(db, watch_id, 'searching', reason,
                        progress_message='Search complete; next check scheduled')
    db.execute("UPDATE watched_items SET state='watching',last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (reason, watch_id))
    db.commit()
    return False


def _search_book_with_own_connection(database_path, watch, library_folder,
                                     source_overrides=None):
    """Run one watched book on a worker-owned SQLite connection."""
    worker_db = connect(Path(database_path).parent)
    try:
        fresh = worker_db.execute('SELECT * FROM watched_items WHERE id=?',
                                  (int(watch['id']),)).fetchone()
        watch = dict(fresh) if fresh else watch
        folders = _profile_library_folders(worker_db, watch, library_folder)
        target_folder = folders[0] if folders else library_folder
        result = _search_book(worker_db, watch, target_folder, source_overrides)
        _copy_watch_to_profile_folders(worker_db, int(watch['id']), library_folder)
        return result
    except Exception as exc:
        logger.exception('Parallel book watch %s (%s) failed',
                         watch.get('id'), watch.get('title'))
        worker_db.execute("""UPDATE watched_items SET state='watching',
            last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=?""",
                          (_now().isoformat(sep='T'), _next_day().isoformat(sep='T'),
                           str(exc), int(watch['id'])))
        worker_db.commit()
        return False
    finally:
        worker_db.close()


def _normalized_provider_records(db, entity_id):
    return [(str(row['provider']).casefold(), str(row['external_id']))
            for row in db.execute('''SELECT provider,external_id FROM media_external_ids
                WHERE entity_id=? ORDER BY provider,external_id''', (int(entity_id),))]


_MAX_WIKIDATA_SERIES_EXPANSION_DEPTH = 3


def _apply_wikidata_watch_policy(db, watch, imported, continuity_path=None):
    """Materialize only policy-approved, explicit continuation relationships."""
    watch = dict(watch)
    metadata = json.loads(watch.get('metadata') or '{}')
    entity_id = int(watch.get('media_entity_id') or sync_watch_entity(db, int(watch['id'])))
    media_type = {
        'book': 'book', 'lightnovel': 'book', 'series': 'book',
        'comic': 'comic', 'comic_issue': 'comic', 'webcomic': 'comic',
        'manga': 'manga', 'webtoon': 'manga', 'video': 'video',
        'video_episode': 'video', 'game': 'game', 'webfiction': 'webfiction',
        'fanfiction': 'fanfiction',
    }.get(watch.get('kind'), 'unknown')
    scopes = [('work', str(entity_id))]
    entity_type = str(metadata.get('entity_type') or '').casefold()
    if entity_type in {'series', 'franchise', 'universe', 'continuity'}:
        scopes.insert(0, (entity_type, str(entity_id)))
    policy = effective_watch_policy(db, media_type, scopes)
    continuity_path = list(continuity_path or [])
    if watch.get('kind') == 'series' and not continuity_path:
        current_qid = str(imported.get('qid') or '').strip()
        if current_qid:
            continuity_path.append(current_qid)
    added = 0
    for relationship in imported.get('relationships') or []:
        relation_type = str(relationship.get('relationship_type') or '').casefold()
        target_type = str(relationship.get('target_type') or 'work').casefold()
        target_media = str(relationship.get('target_media_type') or media_type).casefold()
        if (relation_type == 'parent_series' and target_type == 'series' and
                target_media == 'book' and watch.get('kind') == 'book' and
                not watch.get('parent_id')):
            action = relationship_watch_action(
                policy, 'series_members', relationship.get('confidence', 0.9))
            qid = str(relationship.get('target_qid') or '').strip()
            title = str(relationship.get('target_title') or '').strip()
            source_url = str(relationship.get('target_source_url') or
                             (f'https://www.wikidata.org/entity/{qid}' if qid else '')).strip()
            if action != 'auto_add' or not qid or not title:
                continue
            series_metadata = {
                'sources': metadata.get('sources') or {},
                'entity_type': 'series', 'catalog_provider': 'Wikidata',
                'catalog_metadata': {'catalog_id': qid, 'wikidata_qid': qid},
                'external_ids': [{'provider': 'wikidata', 'external_id': qid,
                                  'source_url': source_url}],
            }
            series_watch_id = add_watch(
                db, 'series', title, watch.get('author') or 'Unknown',
                source_url, None, series_metadata)
            if int(series_watch_id) != int(watch['id']) and watch.get('kind') == 'book':
                db.execute('''UPDATE watched_items SET parent_id=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND parent_id IS NULL''', (series_watch_id, watch['id']))
                db.commit()
                sync_watch_entity(db, int(watch['id']))
            series_watch = dict(db.execute(
                'SELECT * FROM watched_items WHERE id=?', (series_watch_id,)).fetchone())
            if (qid not in continuity_path and
                    len(continuity_path) < _MAX_WIKIDATA_SERIES_EXPANSION_DEPTH):
                try:
                    _process_wikidata_series_collection(
                        db, series_watch, qid_override=qid,
                        continuity_path=continuity_path + [qid])
                except Exception as exc:
                    queue_graph_review(
                        db, 'source_unavailable',
                        f'Could not load members of {title}',
                        {'provider': 'Wikidata', 'qid': qid, 'error': str(exc)},
                        entity_id=int(series_watch.get('media_entity_id') or entity_id),
                        dedupe_key=f'wikidata-series-members:{qid}')
                    logger.info('Wikidata series member refresh failed for %s: %s', title, exc)
            added += int(series_watch_id != int(watch['id']))
            continue
        if relation_type in {'followed_by', 'follows', 'sequel_to', 'prequel_to'}:
            policy_key = continuation_watch_policy_key(
                relation_type, target_type, relationship.get('target_release_date'))
        elif relation_type == 'derivative_work':
            policy_key = 'derivative_work'
        elif relation_type in {'spin_off_of', 'character_spin_off'}:
            policy_key = relation_type
        else:
            continue
        action = relationship_watch_action(policy, policy_key,
                                           relationship.get('confidence', 0.9))
        if action != 'auto_add' or target_type in {'person', 'creator', 'edition', 'collection'}:
            continue
        qid = str(relationship.get('target_qid') or '').strip()
        title = str(relationship.get('target_title') or '').strip()
        source_url = str(relationship.get('target_source_url') or
                         (f'https://www.wikidata.org/entity/{qid}' if qid else '')).strip()
        if not title or not qid or target_media not in {'book', 'comic', 'manga', 'video', 'game'}:
            continue
        kind = ('series' if target_type in {'series', 'franchise'} and target_media == 'book'
                else 'book' if target_media == 'book' else
                'comic' if target_media == 'comic' else
                'manga' if target_media == 'manga' else
                'video' if target_media == 'video' else 'game')
        child_metadata = {
            'sources': metadata.get('sources') or {},
            'entity_type': target_type,
            'catalog_provider': 'Wikidata',
            'catalog_metadata': {'catalog_id': qid, 'wikidata_qid': qid},
            'external_ids': [{'provider': 'wikidata', 'external_id': qid,
                              'source_url': source_url}],
            'continuation_relationship': relation_type,
        }
        if target_media == 'video' and relationship.get('target_video_subtype'):
            child_metadata['video_subtype'] = relationship['target_video_subtype']
        added_id = add_watch(db, kind, title, 'Unknown', source_url,
                             relationship.get('target_release_date'), child_metadata)
        if int(added_id) != int(watch['id']):
            added += 1
        if (kind == 'series' and qid not in continuity_path and
                len(continuity_path) < _MAX_WIKIDATA_SERIES_EXPANSION_DEPTH):
            series_watch_row = db.execute(
                'SELECT * FROM watched_items WHERE id=?', (added_id,)).fetchone()
            if series_watch_row:
                try:
                    _process_wikidata_series_collection(
                        db, dict(series_watch_row), qid_override=qid,
                        continuity_path=continuity_path + [qid])
                except Exception as exc:
                    queue_graph_review(
                        db, 'source_unavailable',
                        f'Could not load members of {title}',
                        {'provider': 'Wikidata', 'qid': qid, 'error': str(exc)},
                        entity_id=int(series_watch_row['media_entity_id'] or entity_id),
                        dedupe_key=f'wikidata-series-members:{qid}')
                    logger.info('Wikidata sequel series member refresh failed for %s: %s',
                                title, exc)
    return added


def _normalized_release_status(value):
    text = str(value or '').strip()
    folded = text.casefold().replace('_', ' ')
    if any(term in folded for term in ('upcoming', 'not yet released', 'announced', 'planned')):
        return 'upcoming'
    if folded in {'canceled', 'cancelled'}:
        return 'cancelled'
    if folded in {'ended', 'complete', 'completed', 'finished'}:
        return 'completed'
    if folded in {'released', 'available'}:
        return 'released'
    if folded in {'releasing', 'running', 'ongoing', 'returning series'}:
        return 'ongoing'
    if folded in {'hiatus', 'on hiatus'}:
        return 'hiatus'
    if folded == 'paused':
        return 'paused'
    if folded in {'in production', 'post production', 'in development'}:
        return 'in_production'
    if folded in {'to be determined', 'tbd', 'unscheduled'}:
        return 'unscheduled'
    return text or None


def _save_igdb_platform_releases(db, entity_id, releases, source_url=None):
    """Retain IGDB's separate regional and platform release-date observations."""
    saved = 0
    for item in (releases or [])[:1000]:
        if not isinstance(item, dict):
            continue
        raw_date = item.get('date')
        release_date = _date(raw_date)
        if release_date is None and isinstance(raw_date, (int, float)):
            try:
                release_date = dt.datetime.fromtimestamp(
                    int(raw_date), dt.timezone.utc).date().isoformat()
            except (ValueError, OSError, OverflowError):
                release_date = None
        platform = _plain(item.get('platform')) or None
        region = _plain(item.get('release_region')) or None
        status = _plain(item.get('status')) or None
        if not any((release_date, platform, region, status)):
            continue
        release_id = str(item.get('id') or '').strip()
        if not release_id:
            stable = json.dumps([platform, region], ensure_ascii=False)
            release_id = hashlib.sha256(stable.encode('utf-8')).hexdigest()
        upsert_media_release(
            db, entity_id, 'platform_launch', release_date, region=region,
            platform=platform, status=status, provider='IGDB',
            provider_release_id=release_id, source_url=source_url,
            metadata={'igdb_release_date_id': str(item.get('id') or '') or None,
                      'release_status': status})
        saved += 1
    return saved


def _save_tmdb_movie_release_windows(db, entity_id, regions, source_url=None):
    """Store TMDB release windows as separate country and release-type records."""
    release_types = {
        1: 'premiere', 2: 'limited_theatrical', 3: 'theatrical',
        4: 'digital', 5: 'physical', 6: 'television',
    }
    saved = 0
    for region_record in (regions or [])[:300]:
        if not isinstance(region_record, dict):
            continue
        region = str(region_record.get('iso_3166_1') or '').strip().upper() or None
        country = str((region_record.get('country') or '')).strip()
        counters = {}
        for item in (region_record.get('release_dates') or [])[:300]:
            if not isinstance(item, dict):
                continue
            try:
                type_id = int(item.get('type'))
            except (TypeError, ValueError):
                type_id = 0
            release_type = release_types.get(type_id, f'tmdb_type_{type_id or "unknown"}')
            index = counters.get(release_type, 0)
            counters[release_type] = index + 1
            release_date = _date(item.get('release_date'))
            release_status = None
            if release_date:
                release_status = ('upcoming' if release_date > dt.date.today().isoformat()
                                  else 'released')
            upsert_media_release(
                db, entity_id, f'video_{release_type}', release_date,
                region=region, status=release_status, provider='TMDB',
                provider_release_id=f'{region or "unknown"}:{release_type}:{index}',
                source_url=source_url,
                metadata={'country': country, 'tmdb_release_type': type_id,
                          'certification': item.get('certification'),
                          'note': item.get('note')})
            saved += 1
    return saved


def _anilist_date(value):
    if not isinstance(value, dict) or not all(value.get(key) for key in ('year', 'month', 'day')):
        return None
    try:
        return dt.date(int(value['year']), int(value['month']), int(value['day'])).isoformat()
    except (TypeError, ValueError):
        return None


def _refresh_announcement_feeds(db, watch, entity_id, metadata):
    """Baseline configured announcement feeds and review typed schedule candidates."""
    urls = list(dict.fromkeys(str(value).strip() for value in
                             (metadata.get('announcement_feed_urls') or [])
                             if str(value or '').strip()))
    if not urls:
        return []
    states = metadata.get('announcement_feed_state')
    if not isinstance(states, dict):
        states = {}
    now_text = _now().isoformat(sep='T')
    today = dt.date.today().isoformat()
    errors = []
    for feed_url in urls:
        try:
            manifest = discover_feed(feed_url, 'webfiction', limit=500)
        except Exception as exc:
            errors.append(f'{feed_url}: {exc}')
            continue
        state = states.get(feed_url)
        if not isinstance(state, dict):
            state = {}
        first_scan = not state.get('baselined_at')
        if first_scan:
            state['baselined_at'] = now_text
        known = set(str(value) for value in state.get('known_entries') or [])
        for entry in manifest.get('chapters') or []:
            entry_url = str(entry.get('url') or '').strip()
            if not entry_url:
                continue
            provider_metadata = entry.get('provider_metadata')
            if not isinstance(provider_metadata, dict):
                provider_metadata = {}
            legacy_entry_key = hashlib.sha256(
                (feed_url + '\n' + entry_url).encode('utf-8')).hexdigest()
            entry_key = _video_feed_entry_identity(
                feed_url, provider_metadata, entry_url)[0]
            is_new = entry_key not in known and legacy_entry_key not in known
            if is_new:
                known.add(legacy_entry_key)
            known.add(entry_key)
            title = str(entry.get('title') or 'Release announcement').strip()
            published = entry.get('published')
            typed_event = _classify_release_announcement(entry)
            summary = _announcement_summary(entry)
            schedule_date = _exact_date(provider_metadata.get('schedule_date'))
            future_schedule = bool(schedule_date and schedule_date > today)
            upsert_media_release(
                db, entity_id, 'announcement', None, status='announced',
                provider='announcement_feed', provider_release_id=entry_key,
                source_url=entry_url,
                metadata={'title': title, 'feed_url': feed_url,
                          'feed_title': manifest.get('title'),
                          'summary': summary,
                          'schedule_date': schedule_date,
                          'provider_metadata': provider_metadata,
                          'announcement_date': published,
                          'baseline': bool(first_scan)})
            if is_new and (not first_scan or future_schedule):
                review_title = ('Review scheduled episode' if
                                typed_event and typed_event.get('event_type') ==
                                'episode_announcement' else 'Review announcement')
                queue_graph_review(
                    db, 'release_announcement', f'{review_title}: {title}',
                    {'title': title, 'source_url': entry_url, 'feed_url': feed_url,
                     'feed_title': manifest.get('title'),
                     'summary': summary,
                     'schedule_date': schedule_date,
                     'provider_metadata': provider_metadata,
                     'announcement_date': published,
                     'media_type': 'game' if watch.get('kind') == 'game' else 'video',
                     'provider': 'announcement_feed', 'provider_entry_id': entry_key,
                     'typed_event_candidate': typed_event},
                    entity_id=int(entity_id),
                    dedupe_key=f'release-announcement:{entity_id}:{entry_key}')
        state['known_entries'] = sorted(known)[-5000:]
        state['last_checked_at'] = now_text
        state['feed_title'] = manifest.get('title')
        states[feed_url] = state
    metadata['announcement_feed_state'] = states
    metadata['announcement_feed_last_checked_at'] = now_text
    return errors


def _classify_release_announcement(entry):
    """Extract conservative, review-only event types from feed text and summaries."""
    entry = entry if isinstance(entry, dict) else {}
    title = str(entry.get('title') or '').strip()
    categories = [str(value).strip() for value in entry.get('categories') or []
                  if str(value).strip()]
    summary = _announcement_summary(entry)
    text_sources = ([('title', title)] if title else [])
    text_sources.extend(('category', value) for value in categories)
    if summary:
        text_sources.append(('summary', summary))
    provider_metadata = entry.get('provider_metadata')
    if not isinstance(provider_metadata, dict):
        provider_metadata = {}
    raw_event_type = str(entry.get('event_type') or provider_metadata.get('event_type') or '').strip()
    normalized_event_type = re.sub(r'[ -]+', '_', raw_event_type.casefold())
    event_type_aliases = {
        'episode': 'episode_announcement', 'new_episode': 'episode_announcement',
        'premiere': 'premiere_announcement', 'availability': 'availability_announcement',
        'streaming': 'availability_announcement', 'renewal': 'series_renewal',
        'cancellation': 'series_cancellation', 'cancelled': 'series_cancellation',
        'canceled': 'series_cancellation', 'hiatus': 'series_hiatus',
        'release_change': 'release_date_change', 'date_change': 'release_date_change',
    }
    normalized_event_type = event_type_aliases.get(normalized_event_type, normalized_event_type)
    if normalized_event_type in {
            'series_cancellation', 'series_renewal', 'series_hiatus',
            'release_date_change', 'premiere_announcement',
            'availability_announcement', 'episode_announcement'}:
        text_sources.insert(0, ('event_type', normalized_event_type.replace('_', ' ')))
    rules = ((normalized_event_type, r'.+'),) if text_sources and text_sources[0][0] == 'event_type' else ()
    rules += (
        ('series_cancellation', r'\b(cancelled|canceled|cancellation)\b'),
        ('series_renewal', r'\b(renewed|renewal|greenlit|picked up for)\b'),
        ('series_hiatus', r'\b(hiatus|hiatuses)\b'),
        ('release_date_change', r'\b(delayed|postponed|pushed back|release date changed)\b'),
        ('episode_announcement',
         r'\b(?:new\s+episode|episode(?:\s*#?\s*\d+)?|episodic)\b'),
        ('premiere_announcement', r'\b(premiere|premieres|premiered|debut)\b'),
        ('availability_announcement',
         r'\b(streaming on|available on|now available|arrives on|availability)\b'),
    )
    for event_type, pattern in rules:
        matches = [(source, value, re.search(pattern, value, flags=re.I))
                   for source, value in text_sources]
        matches = [item for item in matches if item[2]]
        if not matches:
            continue
        source, value, match = next(
            (item for item in matches if item[0] == 'category'), matches[0])
        confidence = {'event_type': 0.96, 'category': 0.95,
                      'title': 0.88, 'summary': 0.78}[source]
        candidate = {'event_type': event_type, 'confidence': confidence,
                     'matched_text': match.group(0),
                     'matched_text_source': source, 'categories': categories[:20]}
        for date_source, date_text in text_sources:
            announced_date = _extract_announced_release_date(date_text)
            if announced_date:
                announced_date['announced_date_source'] = (
                    f'announcement {date_source}')
                candidate.update(announced_date)
                break
        scheduled = _exact_date(provider_metadata.get('schedule_date'))
        schedule_context = {
            'premiere_announcement': 'premiere',
            'availability_announcement': 'availability',
            'release_date_change': 'release',
            'episode_announcement': 'release',
        }.get(event_type)
        if scheduled and schedule_context:
            if candidate.get('announced_date') and candidate['announced_date'] != scheduled:
                candidate['calendar_date_conflict'] = {
                    'text_date': candidate['announced_date'],
                    'text_date_source': candidate.get('announced_date_source'),
                    'calendar_date': scheduled,
                    'calendar_date_source': 'announcement calendar DTSTART',
                }
            candidate.update({
                'announced_date': scheduled,
                'announced_date_precision': 'day',
                'announced_date_context': schedule_context,
                'announced_date_source': 'announcement calendar DTSTART',
            })
            if (event_type == 'availability_announcement' and
                    provider_metadata.get('location')):
                candidate.setdefault('announced_platform',
                                     str(provider_metadata['location'])[:100])
        return candidate
    return None


def _announcement_summary(entry):
    """Return bounded, plain text feed summary for classification and review evidence."""
    entry = entry if isinstance(entry, dict) else {}
    provider_metadata = entry.get('provider_metadata')
    if not isinstance(provider_metadata, dict):
        provider_metadata = {}
    value = (entry.get('summary') or entry.get('description') or
             provider_metadata.get('summary') or '')
    value = html.unescape(re.sub(r'<[^>]*>', ' ', str(value)))
    value = re.sub(r'\s+', ' ', value).strip()
    return value[:4000]


def _exact_date(value):
    """Parse only a day-precision date suitable for a release-day search."""
    if isinstance(value, dict):
        value = value.get('@value') or value.get('name') or ''
    text = str(value or '').strip()
    iso = re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:[Tt ].*)?', text)
    if iso:
        try:
            return dt.date.fromisoformat(iso.group(1)).isoformat()
        except ValueError:
            return None
    for form in ('%B %d, %Y', '%b %d, %Y', '%d %B %Y', '%d %b %Y'):
        try:
            return dt.datetime.strptime(text, form).date().isoformat()
        except ValueError:
            continue
    return None


def _extract_announced_release_date(text):
    """Extract a date only when announcement text ties it to a release schedule."""
    text = html.unescape(re.sub(r'<[^>]*>', ' ', str(text or '')))
    text = re.sub(r'\s+', ' ', text).strip()
    context_patterns = (
        ('premiere', r'\b(?:premiere(?:s|d)?|debut(?:s|ed)?|airs?)\b'),
        ('availability', r'\b(?:available|arrives|stream(?:s|ing)?|launch(?:es|ed)?)\b'),
        ('release', r'\b(?:release(?:s|d)?(?:\s+date)?|launch\s+date)\b'),
    )
    marker = None
    for context, pattern in context_patterns:
        match = re.search(pattern, text, flags=re.I)
        if match and (marker is None or match.start() < marker[0]):
            marker = (match.start(), match.end(), context)
    if marker is None:
        return None
    context_text = text[marker[1]:marker[1] + 180]
    def announced_platform(date_start):
        if marker[2] != 'availability':
            return None
        preceding = context_text[:date_start]
        match = re.search(
            r'\b(?:on|via|through)\s+([A-Za-z0-9][A-Za-z0-9&+.’\'/-]*'
            r'(?:\s+[A-Za-z0-9][A-Za-z0-9&+.’\'/-]*){0,3})\s*$',
            preceding, flags=re.I)
        if not match:
            return None
        value = match.group(1).strip()
        value = re.sub(r'\s+(?:starting|beginning|from|on|in)\s*$', '', value,
                       flags=re.I).strip()
        value = re.sub(r'\s+(?:starting|beginning)\s*$', '', value,
                       flags=re.I).strip()
        return value[:100] or None

    def dated_candidate(value, precision, date_start):
        candidate = {'announced_date': value, 'announced_date_precision': precision,
                     'announced_date_context': marker[2],
                     'announced_date_source': 'announcement text'}
        platform = announced_platform(date_start)
        if platform:
            candidate['announced_platform'] = platform
        return candidate

    month_names = (r'(January|February|March|April|May|June|July|August|September|October|'
                   r'November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)')
    full_date_patterns = (
        (r'\b(20\d{2})-(\d{2})-(\d{2})\b', 'iso'),
        (rf'\b{month_names}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b',
         'month_day_year'),
        (rf'\b(\d{{1,2}})\s+{month_names}\.?[,]?\s+(20\d{{2}})\b',
         'day_month_year'),
    )
    for pattern, layout in full_date_patterns:
        match = re.search(pattern, context_text, flags=re.I)
        if not match:
            continue
        try:
            if layout == 'iso':
                parsed = dt.date(*(int(part) for part in match.groups()))
            elif layout == 'month_day_year':
                month_name, day, year = match.groups()
                try:
                    parsed = dt.datetime.strptime(
                        f'{month_name} {day} {year}', '%B %d %Y').date()
                except ValueError:
                    parsed = dt.datetime.strptime(
                        f'{month_name} {day} {year}', '%b %d %Y').date()
            else:
                day, month_name, year = match.groups()
                try:
                    parsed = dt.datetime.strptime(
                        f'{month_name} {day} {year}', '%B %d %Y').date()
                except ValueError:
                    parsed = dt.datetime.strptime(
                        f'{month_name} {day} {year}', '%b %d %Y').date()
        except (TypeError, ValueError):
            continue
        return dated_candidate(parsed.isoformat(), 'day', match.start())

    month_year = re.search(rf'\b{month_names}\.?[,]?\s+(20\d{{2}})\b',
                           context_text, flags=re.I)
    if month_year:
        month_name, year = month_year.groups()
        try:
            month = dt.datetime.strptime(month_name, '%B').month
        except ValueError:
            try:
                month = dt.datetime.strptime(month_name, '%b').month
            except ValueError:
                return None
        return dated_candidate(f'{year}-{month:02d}', 'month', month_year.start())
    year_match = re.search(r'\b(20\d{2})\b', context_text)
    if year_match:
        return dated_candidate(year_match.group(1), 'year', year_match.start())
    return None


def _normalize_video_feed_source(feed_url):
    """Resolve common YouTube channel and playlist pages to their RSS endpoint."""
    value = str(feed_url or '').strip()
    parts = urllib.parse.urlsplit(value)
    host = (parts.hostname or '').casefold()
    youtube_host = host in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}
    query = urllib.parse.parse_qs(parts.query)
    membership = 'dedicated_feed'
    if youtube_host:
        playlist_id = (query.get('playlist_id') or query.get('list') or [''])[0]
        if playlist_id and re.fullmatch(r'[A-Za-z0-9_-]{5,100}', playlist_id):
            value = 'https://www.youtube.com/feeds/videos.xml?' + urllib.parse.urlencode(
                {'playlist_id': playlist_id})
            membership = 'youtube_playlist'
        elif parts.path == '/feeds/videos.xml' and query.get('channel_id'):
            channel_id = query['channel_id'][0]
            if re.fullmatch(r'UC[A-Za-z0-9_-]{20,30}', channel_id):
                value = 'https://www.youtube.com/feeds/videos.xml?' + urllib.parse.urlencode(
                    {'channel_id': channel_id})
            membership = 'youtube_channel'
        else:
            channel_match = re.fullmatch(r'/channel/(UC[A-Za-z0-9_-]{20,30})/?',
                                         parts.path)
            if channel_match:
                value = 'https://www.youtube.com/feeds/videos.xml?' + urllib.parse.urlencode(
                    {'channel_id': channel_match.group(1)})
                membership = 'youtube_channel'
    return value, membership


def _video_feed_entry_identity(feed_url, provider_metadata, entry_url):
    """Use a creator feed's native ID when present, with URL-based legacy fallback."""
    provider_metadata = provider_metadata if isinstance(provider_metadata, dict) else {}
    provider = str(provider_metadata.get('provider') or 'feed').strip().casefold()
    native_id = (provider_metadata.get('entry_id') or provider_metadata.get('video_id') or
                 provider_metadata.get('playlist_item_id'))
    if native_id:
        identity = '\n'.join((str(feed_url or ''), provider, str(native_id).strip()))
        return hashlib.sha256(identity.encode('utf-8')).hexdigest(), True
    return hashlib.sha256(str(entry_url or '').encode('utf-8')).hexdigest(), False


def _refresh_video_feed_watch(db, watch, entity_id, metadata, policy, source_options=None):
    source_url = str(metadata.get('video_feed_url') or '').strip()
    if not source_url:
        return False
    feed_url, membership = _normalize_video_feed_source(source_url)
    youtube_channel = membership == 'youtube_channel'
    youtube_playlist = membership == 'youtube_playlist'
    provider_manifest = None
    provider_error = None
    native_snapshot_complete = False
    if youtube_playlist:
        api_key = str((source_options or {}).get('youtube_api_key') or
                      os.environ.get('YOUTUBE_API_KEY', '')).strip()
        if api_key:
            playlist_id = urllib.parse.parse_qs(
                urllib.parse.urlsplit(feed_url).query).get('playlist_id', [''])[0]
            try:
                provider_manifest = fetch_youtube_playlist_items(playlist_id, api_key)
                native_snapshot_complete = bool(provider_manifest.get('snapshot_complete'))
            except Exception as exc:
                provider_error = str(exc)
                logger.info('YouTube playlist API refresh failed for %s: %s',
                            watch.get('title'), exc)
    manifest = provider_manifest or discover_feed(feed_url, 'webfiction', limit=500)
    metadata['video_feed_source_url'] = source_url
    metadata['video_feed_url'] = feed_url
    if provider_manifest:
        metadata['youtube_playlist_api_error'] = None
        metadata['youtube_schedule_error'] = provider_manifest.get('youtube_schedule_error')
        metadata['video_feed_provider_metadata'] = {
            'provider': 'youtube_data_api',
            'playlist_id': provider_manifest.get('youtube_playlist_id'),
            'snapshot_complete': native_snapshot_complete,
        }
    elif provider_error:
        metadata['youtube_playlist_api_error'] = provider_error[:500]
    feed_provider_metadata = manifest.get('provider_metadata')
    if isinstance(feed_provider_metadata, dict):
        metadata['video_feed_provider_metadata'] = dict(feed_provider_metadata)
    now_text = _now().isoformat(sep='T')
    baseline = str(metadata.get('video_feed_baselined_at') or '')
    first_scan = not baseline
    if first_scan:
        baseline = now_text
        metadata['video_feed_baselined_at'] = baseline
    known = set(str(value) for value in metadata.get('video_feed_known_episodes') or [])
    known_ids = set(str(value) for value in metadata.get('video_feed_known_ids') or [])
    watched = set(str(value) for value in metadata.get('video_feed_watched_episodes') or [])
    watched_ids = set(str(value) for value in metadata.get('video_feed_watched_ids') or [])
    seen_at = dict(metadata.get('video_feed_seen_at') or {})
    today = dt.date.today().isoformat()
    action = relationship_watch_action(policy, 'episodes', 0.95)
    policy_overrides = [get_watch_policy(db, scope, key) for scope, key in (
        ('global', ''), ('media_type', 'video'), ('series', str(entity_id)),
        ('work', str(entity_id))) ]
    has_episode_override = any('episodes' in value for value in policy_overrides)
    has_special_override = any('specials' in value for value in policy_overrides)
    if not has_episode_override:
        # A channel feed mixes independent uploads. Treat it as discovery until
        # the user opts into auto-adding episodes. A playlist or creator's
        # dedicated series feed is an explicit episode-membership source.
        action = 'discover' if youtube_channel else 'auto_add'
    special_action = (relationship_watch_action(policy, 'specials', 0.95)
                      if has_special_override else action)
    metadata['video_feed_membership'] = (
        'youtube_channel' if youtube_channel else
        'youtube_playlist' if youtube_playlist else 'dedicated_feed')
    if youtube_playlist and provider_manifest and native_snapshot_complete:
        prior_state = metadata.get('youtube_playlist_state')
        prior_state = prior_state if isinstance(prior_state, dict) else {}
        prior_items = prior_state.get('items')
        prior_items = prior_items if isinstance(prior_items, dict) else {}
        current_items = {
            str(item.get('playlist_item_id')): dict(item)
            for item in (provider_manifest.get('youtube_playlist_items') or [])
            if isinstance(item, dict) and item.get('playlist_item_id')
        }
        if prior_state.get('complete') and not first_scan:
            for removed_id in sorted(set(prior_items) - set(current_items)):
                removed = prior_items[removed_id]
                video_id = str(removed.get('video_id') or '')
                video_url = (f'https://www.youtube.com/watch?v={urllib.parse.quote(video_id, safe="")}'
                             if video_id else feed_url)
                removal_detail = {
                    'playlist_id': provider_manifest.get('youtube_playlist_id'),
                    'playlist_item_id': removed_id,
                    'video_id': video_id or None,
                    'title': removed.get('title'), 'position': removed.get('position'),
                    'channel_id': removed.get('channel_id'), 'source_url': video_url,
                    'observed_removed_at': now_text,
                    'evidence': 'Item is absent from a complete YouTube Data API playlist snapshot.',
                }
                upsert_media_release(
                    db, entity_id, 'playlist_item_removed', None, status='removed',
                    provider='youtube_data_api', provider_release_id=removed_id,
                    source_url=video_url, metadata=removal_detail)
                queue_graph_review(
                    db, 'playlist_item_removed',
                    f"Playlist item removed: {removed.get('title') or video_id or removed_id}",
                    removal_detail, entity_id=int(entity_id),
                    dedupe_key=f'youtube-playlist-removal:{entity_id}:{removed_id}')
        metadata['youtube_playlist_state'] = {
            'complete': True, 'items': current_items,
            'observed_at': now_text,
            'playlist_id': provider_manifest.get('youtube_playlist_id'),
        }
    elif youtube_playlist and provider_manifest:
        metadata['youtube_playlist_last_partial_scan_at'] = now_text

    item_metadata = dict(metadata.get('video_feed_item_metadata') or {})
    for old_url, old_item in list(item_metadata.items()):
        old_provider_metadata = old_item.get('provider_metadata') if isinstance(old_item, dict) else {}
        old_key, old_has_native_identity = _video_feed_entry_identity(
            feed_url, old_provider_metadata, old_url)
        if old_has_native_identity:
            known_ids.add(old_key)
            if old_url in watched:
                watched_ids.add(old_key)
    for episode in manifest.get('chapters') or []:
        episode_url = str(episode.get('url') or '').strip()
        if not episode_url:
            continue
        native_metadata = episode.get('provider_metadata')
        if not isinstance(native_metadata, dict):
            native_metadata = {}
        if provider_manifest:
            native_metadata = dict(native_metadata)
            native_metadata.setdefault('provider', 'youtube_data_api')
            native_metadata.setdefault('playlist_id', provider_manifest.get('youtube_playlist_id'))
        key, has_native_identity = _video_feed_entry_identity(
            feed_url, native_metadata, episode_url)
        was_known = (episode_url in known or
                     (has_native_identity and key in known_ids))
        is_new = not was_known
        known.add(episode_url)
        if has_native_identity:
            known_ids.add(key)
        if is_new:
            seen_at[episode_url] = now_text
        release_date = (_date(native_metadata.get('schedule_date')) or
                        _date(episode.get('published')))
        first_seen = str(seen_at.get(episode_url) or baseline)
        is_future = bool(release_date and release_date > today)
        eligible = is_future or (not first_scan and (is_new or first_seen > baseline))
        title = str(episode.get('title') or manifest.get('title') or 'Video episode').strip()
        entry_type = _video_feed_entry_type(
            title, episode.get('categories'), native_metadata)
        entry_action = (special_action if entry_type == 'bonus' and has_special_override
                        else action)
        needs_review = (entry_action == 'discover' or
                        (entry_action == 'auto_add' and entry_type in {'trailer', 'announcement'}))
        episode_position = _feed_episode_position(title, native_metadata)
        episode_position_id = (_episode_position_external_id(
            entity_id, episode_position[0], episode_position[1]) if episode_position else None)
        episode_external_ids = [{'provider': 'video_feed_episode', 'external_id': key,
                                 'source_url': episode_url}]
        if episode_position_id:
            episode_external_ids.append(episode_position_id)
            native_metadata = dict(native_metadata)
            native_metadata['season_number'], native_metadata['episode_number'] = episode_position
        item_metadata[episode_url] = {
            'title': title, 'published': episode.get('published'),
            'entry_type': entry_type, 'provider_metadata': native_metadata,
        }
        upsert_media_release(
            db, entity_id, 'episode', release_date,
            status='upcoming' if is_future else 'released', provider='video_feed',
            provider_release_id=key, source_url=episode_url,
            metadata={'title': title, 'feed_url': feed_url,
                      'video_subtype': 'special' if entry_type == 'bonus' else 'episode',
                      'feed_entry_type': entry_type,
                      'season_number': episode_position[0] if episode_position else None,
                      'episode_number': episode_position[1] if episode_position else None,
                      'provider_metadata': native_metadata})
        child_entity_id = _link_catalog_child(
            db, entity_id, 'work', 'video', title, 'video_feed_episode', key,
            episode_url, {'release_date': release_date,
                          'video_subtype': 'special' if entry_type == 'bonus' else 'episode',
                          'entity_type': 'work',
                          'membership_source': metadata['video_feed_membership'],
                          'feed_entry_type': entry_type,
                          'provider_metadata': native_metadata,
                          'external_ids': [value for value in episode_external_ids
                                           if value['provider'] == 'media-intake-episode']},
            relationship='contains', confidence=0.95,
            inverse_relationship=('special_of' if entry_type == 'bonus' else
                                  'episode_of' if entry_type == 'episode' else None),
            raw_relationship_type='rss-feed-entry',
            status=('ignored' if entry_action == 'ignore' else
                    'accepted' if entry_action == 'auto_add' and not needs_review else 'candidate'))
        if needs_review and is_new and (not first_scan or is_future):
            relation = db.execute('''SELECT id FROM media_relationships
                WHERE source_entity_id=? AND target_entity_id=?
                  AND relationship_type='contains' ''',
                                  (int(entity_id), int(child_entity_id or 0))).fetchone()
            review_title = ('Review feed trailer' if entry_type == 'trailer' else
                            'Review feed announcement' if entry_type == 'announcement' else
                            'Review possible episode')
            queue_graph_review(
                db, 'possible_feed_episode', f'{review_title}: {title}',
                {'series': watch.get('title'), 'title': title, 'author': manifest.get('author'),
                 'source_url': episode_url, 'feed_url': feed_url,
                 'release_date': release_date, 'feed_entry_key': key,
                 'feed_entry_type': entry_type, 'episode_position': episode_position,
                 'categories': episode.get('categories') or [],
                 'provider_metadata': native_metadata,
                 'membership_source': metadata['video_feed_membership']},
                entity_id=entity_id,
                relationship_id=int(relation['id']) if relation else None,
                dedupe_key=f'video-feed-candidate:{entity_id}:{key}')
        if (entry_action == 'auto_add' and entry_type not in {'trailer', 'announcement'} and
                eligible and episode_url not in watched and key not in watched_ids):
            child_metadata = {
                'sources': metadata.get('sources') or {}, 'entity_type': 'work',
                'catalog_provider': 'Video feed',
                'video_subtype': 'special' if entry_type == 'bonus' else 'episode',
                'external_ids': episode_external_ids,
                'episode': {'title': title, 'airdate': release_date,
                            'feed_url': feed_url,
                            'feed_entry_type': entry_type,
                            'provider_metadata': native_metadata},
            }
            child_id = add_watch(db, 'video_episode', title, manifest.get('author') or 'Unknown',
                                 episode_url, release_date if is_future else None,
                                 child_metadata, parent_id=int(watch['id']))
            if is_future:
                queue_watch_search(db, child_id, title, manifest.get('author') or 'Unknown',
                                   release_date, upcoming=True,
                                   publication_status='upcoming')
            watched.add(episode_url)
            watched_ids.add(key)
    metadata['video_feed_known_episodes'] = sorted(known)[-5000:]
    metadata['video_feed_known_ids'] = sorted(known_ids)[-5000:]
    metadata['video_feed_watched_episodes'] = sorted(watched)[-5000:]
    metadata['video_feed_watched_ids'] = sorted(watched_ids)[-5000:]
    metadata['video_feed_seen_at'] = {key: value for key, value in
                                      list(seen_at.items())[-5000:]}
    metadata['video_feed_title'] = manifest.get('title')
    metadata['video_feed_author'] = manifest.get('author')
    metadata['video_feed_item_metadata'] = dict(list(item_metadata.items())[-5000:])
    metadata['video_feed_last_checked_at'] = now_text
    return True


def _feed_episode_position(title, provider_metadata=None):
    """Return a season/episode tuple only when a feed title states it explicitly."""
    text = str(title or '')
    patterns = (
        r'\bS(?P<season>\d{1,3})\s*E(?P<episode>\d{1,4})\b',
        r'\b(?P<season>\d{1,3})\s*[xX]\s*(?P<episode>\d{1,4})\b',
        r'\bseason\s+(?P<season>\d{1,3})\s*(?:episode|ep\.?)[\s#:-]*'
        r'(?P<episode>\d{1,4})\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        try:
            season, episode = int(match.group('season')), int(match.group('episode'))
        except (TypeError, ValueError, OverflowError):
            continue
        if season >= 0 and episode > 0:
            return season, episode
    if isinstance(provider_metadata, dict):
        try:
            season = int(provider_metadata.get('season_number'))
            episode = int(provider_metadata.get('episode_number'))
        except (TypeError, ValueError, OverflowError):
            return None
        if 0 <= season <= 999 and 1 <= episode <= 9999:
            return season, episode
    return None


def _video_feed_entry_type(title, categories=None, provider_metadata=None):
    """Classify explicit creator-feed markers without guessing from generic summaries."""
    provider_metadata = provider_metadata if isinstance(provider_metadata, dict) else {}
    episode_type = str(provider_metadata.get('episode_type') or '').strip().casefold()
    episode_type = episode_type.replace('-', '_').replace(' ', '_')
    category_values = [str(value or '').strip().casefold() for value in categories or []]
    title_text = str(title or '').strip()
    summary = _announcement_summary({'provider_metadata': provider_metadata})

    trailer_pattern = r'\b(?:official\s+)?(?:teaser\s+)?(?:trailer|teaser|preview)s?\b'
    trailer_title = (r'^(?:\[\s*' + trailer_pattern + r'\s*\]|' +
                     trailer_pattern + r')(?:\s*[:\-|]|\s+for\b|\s+of\b|$)')
    trailer_summary = (r'^(?:watch\s+)?' + trailer_pattern +
                       r'(?:\s*[:\-|]|\s+for\b|\s+of\b|$)')
    if (episode_type in {'trailer', 'teaser', 'preview'} or
            any(re.search(trailer_pattern, value, flags=re.I) for value in category_values) or
            re.match(trailer_title, title_text, flags=re.I) or
            re.match(trailer_summary, summary, flags=re.I)):
        return 'trailer'

    bonus_pattern = (r'\b(?:bonus|extra|behind[ -]the[ -]scenes|aftershow|post[ -]show)'
                     r'(?:\s+(?:episode|clip|content|featurette))?\b')
    bonus_title = r'^' + bonus_pattern + r'(?:\s*[:\-|]|\s+for\b|$)'
    if (episode_type in {'bonus', 'extra', 'behind_the_scenes', 'aftershow'} or
            any(re.search(bonus_pattern, value, flags=re.I) for value in category_values) or
            re.match(bonus_title, title_text, flags=re.I) or
            re.match(bonus_title, summary, flags=re.I)):
        return 'bonus'

    structured_event_type = str(provider_metadata.get('event_type') or '').strip().casefold()
    structured_event_type = re.sub(r'[ -]+', '_', structured_event_type)
    if structured_event_type in {
            'premiere', 'premiere_announcement', 'screening', 'availability',
            'availability_announcement', 'renewal', 'series_renewal',
            'cancellation', 'series_cancellation', 'hiatus', 'series_hiatus',
            'release_date_change', 'episode_announcement', 'announcement'}:
        return 'announcement'

    announcement_pattern = (r'\b(?:site\s+)?(?:announcement|schedule\s+update|'
                            r'site\s+update|news\s+update)\b')
    announcement_prefix = (r'^(?:site\s+)?(?:announcement|schedule\s+update|'
                           r'site\s+update|news\s+update)(?:\s*[:\-|]|$)')
    if (episode_type in {'announcement', 'news_update', 'schedule_update'} or
            any(re.search(announcement_pattern, value, flags=re.I)
                for value in category_values) or
            re.match(announcement_prefix, title_text, flags=re.I) or
            re.match(announcement_prefix, summary, flags=re.I)):
        return 'announcement'
    return 'episode'

def _accept_video_feed_episode_review(db, entity_id, detail):
    """Turn an explicitly accepted feed candidate into a watched episode."""
    detail = detail if isinstance(detail, dict) else {}
    parent = db.execute('''SELECT * FROM watched_items
        WHERE media_entity_id=? AND kind='video' ORDER BY id LIMIT 1''',
                        (int(entity_id),)).fetchone()
    if not parent:
        raise ValueError('Accepting this feed entry requires its parent video watch')
    parent = dict(parent)
    parent_metadata = json.loads(parent.get('metadata') or '{}')
    title = str(detail.get('title') or '').strip()
    source_url = str(detail.get('source_url') or '').strip()
    feed_url = str(detail.get('feed_url') or parent_metadata.get('video_feed_url') or '').strip()
    if not title or not source_url or not feed_url:
        raise ValueError('The selected feed entry is missing its title or source URL')
    release_date = _date(detail.get('release_date'))
    is_future = bool(release_date and release_date > dt.date.today().isoformat())
    provider_metadata = detail.get('provider_metadata')
    provider_metadata = dict(provider_metadata) if isinstance(provider_metadata, dict) else {}
    stable_key, has_native_identity = _video_feed_entry_identity(
        feed_url, provider_metadata, source_url)
    entry_key = str(detail.get('feed_entry_key') or stable_key)
    position = detail.get('episode_position')
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        position = _feed_episode_position(title, provider_metadata)
    external_ids = [{'provider': 'video_feed_episode', 'external_id': entry_key,
                     'source_url': source_url}]
    if position:
        position_id = _episode_position_external_id(entity_id, position[0], position[1])
        if position_id:
            external_ids.append(position_id | {'source_url': source_url})
            provider_metadata['season_number'], provider_metadata['episode_number'] = position
    entry_type = str(detail.get('feed_entry_type') or 'episode').casefold()
    video_subtype = 'special' if entry_type == 'bonus' else 'episode'
    child_metadata = {
        'sources': parent_metadata.get('sources') or {}, 'entity_type': 'work',
        'catalog_provider': 'Video feed', 'video_subtype': video_subtype,
        'external_ids': external_ids,
        'episode': {'title': title, 'airdate': release_date, 'feed_url': feed_url,
                    'feed_entry_type': entry_type,
                    'provider_metadata': provider_metadata},
    }
    child_id = add_watch(
        db, 'video_episode', title, detail.get('author') or parent.get('author') or 'Unknown',
        source_url, release_date if is_future else None, child_metadata,
        parent_id=int(parent['id']))
    child = db.execute('SELECT * FROM watched_items WHERE id=?', (int(child_id),)).fetchone()
    if child and child['parent_id'] is None:
        db.execute('UPDATE watched_items SET parent_id=? WHERE id=?',
                   (int(parent['id']), int(child_id)))
        sync_watch_entity(db, int(child_id))
    if child and child['state'] != 'downloaded':
        queue_watch_search(
            db, child_id, title, detail.get('author') or parent.get('author') or 'Unknown',
            release_date, upcoming=is_future,
            publication_status='upcoming' if is_future else 'released')
    watched = list(parent_metadata.get('video_feed_watched_episodes') or [])
    if source_url not in watched:
        watched.append(source_url)
    parent_metadata['video_feed_watched_episodes'] = watched[-5000:]
    watched_ids = list(parent_metadata.get('video_feed_watched_ids') or [])
    if entry_key not in watched_ids:
        watched_ids.append(entry_key)
    parent_metadata['video_feed_watched_ids'] = watched_ids[-5000:]
    known = list(parent_metadata.get('video_feed_known_episodes') or [])
    if source_url not in known:
        known.append(source_url)
    parent_metadata['video_feed_known_episodes'] = known[-5000:]
    known_ids = list(parent_metadata.get('video_feed_known_ids') or [])
    if has_native_identity and stable_key not in known_ids:
        known_ids.append(stable_key)
    parent_metadata['video_feed_known_ids'] = known_ids[-5000:]
    db.execute('UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (json.dumps(parent_metadata, ensure_ascii=False), int(parent['id'])))
    db.commit()
    return int(child_id)


def _episode_position_external_id(series_entity_id, season_number, episode_number):
    """Build a provider-neutral episode key when both catalog positions exist."""
    try:
        season_number, episode_number = int(season_number), int(episode_number)
    except (TypeError, ValueError, OverflowError):
        return None
    if season_number < 0 or episode_number <= 0:
        return None
    return {'provider': 'media-intake-episode',
            'external_id': f'{int(series_entity_id)}:S{season_number:02d}E{episode_number:02d}'}


def _link_catalog_child(db, parent_id, entity_type, media_type, title, provider,
                        provider_id, source_url, metadata, relationship='contains',
                        confidence=0.9, status='candidate', relationship_from_child=False,
                        raw_relationship_type=None, inverse_relationship=None):
    title = str(title or '').strip()
    if not title or not provider_id:
        return None
    try:
        external_ids = [{'provider': provider, 'external_id': str(provider_id),
                         'source_url': source_url}]
        for value in (metadata or {}).get('external_ids') or []:
            if (isinstance(value, dict) and value.get('provider') and
                    value.get('external_id')):
                external_ids.append(value)
        child_id = upsert_media_entity(
            db, entity_type, title, media_type=media_type,
            canonical_url=source_url, release_date=metadata.get('release_date'),
            status=metadata.get('status'), metadata=metadata,
            external_ids=external_ids, provider=provider)
        source_entity_id, target_entity_id = (
            (child_id, int(parent_id)) if relationship_from_child else
            (int(parent_id), child_id))
        add_media_relationship(
            db, source_entity_id, target_entity_id, relationship, confidence=confidence,
            inferred=False, status=status, provider=provider,
            provider_entity_id=str(provider_id),
            raw_relationship_type=(raw_relationship_type or relationship),
            source_url=source_url, evidence=metadata, is_explicit=True)
        if inverse_relationship:
            add_media_relationship(
                db, child_id, int(parent_id), inverse_relationship,
                confidence=confidence, inferred=False, status=status, provider=provider,
                provider_entity_id=str(provider_id),
                raw_relationship_type=(raw_relationship_type or relationship),
                source_url=source_url, evidence=metadata, is_explicit=True)
        return child_id
    except ValueError as exc:
        queue_graph_review(db, 'suspected_duplicate',
                           f'Catalog relationship needs review: {title}',
                           {'provider': provider, 'provider_id': provider_id,
                            'source_url': source_url, 'error': str(exc)},
                           entity_id=int(parent_id),
                           dedupe_key=f'catalog-child:{provider}:{provider_id}:{parent_id}')
        return None


def _process_video_game_watch(db, watch, library_folder, source_overrides=None):
    """Refresh metadata and release graph, then search files at the right time."""
    options = dict(source_overrides or {})
    watch = dict(watch)
    metadata = json.loads(watch.get('metadata') or '{}')
    entity_id = int(watch.get('media_entity_id') or sync_watch_entity(db, watch['id']))
    media_type = 'game' if watch['kind'] == 'game' else 'video'
    sub_type_value = (metadata.get('game_subtype') if media_type == 'game'
                      else metadata.get('video_subtype'))
    sub_type = str(sub_type_value or '').casefold()
    entity_scope = str(metadata.get('entity_type') or 'work').casefold()
    scopes = [(entity_scope, str(entity_id))]
    if entity_scope != 'work':
        scopes.append(('work', str(entity_id)))
    policy = effective_watch_policy(db, media_type, scopes)
    specials_override = _watch_policy_has_override(db, media_type, scopes, 'specials')
    configured_special_action = relationship_watch_action(policy, 'specials', 0.9)
    release_date = watch.get('release_date')
    status = (metadata.get('catalog_status')
              if metadata.get('catalog_status_source') in {'review', 'provider_consensus'} else None)
    title = watch.get('title')
    catalog_provider = str(metadata.get('catalog_provider') or '').strip()
    provider_records = _normalized_provider_records(db, entity_id)
    errors = []
    refreshed = False
    tmdb_key = str(options.get('tmdb_api_key') or os.environ.get('TMDB_API_KEY', '')).strip()
    igdb_client_id = str(options.get('igdb_client_id') or os.environ.get('IGDB_CLIENT_ID', '')).strip()
    igdb_client_secret = str(options.get('igdb_client_secret') or
                              os.environ.get('IGDB_CLIENT_SECRET', '')).strip()
    root_url = watch.get('source_url')
    today = dt.date.today().isoformat()

    for provider, external_id in provider_records:
        if provider != 'wikidata':
            continue
        try:
            imported = import_wikidata_entity(db, external_id, watch_entity_id=entity_id)
            if int(imported.get('root_entity_id') or 0) != entity_id:
                raise ValueError('Wikidata ID resolves to a different graph entity; it was queued for review')
            _apply_wikidata_watch_policy(db, watch, imported)
            wikidata_subtype = str(imported.get('video_subtype') or '').strip()
            primary_provider = str(metadata.get('catalog_provider') or '').casefold()
            if (media_type == 'video' and wikidata_subtype and
                    (primary_provider in {'', 'wikidata'} or sub_type in {'', 'video', 'anime'})):
                metadata['video_subtype'] = wikidata_subtype
                sub_type = wikidata_subtype.casefold()
            refreshed = True
        except Exception as exc:
            errors.append(f'Wikidata {external_id}: {exc}')
            logger.info('Could not refresh Wikidata metadata for %s: %s',
                        watch.get('title'), exc)

    def update_root(provider, new_title=None, new_date=None, new_status=None,
                    extra=None, source_url=None):
        nonlocal title, release_date, status, catalog_provider, refreshed
        if new_title:
            title = str(new_title)
        if new_date and metadata.get('release_date_source') not in {'manual', 'review'}:
            release_date = str(new_date)
        status_source = metadata.get('catalog_status_source')
        if new_status and status_source != 'review':
            status = _normalized_release_status(new_status)
        elif status_source in {'review', 'provider_consensus'}:
            status = metadata.get('catalog_status') or status
        catalog_provider = catalog_provider or provider
        merged = dict(metadata)
        if extra:
            merged.update(extra)
        merged.update({'entity_type': merged.get('entity_type') or 'work',
                       'catalog_provider': catalog_provider})
        if status:
            merged['catalog_status'] = status
        metadata.update(merged)
        upsert_media_entity(
            db, str(merged.get('entity_type') or 'work'), title, media_type=media_type,
            canonical_url=source_url or root_url, release_date=release_date,
            status=(None if status_source == 'review' or
                    (status_source == 'provider_consensus' and not new_status) else status),
            metadata=merged, entity_id=entity_id, provider=provider)
        if new_date or status:
            upsert_media_release(
                db, entity_id, 'game_launch' if media_type == 'game' else 'premiere',
                _date(new_date), status=status, provider=provider,
                provider_release_id='root-release', source_url=source_url or root_url,
                metadata={'title': title, 'subtype': sub_type})
        refreshed = True

    if metadata.get('announcement_feed_urls'):
        errors.extend(_refresh_announcement_feeds(db, watch, entity_id, metadata))
        refreshed = True

    if metadata.get('video_feed_url'):
        try:
            _refresh_video_feed_watch(db, watch, entity_id, metadata, policy, options)
            feed_title = metadata.get('video_feed_title')
            update_root('Video Feed', feed_title, None, None,
                        {'entity_type': 'series',
                         'video_subtype': metadata.get('video_subtype') or 'web_series',
                         'video_feed_url': metadata['video_feed_url']},
                        metadata['video_feed_url'])
        except Exception as exc:
            errors.append(f'Video feed: {exc}')
            logger.info('Could not refresh the video feed for %s: %s', watch.get('title'), exc)

    for provider, external_id in provider_records:
        try:
            if provider == 'wikidata':
                continue
            if provider == 'anilist':
                details = fetch_anilist_media(external_id)
                title_value = details.get('title') or {}
                anime_type = str(details.get('type') or '').upper() == 'ANIME'
                format_value = str(details.get('format') or '').upper()
                date_value = _anilist_date(details.get('startDate'))
                raw_status = str(details.get('status') or '')
                is_series = format_value in {'TV', 'TV_SHORT', 'ONA'}
                anime_subtype = anilist_video_subtype(format_value) if anime_type else None
                update_root('AniList', title_value.get('english') or title_value.get('romaji') or
                            title_value.get('native'), date_value,
                            _normalized_release_status(raw_status),
                            {'anilist_details': {
                                'id': str(details.get('id')), 'format': format_value,
                                'status': raw_status, 'episodes': details.get('episodes'),
                                'chapters': details.get('chapters'), 'volumes': details.get('volumes'),
                                'end_date': _anilist_date(details.get('endDate')),
                                'description': re.sub(r'\s+', ' ', str(details.get('description') or '')).strip(),
                                'media_type': 'anime' if anime_type else 'manga',
                            }, 'entity_type': 'series' if is_series else 'work',
                               'video_subtype': anime_subtype},
                            details.get('siteUrl'))
                airing = details.get('nextAiringEpisode')
                if isinstance(airing, dict) and airing.get('airingAt'):
                    try:
                        air_date = dt.datetime.fromtimestamp(
                            int(airing['airingAt']), dt.timezone.utc).date().isoformat()
                    except (TypeError, ValueError, OSError):
                        air_date = None
                    upsert_media_release(
                        db, entity_id, 'episode', air_date,
                        status='upcoming' if air_date and air_date > dt.date.today().isoformat() else 'released',
                        provider='AniList', provider_release_id=f"episode:{airing.get('episode')}",
                        source_url=details.get('siteUrl'),
                        metadata={'episode_number': airing.get('episode'),
                                  'video_subtype': 'episode'})
                    if (air_date and relationship_watch_action(policy, 'episodes', 0.9) == 'auto_add'):
                        episode_number = str(airing.get('episode') or 'Upcoming')
                        episode_title = f"{title} Episode {episode_number}"
                        episode_key = f'{external_id}:{episode_number}'
                        episode_meta = {
                            'sources': metadata.get('sources') or {}, 'entity_type': 'work',
                            'catalog_provider': 'AniList', 'video_subtype': 'episode',
                            'external_ids': [{'provider': 'anilist_episode',
                                              'external_id': episode_key,
                                              'source_url': details.get('siteUrl')}],
                            'episode': {'number': episode_number, 'airdate': air_date,
                                        'show_id': str(external_id)},
                        }
                        episode_watch_id = add_watch(
                            db, 'video_episode', episode_title, 'Unknown', None,
                            air_date if air_date > dt.date.today().isoformat() else None,
                            episode_meta, parent_id=int(watch['id']))
                        if air_date > dt.date.today().isoformat():
                            queue_watch_search(db, episode_watch_id, episode_title,
                                               'Unknown', air_date, upcoming=True,
                                               publication_status='upcoming')
                relation_map = {
                    # AniList describes the node relative to the current title.
                    # These edges point back from the node to the work it follows
                    # or spins off from; keep the graph's subject-first direction.
                    'SEQUEL': ('sequel_to', 'direct_sequel_series', True),
                    'PREQUEL': ('prequel_to', 'direct_prequel_series', True),
                    'SPIN_OFF': ('spin_off_of', 'spin_off_of', True),
                    'SIDE_STORY': ('parallel_story', 'parallel_story', False),
                    'CHARACTER': ('character_spin_off', 'character_spin_off', False),
                    # AniList lists a remake as a related node on the original;
                    # normalize the derivative back toward its source.
                    'REMAKE': ('remake_of', 'remake_of', True),
                    'PARENT': ('part_of', 'same_universe', False),
                    'ADAPTATION': ('adaptation_of', 'adaptation_of', False),
                    'SUMMARY': ('collection_of', 'collection_of', False),
                    'COMPILATION': ('collection_of', 'collection_of', False),
                    'ALTERNATIVE': ('alternate_continuity_of', 'alternate_continuity_of', False),
                }
                edges = ((details.get('relations') or {}).get('edges') or [])
                for edge in edges[:100]:
                    relation_type = str(edge.get('relationType') or '').upper()
                    mapped = relation_map.get(relation_type)
                    node = edge.get('node') or {}
                    if not mapped or not node.get('id') or str(node.get('id')) == str(external_id):
                        continue
                    node_title = node.get('title') or {}
                    node_title = node_title.get('english') or node_title.get('romaji') or node_title.get('native')
                    if not node_title:
                        continue
                    graph_relation, policy_key, relationship_from_child = mapped
                    node_anime = str(node.get('type') or '').upper() == 'ANIME'
                    node_format = str(node.get('format') or '').upper()
                    node_series = node_format in {'TV', 'TV_SHORT', 'ONA'}
                    node_type = 'series' if node_series else 'work'
                    node_kind = 'video' if node_anime else 'manga'
                    node_media_type = 'video' if node_anime else 'manga'
                    node_subtype = anilist_video_subtype(node_format) if node_anime else None
                    node_url = node.get('siteUrl') or f'https://anilist.co/{"anime" if node_anime else "manga"}/{node["id"]}'
                    node_release = _anilist_date(node.get('startDate'))
                    if relation_type in {'SEQUEL', 'PREQUEL'}:
                        policy_key = continuation_watch_policy_key(
                            graph_relation, node_type, node_release)
                    action = relationship_watch_action(policy, policy_key, 0.88)
                    linked = _link_catalog_child(
                        db, entity_id, node_type, node_media_type, node_title,
                        'anilist', node['id'], node_url,
                        {'video_subtype': node_subtype,
                         'release_date': node_release,
                         'entity_type': node_type},
                        relationship=graph_relation, confidence=0.88,
                        relationship_from_child=relationship_from_child,
                        raw_relationship_type=relation_type,
                        status=('ignored' if action == 'ignore' else
                                'candidate' if action == 'discover' else 'accepted'))
                    if linked and action == 'auto_add':
                        node_status = _normalized_release_status(node.get('status'))
                        child_meta = {'sources': metadata.get('sources') or {},
                                      'entity_type': node_type,
                                      'catalog_provider': 'AniList',
                                      'external_ids': [{'provider': 'anilist',
                                                        'external_id': str(node['id']),
                                                        'source_url': node_url}],
                                      'catalog_status': node_status,
                                      'video_subtype': node_subtype}
                        add_watch(db, node_kind, node_title, 'Unknown', node_url,
                                  node_release, child_meta)
            elif provider in {'tmdb_movie', 'tmdb_tv'} and tmdb_key:
                api_type = 'movie' if provider.endswith('_movie') else 'tv'
                details = fetch_tmdb_media(external_id, api_type, tmdb_key)
                date_value = _date(details.get('release_date') if api_type == 'movie'
                                   else details.get('first_air_date'))
                update_root('TMDB', details.get('title') or details.get('name'),
                            date_value,
                            details.get('status'),
                            {'tmdb_details': details,
                             'tmdb_collection': details.get('belongs_to_collection')},
                            f'https://www.themoviedb.org/{api_type}/{external_id}')
                if api_type == 'movie':
                    try:
                        release_regions = fetch_tmdb_release_dates(external_id, tmdb_key)
                        _save_tmdb_movie_release_windows(
                            db, entity_id, release_regions,
                            f'https://www.themoviedb.org/movie/{external_id}')
                    except Exception as exc:
                        errors.append(f'TMDB release dates: {exc}')
                        logger.info('Could not refresh TMDB release windows for %s: %s',
                                    watch.get('title'), exc)
                if api_type == 'movie' and details.get('belongs_to_collection'):
                    collection = details['belongs_to_collection']
                    collection_id = collection.get('id')
                    if collection_id:
                        collection_entity = _link_catalog_child(
                            db, entity_id, 'collection', 'video', collection.get('name'),
                            'tmdb_collection', collection_id,
                            f'https://www.themoviedb.org/collection/{collection_id}',
                            {'tmdb_id': str(collection_id), 'video_subtype': 'collection'},
                            relationship='part_of', status='candidate',
                            raw_relationship_type='belongs_to_collection')
                        if collection_entity:
                            try:
                                collection_data = fetch_tmdb_collection(collection_id, tmdb_key)
                                for part in (collection_data.get('parts') or [])[:100]:
                                    _link_catalog_child(
                                        db, collection_entity, 'work', 'video', part.get('title'),
                                        'tmdb_movie', part.get('id'),
                                        f"https://www.themoviedb.org/movie/{part.get('id')}",
                                        {'release_date': part.get('release_date'),
                                         'video_subtype': 'movie',
                                         'overview': part.get('overview') or ''},
                                        relationship='contains', status='candidate',
                                        raw_relationship_type='parts')
                            except Exception as exc:
                                errors.append(f'TMDB collection: {exc}')
                if api_type == 'tv':
                    next_episode = details.get('next_episode_to_air')
                    if isinstance(next_episode, dict) and next_episode.get('id'):
                        episode_date = _date(next_episode.get('air_date'))
                        upsert_media_release(
                            db, entity_id, 'episode', episode_date,
                            status='upcoming' if episode_date and episode_date > dt.date.today().isoformat() else 'released',
                            provider='TMDB', provider_release_id=f"episode:{next_episode['id']}",
                            source_url=f"https://www.themoviedb.org/tv/{external_id}/episode/"
                                       f"{next_episode.get('season_number')}/{next_episode.get('episode_number')}",
                            metadata={'title': next_episode.get('name'),
                                      'season_number': next_episode.get('season_number'),
                                      'episode_number': next_episode.get('episode_number'),
                                      'video_subtype': 'episode'})
                    season_action = relationship_watch_action(policy, 'new_seasons', 0.9)
                    episode_action = relationship_watch_action(policy, 'episodes', 0.9)
                    special_action = (configured_special_action if specials_override
                                      else episode_action)
                    baseline = (metadata.get('episodes_baselined_at') or
                                str(watch.get('created_at') or '')[:10] or
                                dt.date.today().isoformat())
                    if not metadata.get('episodes_baselined_at'):
                        metadata['episodes_baselined_at'] = dt.date.today().isoformat()
                    for season in (details.get('seasons') or [])[:100]:
                        season_no = season.get('season_number')
                        try:
                            season_no = int(season_no)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        season_url = f'https://www.themoviedb.org/tv/{external_id}/season/{season_no}'
                        season_entity_id = season.get('id') or f'{external_id}:season:{season_no}'
                        season_date = _date(season.get('air_date'))
                        if season_action != 'ignore':
                            _link_catalog_child(
                                db, entity_id, 'season', 'video',
                                f"{details.get('name') or title} Season {season_no}".strip(),
                                'tmdb_season', season_entity_id, season_url,
                                {'entity_type': 'season', 'season_number': season_no,
                                 'release_date': season_date,
                                 'status': ('released' if season_date and season_date <= today
                                            else 'upcoming')},
                                relationship='contains',
                                inverse_relationship='season_of',
                                raw_relationship_type='seasons',
                                status=('accepted' if season_action == 'auto_add' else 'candidate'))
                        try:
                            season_data = fetch_tmdb_season(external_id, season_no, tmdb_key)
                        except Exception as exc:
                            errors.append(f'TMDB season {season_no}: {exc}')
                            continue
                        for episode in (season_data.get('episodes') or [])[:500]:
                            episode_id = episode.get('id')
                            episode_no = episode.get('episode_number')
                            if not episode_id:
                                continue
                            try:
                                episode_no = int(episode_no)
                            except (TypeError, ValueError, OverflowError):
                                episode_no = None
                            episode_date = _date(episode.get('air_date'))
                            episode_status = ('upcoming' if episode_date and episode_date > today
                                              else 'released')
                            episode_subtype = 'special' if season_no == 0 else 'episode'
                            work_action = (special_action if episode_subtype == 'special'
                                           else episode_action)
                            episode_url = (f'{season_url}/episode/{episode_no}'
                                           if episode_no is not None else
                                           f'{season_url}/episode/{episode_id}')
                            position_id = _episode_position_external_id(
                                entity_id, season_no, episode_no)
                            episode_title = str(episode.get('name') or '').strip()
                            episode_code = (f'S{season_no:02d}E{episode_no:02d}'
                                            if episode_no is not None else f'Episode {episode_id}')
                            child_title = f"{details.get('name') or title} {episode_code} - {episode_title}".strip(' -')
                            position_ids = [position_id] if position_id else []
                            child_entity = _link_catalog_child(
                                db, entity_id, 'work', 'video', child_title,
                                'tmdb_episode', episode_id, episode_url,
                                {'entity_type': 'work', 'video_subtype': episode_subtype,
                                 'release_date': episode_date, 'status': episode_status,
                                 'season_number': season_no, 'episode_number': episode_no,
                                 'external_ids': position_ids},
                                relationship='contains', confidence=0.94,
                                inverse_relationship=('special_of' if episode_subtype == 'special'
                                                      else 'episode_of'),
                                raw_relationship_type='season.episodes',
                                status=('ignored' if work_action == 'ignore' else
                                        'accepted' if work_action == 'auto_add' else 'candidate'))
                            upsert_media_release(
                                db, entity_id, 'episode', episode_date,
                                status=episode_status, provider='TMDB',
                                provider_release_id=f'episode:{episode_id}',
                                source_url=episode_url,
                                metadata={'title': child_title, 'season_number': season_no,
                                          'episode_number': episode_no,
                                          'video_subtype': episode_subtype})
                            if (not child_entity or work_action != 'auto_add' or not episode_date or
                                    not (episode_date >= baseline or episode_status == 'upcoming')):
                                continue
                            child_ids = [
                                {'provider': 'tmdb_episode', 'external_id': str(episode_id),
                                 'source_url': episode_url}, *position_ids]
                            child_metadata = {
                                'sources': metadata.get('sources') or {}, 'entity_type': 'work',
                                'catalog_provider': 'TMDB', 'video_subtype': episode_subtype,
                                'external_ids': child_ids,
                                'episode': {'season': season_no, 'number': episode_no,
                                            'title': episode_title, 'show_id': str(external_id),
                                            'airdate': episode_date},
                            }
                            child_watch_id = add_watch(
                                db, 'video_episode', child_title, 'Unknown', episode_url,
                                episode_date if episode_status == 'upcoming' else None,
                                child_metadata, parent_id=int(watch['id']))
                            if episode_status == 'upcoming':
                                queue_watch_search(db, child_watch_id, child_title, 'Unknown',
                                                   episode_date, upcoming=True,
                                                   publication_status='upcoming')
            elif provider == 'tvmaze':
                show_id = int(external_id)
                show = fetch_tvmaze_show(show_id)
                update_root('TVMaze', show.get('name'), _date(show.get('premiered')), show.get('status'),
                            {'tvmaze_details': {
                                'premiered': show.get('premiered'), 'ended': show.get('ended'),
                                'schedule': show.get('schedule') or {}, 'genres': show.get('genres') or [],
                                'network': (show.get('network') or {}).get('name'),
                                'web_channel': (show.get('webChannel') or {}).get('name'),
                            }}, show.get('url'))
                # TVMaze also tracks episodic movies/specials when a record is in
                # the show catalog. Keep release data in graph history, not as
                # thousands of independent Watched rows unless policy opts in.
                seasons = list_tvmaze_seasons(show_id)
                season_action = relationship_watch_action(policy, 'new_seasons', 0.9)
                for season in seasons:
                    season_id = season.get('id')
                    if not season_id or season_action == 'ignore':
                        continue
                    first_date = _date(season.get('premiereDate'))
                    _link_catalog_child(
                        db, entity_id, 'season', 'video',
                        f"{show.get('name')} Season {season.get('number') or ''}".strip(),
                        'tvmaze_season', season_id,
                        f'https://www.tvmaze.com/seasons/{season_id}',
                        {'season_number': season.get('number'), 'release_date': first_date,
                         'status': 'released' if first_date and first_date <= dt.date.today().isoformat() else 'upcoming'},
                        relationship='contains',
                        inverse_relationship='season_of',
                        raw_relationship_type='seasons',
                        status='accepted' if season_action == 'auto_add' else 'candidate')
                episodes = list_tvmaze_episodes(show_id, include_specials=True)
                baseline = metadata.get('episodes_baselined_at') or str(watch.get('created_at') or '')[:10]
                baseline = baseline or dt.date.today().isoformat()
                if not metadata.get('episodes_baselined_at'):
                    metadata['episodes_baselined_at'] = dt.date.today().isoformat()
                episode_action = relationship_watch_action(policy, 'episodes', 0.9)
                special_action = (configured_special_action if specials_override
                                  else episode_action)
                for episode in episodes:
                    episode_id = episode.get('id')
                    if not episode_id:
                        continue
                    airdate = _date(episode.get('airdate'))
                    episode_url = episode.get('url')
                    episode_status = ('upcoming' if airdate and airdate > dt.date.today().isoformat()
                                      else 'released')
                    episode_subtype = ('special' if str(episode.get('type') or '').casefold() == 'special'
                                       or episode.get('season') == 0 else 'episode')
                    work_action = (special_action if episode_subtype == 'special'
                                   else episode_action)
                    release_observation = upsert_media_release(
                        db, entity_id, 'episode', airdate, status=episode_status,
                        provider='TVMaze', provider_release_id=f'episode:{episode_id}',
                        source_url=episode_url,
                        metadata={'title': episode.get('name'), 'season_number': episode.get('season'),
                                  'episode_number': episode.get('number'), 'type': episode.get('type'),
                                  'video_subtype': episode_subtype})
                    season_no, episode_no = episode.get('season'), episode.get('number')
                    position_id = _episode_position_external_id(entity_id, season_no, episode_no)
                    code = (f"S{int(season_no):02d}E{int(episode_no):02d}"
                            if isinstance(season_no, int) and isinstance(episode_no, int)
                            else f"Episode {episode_id}")
                    child_title = f"{show.get('name')} {code} - {episode.get('name') or ''}".strip(' -')
                    episode_url = episode.get('url')
                    position_ids = [position_id] if position_id else []
                    child_entity = _link_catalog_child(
                        db, entity_id, 'work', 'video', child_title,
                        'tvmaze_episode', episode_id, episode_url,
                        {'entity_type': 'work', 'video_subtype': episode_subtype,
                         'release_date': airdate, 'status': episode_status,
                         'season_number': season_no, 'episode_number': episode_no,
                         'external_ids': position_ids},
                        relationship='contains', confidence=0.94,
                        inverse_relationship=('special_of' if episode_subtype == 'special'
                                              else 'episode_of'),
                        raw_relationship_type='episodes',
                        status=('ignored' if work_action == 'ignore' else
                                'accepted' if work_action == 'auto_add' else 'candidate'))
                    if (child_entity and work_action == 'auto_add' and airdate and
                            (airdate >= baseline or episode_status == 'upcoming')):
                        child_meta = {
                            'sources': metadata.get('sources') or {}, 'entity_type': 'work',
                            'catalog_provider': 'TVMaze', 'video_subtype': episode_subtype,
                            'external_ids': [{'provider': 'tvmaze_episode',
                                              'external_id': str(episode_id),
                                              'source_url': episode_url}, *position_ids],
                            'episode': {'season': season_no, 'number': episode_no,
                                        'title': episode.get('name'), 'show_id': show_id,
                                        'airdate': airdate},
                        }
                        child_id = add_watch(db, 'video_episode', child_title, 'Unknown', episode_url,
                                             airdate if episode_status == 'upcoming' else None,
                                             child_meta, parent_id=int(watch['id']))
                        if episode_status == 'upcoming':
                            queue_watch_search(db, child_id, child_title, 'Unknown', airdate,
                                               upcoming=True, publication_status='upcoming')
            elif provider == 'steam':
                details = fetch_steam_game(external_id)
                date_value = _exact_date((details.get('release_date') or {}).get('date'))
                release_text = str((details.get('release_date') or {}).get('date') or '').casefold()
                status_value = 'upcoming' if (details.get('release_date') or {}).get('coming_soon') else 'released'
                update_root('Steam', details.get('name'), date_value, status_value,
                            {'steam_details': {
                                'type': details.get('type'), 'is_free': details.get('is_free'),
                                'developers': details.get('developers') or [],
                                'publishers': details.get('publishers') or [],
                                'genres': details.get('genres') or [], 'dlc_ids': details.get('dlc') or [],
                                'release_text': release_text,
                            }}, f'https://store.steampowered.com/app/{external_id}/')
                upsert_media_release(
                    db, entity_id, 'platform_launch', date_value, region='US',
                    platform='Steam', status=status_value, provider='Steam',
                    provider_release_id=f'steam-app:{external_id}',
                    source_url=f'https://store.steampowered.com/app/{external_id}/',
                    metadata={'release_text': release_text})
                dlc_action = relationship_watch_action(policy, 'game_dlc', 0.92)
                for dlc_id in (details.get('dlc') or [])[:30]:
                    if not str(dlc_id).isdigit() or dlc_action == 'ignore':
                        continue
                    try:
                        dlc = fetch_steam_game(dlc_id)
                        dlc_data = dlc.get('release_date') or {}
                        dlc_date = _exact_date(dlc_data.get('date'))
                        dlc_status = ('upcoming' if dlc_data.get('coming_soon') else
                                      'released' if dlc_date else 'unknown')
                        dlc_title = str(dlc.get('name') or '').strip()
                        if not dlc_title:
                            continue
                        dlc_url = f'https://store.steampowered.com/app/{int(dlc_id)}/'
                        child_entity = _link_catalog_child(
                            db, entity_id, 'release', 'game', dlc_title, 'steam', dlc_id,
                            dlc_url, {'game_subtype': 'dlc', 'entity_type': 'release',
                                      'release_date': dlc_date, 'status': dlc_status,
                                      'parent_game_id': str(external_id)},
                            relationship='contains', confidence=0.92,
                            status='accepted' if dlc_action == 'auto_add' else 'candidate')
                        if child_entity:
                            upsert_media_release(
                                db, child_entity, 'game_dlc', dlc_date,
                                status=dlc_status, provider='Steam',
                                provider_release_id=str(dlc_id), source_url=dlc_url,
                                metadata={'title': dlc_title,
                                          'parent_game_id': str(external_id)})
                        if not child_entity or dlc_action != 'auto_add':
                            continue
                        child_metadata = {
                            'sources': metadata.get('sources') or {},
                            'entity_type': 'release', 'catalog_provider': 'Steam',
                            'game_subtype': 'dlc', 'catalog_status': dlc_status,
                            'external_ids': [{'provider': 'steam',
                                              'external_id': str(dlc_id),
                                              'source_url': dlc_url}],
                            'parent_game_id': str(external_id),
                        }
                        dlc_watch_id = add_watch(
                            db, 'game', dlc_title, 'Unknown', dlc_url, dlc_date,
                            child_metadata, parent_id=int(watch['id']))
                        if dlc_status == 'upcoming' and dlc_date:
                            queue_watch_search(db, dlc_watch_id, dlc_title, 'Unknown',
                                               dlc_date, upcoming=True,
                                               publication_status='upcoming')
                    except Exception as exc:
                        errors.append(f'Steam DLC {dlc_id}: {exc}')
            elif provider == 'igdb' and igdb_client_id and igdb_client_secret:
                details = fetch_igdb_game(external_id, igdb_client_id, igdb_client_secret)
                try:
                    date_value = dt.datetime.fromtimestamp(
                        int(details.get('first_release_date')), dt.timezone.utc).date().isoformat()
                except (TypeError, ValueError, OSError):
                    date_value = None
                update_root('IGDB', details.get('name'), date_value,
                            'released' if date_value and date_value <= dt.date.today().isoformat() else 'upcoming',
                            {'igdb_details': details}, details.get('url'))
                _save_igdb_platform_releases(
                    db, entity_id, details.get('release_dates'), details.get('url'))
                for franchise in (details.get('franchises') or [])[:50]:
                    if isinstance(franchise, dict) and franchise.get('id') and franchise.get('name'):
                        slug = str(franchise.get('slug') or franchise['id'])
                        _link_catalog_child(
                            db, entity_id, 'franchise', 'game', franchise['name'],
                            'igdb_franchise', franchise['id'],
                            f'https://www.igdb.com/franchises/{slug}',
                        {'game_subtype': 'franchise'}, relationship='part_of',
                        confidence=0.96, status='accepted',
                        raw_relationship_type='franchises')
                collection = details.get('collection')
                if isinstance(collection, dict) and collection.get('id') and collection.get('name'):
                    slug = str(collection.get('slug') or collection['id'])
                    _link_catalog_child(
                        db, entity_id, 'collection', 'game', collection['name'],
                        'igdb_collection', collection['id'],
                        f'https://www.igdb.com/collections/{slug}',
                        {'game_subtype': 'collection'}, relationship='part_of',
                        confidence=0.94, status='accepted',
                        raw_relationship_type='collection')
                version_parent = details.get('version_parent')
                if isinstance(version_parent, dict) and version_parent.get('id') and version_parent.get('name'):
                    version_relation = 'remake_of' if sub_type == 'remake' else 'edition_of'
                    _link_catalog_child(
                        db, entity_id, 'work', 'game', version_parent['name'], 'igdb',
                        version_parent['id'], None,
                        {'game_subtype': 'base_game'}, relationship=version_relation,
                        confidence=0.9, status='candidate',
                        raw_relationship_type='version_parent')
                parent_game = details.get('parent_game')
                if isinstance(parent_game, dict) and parent_game.get('id') and parent_game.get('name'):
                    _link_catalog_child(
                        db, entity_id, 'work', 'game', parent_game['name'], 'igdb',
                        parent_game['id'], None,
                        {'game_subtype': 'base_game'}, relationship='part_of',
                        confidence=0.92, status='accepted',
                        raw_relationship_type='parent_game')
                categories = {
                    'dlcs': ('dlc', 'contains', False),
                    'expansions': ('expansion', 'contains', False),
                    'standalone_expansions': ('standalone_expansion', 'contains', False),
                    'remakes': ('remake', 'remake_of', True),
                    'remasters': ('remaster', 'edition_of', True),
                    'ports': ('port', 'edition_of', True),
                }
                for field, (subtype_value, relationship_type, reverse_relationship) in categories.items():
                    policy_key = ('game_expansions' if subtype_value in
                                  {'expansion', 'standalone_expansion'} else
                                  'game_dlc' if subtype_value == 'dlc' else
                                  'remasters_and_ports')
                    action = relationship_watch_action(policy, policy_key, 0.94)
                    for child in (details.get(field) or [])[:100]:
                        if not isinstance(child, dict) or not child.get('id'):
                            continue
                        if action == 'ignore':
                            continue
                        child_entity_type = 'work' if subtype_value == 'remake' else 'release'
                        child_metadata = {'game_subtype': subtype_value,
                                          'parent_game_id': str(external_id),
                                          'status': 'announced',
                                          'entity_type': child_entity_type}
                        child_entity = _link_catalog_child(
                            db, entity_id,
                            child_entity_type, 'game',
                            child.get('name'), 'igdb', child.get('id'), child.get('url'),
                            child_metadata, relationship=relationship_type,
                            confidence=0.94,
                            status=('accepted' if action == 'auto_add' or reverse_relationship
                                    else 'candidate'),
                            raw_relationship_type=field,
                            relationship_from_child=reverse_relationship)
                        if child_entity and action == 'auto_add':
                            child_watch_meta = {
                                'sources': metadata.get('sources') or {},
                                'entity_type': child_entity_type,
                                'catalog_provider': 'IGDB', 'game_subtype': subtype_value,
                                'external_ids': [{'provider': 'igdb',
                                                  'external_id': str(child['id']),
                                                  'source_url': child.get('url')}],
                                'parent_game_id': str(external_id),
                            }
                            add_watch(db, 'game', child.get('name'), 'Unknown',
                                      child.get('url'), None, child_watch_meta,
                                      parent_id=int(watch['id']))
        except Exception as exc:
            errors.append(f'{provider} {external_id}: {exc}')
            logger.info('Could not refresh %s metadata for %s: %s',
                        provider, watch.get('title'), exc)

    if refreshed:
        canonical = db.execute('''SELECT title,release_date,status,metadata_json FROM media_entities WHERE id=?''',
                               (entity_id,)).fetchone()
        if canonical:
            title = canonical['title'] or title
            if (metadata.get('release_date_source') not in {'manual', 'review'} and
                    canonical['release_date']):
                release_date = canonical['release_date']
            if canonical['status']:
                status = canonical['status']
                metadata['catalog_status'] = status
            try:
                canonical_metadata = json.loads(canonical['metadata_json'] or '{}')
            except (TypeError, ValueError):
                canonical_metadata = {}
            if canonical_metadata.get('status_source') in {'review', 'provider_consensus'}:
                metadata['catalog_status_source'] = canonical_metadata['status_source']
                metadata['canonical_status_observation'] = canonical_metadata.get(
                    'canonical_status_observation')
        metadata['last_graph_refresh_at'] = _now().isoformat(sep='T')
        metadata['catalog_provider'] = catalog_provider
        watch_state = dict(watch)
        watch_state.update({'title': title, 'release_date': release_date,
                            'metadata': json.dumps(metadata, ensure_ascii=False),
                            'status': status})
        db.execute('''UPDATE watched_items SET title=?,release_date=?,metadata=?,last_checked_at=?,
            next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   (title, release_date, watch_state['metadata'], _now().isoformat(sep='T'),
                    _next_day().isoformat(sep='T'), '; '.join(errors[-3:]) or None,
                    watch['id']))
        db.commit()
        watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch['id'],)).fetchone())
        sync_watch_entity(db, int(watch['id']))
        entity_id = int(watch['media_entity_id'])
        # The first refresh is a baseline; subsequent refreshes produce durable diffs.
        capture_graph_snapshot(db, entity_id)
        db.commit()
    elif errors:
        db.execute('''UPDATE watched_items SET last_checked_at=?,next_check_at=?,last_error=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   (_now().isoformat(sep='T'), _next_day().isoformat(sep='T'),
                    '; '.join(errors[-3:]), watch['id']))
        db.commit()

    watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch['id'],)).fetchone())
    metadata = json.loads(watch.get('metadata') or '{}')
    entity_type = str(metadata.get('entity_type') or 'work').casefold()
    subtype = str(metadata.get('video_subtype') or metadata.get('game_subtype') or '').casefold()
    if entity_type in {'series', 'franchise', 'collection', 'season'}:
        db.execute("UPDATE watched_items SET state='watching',last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (watch['id'],))
        db.commit()
        return False
    if watch.get('release_date') and watch['release_date'] > dt.date.today().isoformat():
        queue_watch_search(db, watch['id'], watch['title'], watch.get('author'),
                           watch['release_date'], upcoming=True,
                           publication_status=metadata.get('catalog_status') or 'upcoming')
        db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (_next_day().isoformat(sep='T'), watch['id']))
        db.commit()
        return False
    if str(metadata.get('catalog_status') or '').casefold() == 'upcoming' and not watch.get('release_date'):
        queue_watch_search(db, watch['id'], watch['title'], watch.get('author'),
                           upcoming=True, publication_status='upcoming')
        db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (_next_day().isoformat(sep='T'), watch['id']))
        db.commit()
        return False
    if subtype in {'tv_series', 'web_series', 'miniseries', 'franchise', 'collection'}:
        db.execute("UPDATE watched_items SET state='watching',last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (watch['id'],))
        db.commit()
        return False
    return _search_book(db, watch, library_folder, source_overrides)


def _process_goodreads_collection(db, watch, page=None, provider='Goodreads'):
    page = page or fetch_goodreads(watch['source_url'])
    metadata = json.loads(watch.get('metadata') or '{}')
    known = set(metadata.get('known_books') or [])
    current = {book.get('source_url'): book for book in page.get('books', []) if book.get('source_url')}
    sources = metadata.get('sources') or {}
    added = 0
    for url, book in current.items():
        record = book
        # Series/author pages often omit release dates. Fetch a new work's own
        # Goodreads page once so upcoming books can be scheduled accurately.
        if (provider.casefold() == 'goodreads' and url not in known and
                (not book.get('release_date') or not book.get('author') or
                 str(book.get('author')).casefold() == 'unknown')):
            try:
                detail = fetch_goodreads(url)
                record = {**book, **{key: value for key, value in detail.items()
                                     if value not in (None, '', 'Unknown')}}
            except Exception as exc:
                logger.info('Goodreads book detail lookup failed for %s; using collection metadata: %s',
                            url, exc)
        fallback_author = (watch['title'] if watch['kind'] == 'author' else
                           'Unknown' if watch['kind'] == 'book_list' else watch.get('author'))
        book_author = _catalog_author(record.get('author'),
                                      _catalog_author(book.get('author'), fallback_author))
        _, created = _add_catalog_book_watch(
            db, {**record, 'source_url': record.get('source_url') or url},
            book_author, sources, watch['id'], provider)
        added += int(created)
    if watch['kind'] in {'series', 'author'}:
        # Let catalog IDs flow through watch-level identity matching first. The
        # graph sync can then attach alternate-format records to the same work.
        graph_members = ({**book, 'provider': book.get('provider') or provider}
                         for book in current.values())
        _sync_series_members_graph(db, watch, graph_members)
    metadata.pop('goodreads_baseline_pending', None)
    metadata['known_books'] = sorted(known | set(current))
    if page.get('collection_error'):
        metadata['last_collection_error'] = page['collection_error']
    else:
        metadata.pop('last_collection_error', None)
    now = _now()
    db.execute("UPDATE watched_items SET metadata=?,last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(metadata, ensure_ascii=False), now.isoformat(sep='T'),
                _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    graph_entity = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                              (watch['id'],)).fetchone()
    if graph_entity and graph_entity['media_entity_id']:
        capture_graph_snapshot(db, int(graph_entity['media_entity_id']))
        db.commit()
    return added


def _process_openlibrary_author_collection(db, watch):
    """Import all available pages of an Open Library author bibliography."""
    parts = urllib.parse.urlsplit(watch.get('source_url') or '')
    match = re.fullmatch(r'/authors/(OL\d+A)/?', parts.path, flags=re.I)
    if parts.scheme != 'https' or parts.hostname != 'openlibrary.org' or not match:
        raise ValueError('Open Library author watch needs an Open Library author page')
    author_id = match.group(1)
    base_url = f'https://openlibrary.org/authors/{author_id}/works.json'

    def get_page(offset):
        url = base_url + '?' + urllib.parse.urlencode({'limit': 100, 'offset': offset})
        raw, final = http_get(url, max_bytes=4_000_000)
        if urllib.parse.urlsplit(final).hostname != 'openlibrary.org':
            raise ValueError('Open Library author works redirected away from its own site')
        payload = json.loads(raw.decode('utf-8'))
        return payload.get('entries') or [], payload

    entries, first_payload = get_page(0)
    watch_metadata = json.loads(watch.get('metadata') or '{}')
    catalog_profile = watch_metadata.get('catalog_metadata') or {}
    try:
        profile_count = int(catalog_profile.get('work_count') or 0)
    except (TypeError, ValueError):
        profile_count = 0
    total = max(len(entries), int(first_payload.get('size') or 0), profile_count)
    offsets = list(range(100, min(total, 5000), 100))
    page_errors = []
    if offsets:
        def safe_page(offset):
            try:
                page_entries, payload = get_page(offset)
                return offset, page_entries, payload, None
            except Exception as exc:
                return offset, [], {}, str(exc)
        with ThreadPoolExecutor(max_workers=min(8, len(offsets))) as pool:
            pages = list(pool.map(safe_page, offsets))
        for _offset, page_entries, _payload, error in pages:
            entries.extend(page_entries)
            if error:
                page_errors.append(error)
    current = {}
    for entry in entries:
        key = str(entry.get('key') or '')
        title = str(entry.get('title') or '').strip()
        if not title or not re.fullmatch(r'/works/OL\d+W', key):
            continue
        source_url = 'https://openlibrary.org' + key
        current[source_url] = {
            'title': title, 'author': watch.get('title') or 'Unknown',
            'source_url': source_url, 'release_date': _date(entry.get('first_publish_date')),
        }

    metadata = json.loads(watch.get('metadata') or '{}')
    known = set(metadata.get('known_books') or [])
    sources = metadata.get('sources') or {}
    metadata.pop('openlibrary_baseline_pending', None)
    added = 0
    for source_url, book in current.items():
        _, created = _add_catalog_book_watch(db, book, book['author'], sources,
                                             watch['id'], 'Open Library')
        added += int(created)
    _sync_series_members_graph(
        db, watch,
        ({**book, 'provider': 'Open Library'} for book in current.values()))
    metadata['known_books'] = sorted(known | set(current))
    if page_errors:
        metadata['openlibrary_page_errors'] = page_errors[:20]
    else:
        metadata.pop('openlibrary_page_errors', None)
    now = _now()
    db.execute("UPDATE watched_items SET metadata=?,last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (json.dumps(metadata, ensure_ascii=False), now.isoformat(sep='T'),
                _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    graph_entity = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                              (watch['id'],)).fetchone()
    if graph_entity and graph_entity['media_entity_id']:
        capture_graph_snapshot(db, int(graph_entity['media_entity_id']))
        db.commit()
    return added


def _process_openlibrary_series_collection(db, watch):
    metadata = json.loads(watch.get('metadata') or '{}')
    matches = search_openlibrary_metadata(watch['title'], limit=30)
    series = next((item for item in matches
                   if item.get('kind') == 'series' and
                   _normalized(item.get('title')) == _normalized(watch['title'])), None)
    books = (series or {}).get('books') or []
    known = set(metadata.get('known_books') or [])
    added = 0
    for book in books:
        _, created = _add_catalog_book_watch(db, book, book.get('author') or watch.get('author'),
                                             metadata.get('sources') or {}, watch['id'],
                                             'Open Library')
        added += int(created)
    metadata['known_books'] = sorted(known | {
        book.get('source_url') for book in books if book.get('source_url')})
    now = _now()
    db.execute('''UPDATE watched_items SET metadata=?,last_checked_at=?,next_check_at=?,
        last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (json.dumps(metadata, ensure_ascii=False), now.isoformat(sep='T'),
         _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    graph_entity = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                              (watch['id'],)).fetchone()
    if graph_entity and graph_entity['media_entity_id']:
        capture_graph_snapshot(db, int(graph_entity['media_entity_id']))
        db.commit()
    return added


def _process_hardcover_collection(db, watch):
    token = get_local_secret(db, 'hardcover_api_token')
    page = fetch_hardcover_list(watch['source_url'], token)
    return _process_goodreads_collection(db, watch, page=page, provider='Hardcover')


def _process_google_books_series_collection(db, watch):
    """Refresh named Google Books series references without guessing membership."""
    metadata = json.loads(watch.get('metadata') or '{}')
    matches = search_google_books_metadata(watch['title'], limit=40)
    series_key = _normalized(watch['title'])
    series = next((item for item in matches
                   if item.get('kind') == 'series' and
                   _normalized(item.get('title')) == series_key), None)
    books = list((series or {}).get('books') or [])
    if not books:
        # A named series may have only one visible hit in a bounded API result.
        # Accept the work only when that book record explicitly carries the same
        # series name; never infer membership from a title or a series ID alone.
        books = [item for item in matches
                 if item.get('kind') == 'book' and
                 _normalized(item.get('series')) == series_key]
    books = [dict(book, provider=book.get('provider') or 'Google Books')
             for book in books if isinstance(book, dict) and book.get('title')]
    known = set(metadata.get('known_books') or [])
    sources = metadata.get('sources') or {}
    added = 0
    for book in books:
        _, created = _add_catalog_book_watch(
            db, book, book.get('author') or watch.get('author'), sources,
            watch['id'], 'Google Books')
        added += int(created)
    if books:
        _sync_series_members_graph(db, watch, books)
    metadata['known_books'] = sorted(known | {
        book.get('source_url') for book in books if book.get('source_url')})
    now = _now()
    db.execute('''UPDATE watched_items SET metadata=?,last_checked_at=?,next_check_at=?,
        last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (json.dumps(metadata, ensure_ascii=False), now.isoformat(sep='T'),
         _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    graph_entity = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                              (watch['id'],)).fetchone()
    if graph_entity and graph_entity['media_entity_id']:
        capture_graph_snapshot(db, int(graph_entity['media_entity_id']))
        db.commit()
    return added


def _process_wikidata_series_collection(db, watch, qid_override=None,
                                        continuity_path=None):
    metadata = json.loads(watch.get('metadata') or '{}')
    qid = str(qid_override or (metadata.get('catalog_metadata') or {}).get('catalog_id') or '').strip()
    if not qid:
        match = re.search(r'/entity/(Q[1-9]\d{0,11})$', watch.get('source_url') or '')
        qid = match.group(1) if match else ''
    catalog_members = _wikidata_series_members(qid, limit=5000)
    books = [book for book in catalog_members if not book.get('needs_review')]
    review_members = [book for book in catalog_members if book.get('needs_review')]
    known = set(metadata.get('known_books') or [])
    saved_candidates = list(metadata.get('series_review_candidates') or [])
    candidate_keys = {
        str(((candidate.get('catalog_metadata') or {}).get('catalog_id') or
             candidate.get('source_url') or candidate.get('title') or '')).casefold()
        for candidate in saved_candidates if isinstance(candidate, dict)
    }
    series_entity_id = watch.get('media_entity_id') or sync_watch_entity(db, watch['id'])
    continuity_path = list(continuity_path or ([qid] if qid else []))
    if qid:
        try:
            imported = import_wikidata_entity(db, qid, watch_entity_id=series_entity_id)
            if int(imported.get('root_entity_id') or 0) == int(series_entity_id):
                _apply_wikidata_watch_policy(
                    db, watch, imported, continuity_path=continuity_path)
        except Exception as exc:
            logger.info('Wikidata graph refresh failed for series %s: %s',
                        watch.get('title'), exc)
    for candidate in review_members:
        catalog_id = str((candidate.get('catalog_metadata') or {}).get('catalog_id') or '')
        candidate_key = (catalog_id or candidate.get('source_url') or candidate.get('title') or '').casefold()
        if candidate_key not in candidate_keys:
            saved_candidates.append(candidate)
            candidate_keys.add(candidate_key)
            queue_graph_review(
                db, 'uncertain_series_member',
                f"Review possible member of {watch['title']}: {candidate['title']}",
                {'series': watch['title'], 'candidate': candidate,
                 'reason': candidate.get('review_reason')},
                entity_id=series_entity_id,
                dedupe_key=f'series_member_candidate:{watch["id"]}:{candidate_key}')
    if saved_candidates:
        metadata['series_review_candidates'] = saved_candidates
    added = 0
    for book in books:
        _, created = _add_catalog_book_watch(db, book, book.get('author') or watch.get('author'),
                                             metadata.get('sources') or {}, watch['id'],
                                             'Wikidata')
        added += int(created)
    metadata['known_books'] = sorted(known | {
        book.get('source_url') for book in books if book.get('source_url')})
    now = _now()
    db.execute('''UPDATE watched_items SET metadata=?,last_checked_at=?,next_check_at=?,
        last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
        (json.dumps(metadata, ensure_ascii=False), now.isoformat(sep='T'),
         _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    capture_graph_snapshot(db, int(series_entity_id))
    db.commit()
    return added


def _process_alternate_collections(db, watch, primary_provider):
    """Refresh additional book/series metadata catalogs associated with a watch."""
    initial = json.loads(watch.get('metadata') or '{}')
    providers = initial.get('catalog_sources') or {}
    ids_by_provider = initial.get('catalog_ids') or {}
    for provider, source_urls in providers.items():
        provider_key = str(provider).casefold()
        if provider_key == str(primary_provider or '').casefold():
            continue
        current_row = db.execute('SELECT * FROM watched_items WHERE id=?', (watch['id'],)).fetchone()
        if not current_row:
            return
        current = dict(current_row)
        try:
            if provider_key == 'goodreads' and source_urls:
                current['source_url'] = source_urls[0]
                _process_goodreads_collection(db, current)
            elif provider_key == 'open library' and current['kind'] == 'author':
                openlibrary_urls = source_urls or []
                if openlibrary_urls:
                    current['source_url'] = openlibrary_urls[0]
                    _process_openlibrary_author_collection(db, current)
            elif provider_key == 'open library' and current['kind'] == 'series':
                _process_openlibrary_series_collection(db, current)
            elif provider_key == 'google books' and current['kind'] == 'series':
                _process_google_books_series_collection(db, current)
            elif provider_key == 'wikidata':
                qids = ids_by_provider.get(provider) or []
                if not qids:
                    qids = [match.group(1) for url in source_urls
                            if (match := re.search(r'/entity/(Q[1-9]\d{0,11})$', str(url)))]
                for qid in qids:
                    fresh = db.execute('SELECT * FROM watched_items WHERE id=?',
                                       (watch['id'],)).fetchone()
                    if fresh:
                        _process_wikidata_series_collection(db, dict(fresh), qid)
        except Exception as exc:
            logger.info('Alternate %s catalog refresh failed for series %s: %s',
                        provider, watch['title'], exc)


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
    _category, keys = _download_search_plan(watch, options)
    if has_rights_file and not any(_approved_source_record(options, key, watch, db) for key in keys):
        error = 'Local rights record is NO; automatic downloads are paused.'
        db.execute("UPDATE watched_items SET state='rights_hold',last_checked_at=?,next_check_at=NULL,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), error, watch['id']))
        db.commit()
        return 0
    results, connector_errors = _search_candidates(watch, source_overrides, db)
    for message in connector_errors:
        logger.warning('Comic issue watch %s connector error: %s', watch['id'], message)
    requested_number = str((metadata.get('comic_issue') or {}).get('issue_number') or '').strip()
    requested_year = str((metadata.get('catalog_metadata') or {}).get('series_start_year') or
                         (metadata.get('comic_issue') or {}).get('series_start_year') or '')
    candidates = [item for item in results
                      if str(item.get('format') or '').casefold() in ('.cbz', '.cbr', '.pdf')
                      and _is_match(item, watch)
                      and _comic_issue_number_matches(item.get('title'), requested_number)
                      and _comic_issue_run_matches(item, requested_year)]
    if not candidates:
        error = 'No matching downloadable issue file was found from an approved connector.'
        if connector_errors:
            error += ' ' + '; '.join(connector_errors[-2:])
        db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), error, watch['id']))
        db.commit()
        return 0
    saved = None
    for candidate in candidates:
        candidate = dict(candidate)
        for field in ('comic_issue', 'external_ids', 'comic_metadata_provenance', 'catalog_metadata'):
            if metadata.get(field):
                candidate[field] = metadata[field]
        if rights.get('rights_work_key'):
            candidate.setdefault('rights_work_key', rights['rights_work_key'])
        if has_rights_file:
            candidate.setdefault('rights_evidence_path', options['rights_evidence_path'])
        try:
            row = (enqueue_irc(db, candidate['irc'], watch_id=watch['id']) if candidate.get('irc') else
                   enqueue_for_watch(db, candidate, watch['id']))
            saved = download(db, row['id'], Path(library_folder).parent, destination=library_folder)
            break
        except Exception as exc:
            connector_errors.append(f"{candidate.get('file_host') or candidate.get('source_key')}: {exc}")
    if saved is None:
        db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), '; '.join(connector_errors[-3:]), watch['id']))
        db.commit()
        return 0
    db.execute("UPDATE watched_items SET state='downloaded',file_path=?,last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
               (saved.get('file_path'), now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
    db.commit()
    return 1


def _comic_issue_number_matches(title, requested_number):
    if not requested_number:
        return True
    from .comic_metadata import normalize, issue_number
    record = normalize({'title': str(title or '')})
    # A trade, multi-issue pack, or unnumbered file must not satisfy an
    # individual issue watch merely because its title resembles the series.
    return (record['comic_entity'] == 'issue' and
            record['issue_number'] == issue_number(requested_number))


def _comic_issue_run_matches(candidate, requested_year):
    if not requested_year:
        return True
    from .comic_metadata import normalize
    candidate_year = normalize(candidate)['series_start_year']
    return not candidate_year or candidate_year == str(requested_year)


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


def _download_candidate_key(item):
    if item.get('url'):
        return str(item['url'])
    config = item.get('irc') or {}
    if config:
        token = config.get('request') or config.get('pack') or ''
        return 'irc|' + '|'.join(str(value or '') for value in (
            config.get('server'), config.get('channel'), config.get('bot'), token,
            item.get('format')))
    return ''


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
    seen_releases = set(comic.get('known_releases') or [])
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
        candidates, connector_errors = _search_candidates(watch, source_overrides, db)
    errors.extend(connector_errors)
    for error in connector_errors:
        logger.warning('Comic watch %s connector error: %s', watch['id'], error)
    for item in candidates:
        item_format = str(item.get('format') or '').casefold()
        if item_format not in COMIC_FORMATS and not (
                item_format == '.pdf' and item.get('source_key') in {
                    'public_domain_comic_mirror', 'getcomics.org', 'irc'}):
            continue
        key = _download_candidate_key(item)
        release = item.get('release_identity')
        if not key or key in seen_files or release and release in seen_releases:
            continue
        try:
            row = enqueue_irc(db, item['irc']) if item.get('irc') else enqueue(db, item)
            result = download(db, row['id'], Path(library_folder).parent,
                              destination=library_folder)
            seen_files.add(key)
            if release:
                seen_releases.add(release)
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
    comic['known_releases'] = sorted(seen_releases)[-1000:]
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
    feed_host = (urllib.parse.urlsplit(feed_url).hostname or '').casefold() if feed_url else ''
    chapter_source_key = ('novelarchive.cc' if feed_host.endswith('novelarchive.cc')
                          else 'webfiction_rss_atom') if feed_url else None
    chapter_approval = (_approved_source_record(options, chapter_source_key, watch, db)
                        if chapter_source_key else None)
    errors = []
    manifest = {'chapters': []}
    if feed_url and not chapter_approval:
        errors.append(f'{chapter_source_key} download source is NO in rights-evidence.json.')
    elif chapter_source_key == 'novelarchive.cc':
        manifest = discover_novelarchive_chapters(feed_url, limit=200)
    elif feed_url:
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
    # Search the configured ebook connectors as well as the chapter feed.
    # Only text-book formats are relevant to this watch type.
    candidates, connector_errors = _search_candidates(watch, source_overrides, db)
    errors.extend(connector_errors)
    for error in connector_errors:
        logger.warning('Web fiction watch %s connector error: %s', watch['id'], error)
    for item in candidates:
        if str(item.get('format') or '').casefold() not in {'.epub', '.pdf', '.mobi', '.azw3'}:
            continue
        key = _download_candidate_key(item)
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


def _process_fanfiction_watch(db, watch, library_folder, source_overrides=None):
    """Update a fic with FanFicFare, falling back to an approved IRC EPUB."""
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
    try:
        row = enqueue(db, item)
        result = download(db, row['id'], Path(library_folder).parent,
                          destination=library_folder)
    except Exception as fanfic_error:
        result = None
        irc_errors = []
        candidates, search_errors = _search_candidates(watch, source_overrides, db)
        irc_errors.extend(search_errors)
        for candidate in candidates:
            if (not candidate.get('irc') or
                    str(candidate.get('format') or '').casefold() != '.epub'):
                continue
            try:
                irc_row = enqueue_irc(db, candidate['irc'])
                result = download(db, irc_row['id'], Path(library_folder).parent,
                                  destination=library_folder)
                break
            except Exception as exc:
                irc_errors.append(f"{candidate.get('title') or 'IRC result'}: {exc}")
        if result is None:
            detail = '; '.join(irc_errors[-2:])
            message = f'FanFicFare: {fanfic_error}'
            if detail:
                message += f'; IRC: {detail}'
            raise RuntimeError(message) from fanfic_error
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
    database_path = db.execute('PRAGMA database_list').fetchone()['file']
    book_pool = ThreadPoolExecutor(max_workers=4) if database_path else None
    book_jobs = []
    for book, release_date in new_release_ids:
        title = str(book.get('title') or f"{watch['title']} volume")
        book_url = f"https://ranobedb.org/book/{book['id']}"
        child_id = add_watch(
            db, 'book', title, watch.get('author') or 'Unknown', book_url,
            release_date, {'sources': sources, 'lightnovel_release': {
                'ranobedb_series_id': str(series_id), 'ranobedb_book_id': str(book['id']),
                'english_official': True,
            }}, watch['id'])
        try:
            parent_profile_ids = metadata.get('library_profile_ids') or []
            if not isinstance(parent_profile_ids, list):
                parent_profile_ids = [parent_profile_ids]
            for profile_id in parent_profile_ids:
                assign_watch_profile(db, child_id, profile_id)
        except Exception:
            logger.exception('Could not apply library profile to light-novel volume %s', title)
        child = db.execute('SELECT * FROM watched_items WHERE id=?', (child_id,)).fetchone()
        if child and child['state'] != 'downloaded' and release_date <= today.isoformat():
            if book_pool:
                book_jobs.append(book_pool.submit(
                    _search_book_with_own_connection, database_path, dict(child),
                    library_folder, source_overrides))
            else:
                downloaded += int(bool(_search_book(db, dict(child), library_folder,
                                                    source_overrides)))
    if book_pool:
        for future in as_completed(book_jobs):
            try:
                downloaded += int(bool(future.result()))
            except Exception:
                logger.exception('Parallel light-novel release search failed for %s',
                                 watch.get('title'))
        book_pool.shutdown(wait=True)

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
    processed_children = set()
    database_path = db.execute('PRAGMA database_list').fetchone()['file']
    book_pool = ThreadPoolExecutor(max_workers=4) if database_path else None
    book_jobs = []

    def destination_for(watch):
        folders = _profile_library_folders(db, watch, library_folder)
        return folders[0] if folders else library_folder

    def schedule_book_search(watch):
        target_folder = destination_for(watch)
        if book_pool:
            book_jobs.append(book_pool.submit(
                _search_book_with_own_connection, database_path, dict(watch),
                target_folder, source_options))
            # Bound queued work as well as running workers for large libraries.
            if len(book_jobs) >= 8:
                completed, pending = wait(book_jobs, return_when=FIRST_COMPLETED)
                book_jobs[:] = list(pending)
                for future in completed:
                    try:
                        future.result()
                    except Exception:
                        logger.exception('A parallel watched-book search could not be completed')
        else:
            _search_book(db, watch, target_folder, source_options)
            _copy_watch_to_profile_folders(db, int(watch['id']), library_folder)

    for watch in rows:
        if int(watch['id']) in processed_children:
            continue
        video_game_watch = watch['kind'] in {'video', 'game'}
        if (not video_game_watch and watch['kind'] not in ('series', 'author', 'book_list') and
                watch.get('release_date') and watch['release_date'] > today):
            due_at = f"{watch['release_date']}T00:00:00"
            if watch['state'] != 'upcoming' or watch.get('next_check_at') != due_at:
                db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (due_at, watch['id']))
                db.commit()
            continue
        watch_metadata = json.loads(watch.get('metadata') or '{}')
        if (watch['kind'] == 'book' and
                is_unscheduled_announced_member(watch_metadata) and
                not watch.get('release_date')):
            queue_watch_search(db, watch['id'], watch['title'], watch.get('author'),
                               upcoming=True, series=watch_metadata.get('series'),
                               series_index=watch_metadata.get('series_index'),
                               publication_status=watch_metadata.get('publication_status'))
            db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (_next_day(now).isoformat(sep='T'), watch['id']))
            db.commit()
            continue
        next_check = watch.get('next_check_at')
        if not force and next_check and next_check > now.isoformat(sep='T'):
            continue
        checked += 1
        if watch['kind'] == 'webcomic' and watch_metadata.get('webcomics'):
            from .webcomics.watcher import process_watch as process_webcomic_watch
            process_webcomic_watch(db, watch, destination_for(watch), source_options)
            continue
        if watch['kind'] in {'video', 'game'}:
            try:
                _process_video_game_watch(db, watch, destination_for(watch), source_options)
                _copy_watch_to_profile_folders(db, int(watch['id']), library_folder)
            except Exception as exc:
                logger.exception('Video/game watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] == 'software':
            try:
                _process_software_watch(db, watch, destination_for(watch), source_options)
                _copy_watch_to_profile_folders(db, int(watch['id']), library_folder)
            except Exception as exc:
                logger.exception('Software watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("""UPDATE watched_items SET state='watching',last_checked_at=?,
                    next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'),
                            str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] == 'lightnovel':
            try:
                _process_lightnovel_watch(db, watch, destination_for(watch), source_options)
                for child in db.execute('SELECT id FROM watched_items WHERE parent_id=?',
                                        (watch['id'],)).fetchall():
                    _copy_watch_to_profile_folders(db, int(child['id']), library_folder)
            except Exception as exc:
                logger.exception('Light-novel watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] in ('series', 'author', 'book_list'):
            provider = ''
            try:
                metadata = json.loads(watch.get('metadata') or '{}')
                provider = str(metadata.get('catalog_provider') or '').casefold()
                if watch['kind'] == 'book_list' and provider == 'goodreads':
                    _process_goodreads_collection(db, watch)
                elif watch['kind'] == 'book_list' and provider == 'hardcover':
                    _process_hardcover_collection(db, watch)
                elif watch['kind'] == 'author' and provider == 'open library':
                    _process_openlibrary_author_collection(db, watch)
                elif watch['kind'] == 'series' and provider == 'open library':
                    _process_openlibrary_series_collection(db, watch)
                elif watch['kind'] == 'series' and provider == 'google books':
                    _process_google_books_series_collection(db, watch)
                elif watch['kind'] == 'series' and provider == 'wikidata':
                    _process_wikidata_series_collection(db, watch)
                elif watch.get('source_url') and (urllib.parse.urlsplit(
                        watch['source_url']).hostname or '').casefold().endswith('goodreads.com'):
                    _process_goodreads_collection(db, watch)
                else:
                    db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
                    db.commit()
            except Exception as exc:
                logger.exception('Author or series watch %s (%s) failed',
                                 watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            if watch['kind'] in ('series', 'author'):
                _process_alternate_collections(db, watch, provider)
            # The initial add or this collection scan has now exposed all known
            # works. Search every released child immediately; future releases stay
            # visible in Queue and begin their source search on release day.
            children = [dict(row) for row in db.execute('''SELECT * FROM watched_items
                WHERE parent_id=? AND kind='book' AND state!='downloaded' ORDER BY id''',
                (watch['id'],))]
            for child in children:
                child_id = int(child['id'])
                processed_children.add(child_id)
                checked += 1
                child_metadata = json.loads(child.get('metadata') or '{}')
                if (is_unscheduled_announced_member(child_metadata) and
                        not child.get('release_date')):
                    queue_watch_search(db, child_id, child['title'], child.get('author'),
                                       upcoming=True, series=child_metadata.get('series'),
                                       series_index=child_metadata.get('series_index'),
                                       publication_status=child_metadata.get('publication_status'))
                    db.execute("UPDATE watched_items SET state='upcoming',next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (_next_day(now).isoformat(sep='T'), child_id))
                    db.commit()
                    continue
                if child.get('release_date') and child['release_date'] > today:
                    schedule_book_search(child)
                    continue
                db.execute("UPDATE watched_items SET state='searching',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (child_id,))
                db.commit()
                schedule_book_search(child)
            continue
        if watch['kind'] in SERIAL_KINDS:
            try:
                watch_metadata = json.loads(watch.get('metadata') or '{}')
                if watch['kind'] == 'fanfiction' and watch_metadata.get('fanfiction'):
                    _process_fanfiction_watch(db, watch, destination_for(watch), source_options)
                else:
                    _process_webfiction_watch(db, watch, destination_for(watch), source_options)
                _copy_watch_to_profile_folders(db, int(watch['id']), library_folder)
            except Exception as exc:
                logger.exception('Fanfiction watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        if watch['kind'] in COMIC_KINDS:
            try:
                _process_comic_watch(db, watch, destination_for(watch), source_options)
                _copy_watch_to_profile_folders(db, int(watch['id']), library_folder)
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
                _process_comic_issue_watch(db, watch, destination_for(watch), source_options)
                _copy_watch_to_profile_folders(db, int(watch['id']), library_folder)
            except Exception as exc:
                logger.exception('Comic issue watch %s (%s) failed', watch['id'], watch['title'])
                db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                           (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), str(exc), watch['id']))
                db.commit()
            continue
        db.execute("UPDATE watched_items SET state='searching',last_checked_at=?,next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (now.isoformat(sep='T'), _next_day(now).isoformat(sep='T'), watch['id']))
        db.commit()
        schedule_book_search(watch)
    if book_pool:
        for future in as_completed(book_jobs):
            try:
                future.result()
            except Exception:
                logger.exception('A parallel watched-book search could not be completed')
        book_pool.shutdown(wait=True)
    return checked
