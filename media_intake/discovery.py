"""Category-first catalog and source searches for the Add Anything dashboard."""
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from .comics import search_anilist, search_comicvine, search_mangadex, search_taddy
from .fanfiction import story_url_key
from .media_catalogs import (resolve_catalog_file_links, search_lnori,
                             search_mangaupdates, search_novelarchive,
                             search_ranobedb)
from .sources import search_openlibrary_metadata
from .watched import (_approved_source_record, _search_candidates,
                      search_goodreads)
from .wikidata import search_wikidata


logger = logging.getLogger(__name__)

CATEGORIES = (
    'Books', 'Comics', 'Manga', 'Anime', 'Videos', 'Games',
    'Light novels', 'Web serials', 'Fanfiction',
)

CATEGORY_CONNECTORS = {
    'Books': ('Goodreads', 'Open Library', 'LNORI', 'NovelArchive', 'RanobeDB',
              'LibGen', "Anna's Archive", 'OPDS feeds', 'JSON catalogs', 'IRC'),
    'Comics': ('GetComics', 'Public-domain comic mirror', 'Comic Vine', 'Taddy'),
    'Manga': ('AniList', 'MangaDex', 'MangaUpdates', 'Taddy'),
    'Anime': ('AniList', 'Wikidata'),
    'Videos': ('Wikidata',),
    'Games': ('Wikidata',),
    'Light novels': ('RanobeDB', 'LNORI', 'NovelArchive', 'Open Library', 'LibGen',
                     "Anna's Archive", 'OPDS feeds', 'JSON catalogs', 'IRC'),
    'Web serials': ('NovelArchive', 'LNORI', 'RanobeDB'),
    'Fanfiction': ('FanFicFare story URL lookup',),
}


def _catalog_rows(items, category, provider=None):
    rows = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item['provider'] = str(item.get('provider') or provider or 'Catalog')
        item['source'] = str(item.get('source') or item['provider'])
        item['url'] = item.get('url') or item.get('source_url') or ''
        item['source_url'] = item.get('source_url') or item['url']
        item['category'] = category
        if not item.get('format') or item.get('format') in {'link', 'catalog'}:
            item['format'] = 'catalog'
            item['discovery_only'] = True
        rows.append(item)
    return rows


def _query_wikidata(query):
    results = search_wikidata(query, limit=20)
    for item in results:
        item['description'] = item.get('description') or ''
        item['format'] = 'catalog'
        item['source'] = 'Wikidata'
        item['url'] = item.get('source_url') or ''
        item['discovery_only'] = True
    return results


def _fanfiction_url(query):
    story_url_key(query)
    from urllib.parse import urlsplit
    title = urlsplit(query).path.rstrip('/').rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ')
    return [{'provider': 'FanFicFare', 'kind': 'fanfiction',
             'title': title or 'Fanfiction story', 'author': 'Unknown',
             'source_url': query, 'url': query, 'format': 'catalog',
             'discovery_only': True}]


def _comic_files(query, source_options, kind):
    watch = {'kind': kind, 'title': query, 'author': 'Unknown',
             'metadata': json.dumps({'sources': source_options or {}})}
    return _search_candidates(watch, source_overrides=source_options)


def _webserial_files(query, source_options):
    options = dict(source_options or {})
    approval = _approved_source_record(options, 'novelarchive.cc')
    if not approval:
        return [], []
    results, errors = [], []
    for item in search_novelarchive(query, limit=10):
        try:
            results.extend(resolve_catalog_file_links(item, approval))
        except Exception as exc:
            errors.append(f'NovelArchive: {exc}')
    return results, errors


def _tasks(query, category, source_options, credentials=None):
    credentials = credentials or {}
    if category == 'Books':
        return [
            ('Goodreads', lambda: search_goodreads(query, limit=60)),
            ('Open Library', lambda: search_openlibrary_metadata(query, limit=40)),
            ('book download sources', lambda: _search_candidates(
                {'kind': 'book', 'title': query, 'author': 'Unknown',
                 'metadata': json.dumps({'sources': source_options or {}})}, source_options)),
        ]
    if category == 'Comics':
        tasks = [('comic download sources', lambda: _comic_files(query, source_options, 'comic'))]
        comicvine_key = str(credentials.get('comicvine_api_key') or
                            os.environ.get('COMICVINE_API_KEY', '')).strip()
        if comicvine_key:
            tasks.append(('Comic Vine', lambda: search_comicvine(query, comicvine_key)))
        taddy_id = str(credentials.get('taddy_user_id') or os.environ.get('TADDY_USER_ID', '')).strip()
        taddy_key = str(credentials.get('taddy_api_key') or os.environ.get('TADDY_API_KEY', '')).strip()
        if taddy_id and taddy_key:
            tasks.append(('Taddy', lambda: search_taddy(query, taddy_id, taddy_key)))
        return tasks
    if category == 'Manga':
        tasks = [
            ('AniList', lambda: search_anilist(query)),
            ('MangaDex', lambda: search_mangadex(query)),
            ('MangaUpdates', lambda: search_mangaupdates(query)),
        ]
        taddy_id = str(credentials.get('taddy_user_id') or os.environ.get('TADDY_USER_ID', '')).strip()
        taddy_key = str(credentials.get('taddy_api_key') or os.environ.get('TADDY_API_KEY', '')).strip()
        if taddy_id and taddy_key:
            tasks.append(('Taddy', lambda: search_taddy(query, taddy_id, taddy_key)))
        return tasks
    if category == 'Anime':
        return [
            ('AniList', lambda: search_anilist(query, media_type='ANIME')),
            ('Wikidata', lambda: _query_wikidata(query)),
        ]
    if category in {'Videos', 'Games'}:
        return [('Wikidata', lambda: _query_wikidata(query))]
    if category == 'Light novels':
        return [
            ('RanobeDB', lambda: search_ranobedb(query)),
            ('LNORI', lambda: search_lnori(query)),
            ('NovelArchive', lambda: search_novelarchive(query)),
            ('light-novel download sources', lambda: _search_candidates(
                {'kind': 'lightnovel', 'title': query, 'author': 'Unknown',
                 'metadata': json.dumps({'sources': source_options or {}})}, source_options)),
        ]
    if category == 'Web serials':
        return [
            ('NovelArchive metadata', lambda: search_novelarchive(query)),
            ('NovelArchive download source', lambda: _webserial_files(query, source_options)),
        ]
    if category == 'Fanfiction':
        return [('FanFicFare story URL', lambda: _fanfiction_url(query))]
    raise ValueError(f'Unknown media category: {category}')


def search_media_category(query, category, source_options=None, credentials=None):
    """Search every configured metadata and download connector for one category.

    Returns normalized results plus connector errors so the GUI can show partial
    results without hiding which providers failed.
    """
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    category = str(category or '').strip()
    tasks = _tasks(query, category, source_options or {}, credentials)
    results, errors = [], []
    if not tasks:
        return {'category': category, 'results': [], 'errors': [], 'searched': []}
    with ThreadPoolExecutor(max_workers=min(6, len(tasks))) as pool:
        futures = [(provider, pool.submit(searcher)) for provider, searcher in tasks]
        for provider, future in futures:
            try:
                value = future.result()
                if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], list):
                    found, provider_errors = value
                    errors.extend(f'{provider}: {error}' for error in provider_errors)
                    for error in provider_errors:
                        logger.warning('%s category search failed for %r: %s', provider, query, error)
                else:
                    found, provider_errors = value, []
                for row in _catalog_rows(found, category, provider):
                    if not row.get('kind'):
                        if category == 'Manga':
                            row['kind'] = 'manga'
                        elif category == 'Comics':
                            row['kind'] = 'comic'
                        elif category == 'Light novels':
                            row['kind'] = 'lightnovel'
                        elif category == 'Web serials':
                            row['kind'] = 'webfiction'
                    if category == 'Manga' and row.get('kind') not in {'manga', 'webtoon'}:
                        continue
                    if category == 'Comics' and row.get('kind') not in {
                            None, 'comic', 'comic_issue', 'webcomic', 'webtoon'}:
                        continue
                    results.append(row)
            except Exception as exc:
                logger.warning('%s category search failed for %r: %s', provider, query, exc)
                errors.append(f'{provider}: {exc}')
    deduplicated, seen = [], set()
    for item in results:
        key = (item.get('provider', '').casefold(), item.get('kind', '').casefold(),
               (item.get('url') or item.get('source_url') or '').casefold(),
               item.get('title', '').casefold())
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)
    return {'category': category, 'results': deduplicated[:150],
            'errors': errors, 'searched': [provider for provider, _ in tasks]}
