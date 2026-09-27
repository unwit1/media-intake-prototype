"""Category-first catalog and source searches for the Add Anything dashboard."""
import json
import logging
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .comics import search_anilist, search_comicvine, search_mangadex, search_taddy
from .core import (AUDIO_FORMATS, BOOK_FORMATS, COMIC_FORMATS, EXTENSIONS,
                   GAME_FORMATS, VIDEO_FORMATS)
from .fanfiction import story_url_key
from .media_catalogs import (resolve_catalog_file_links, search_lnori,
                             search_mangaupdates, search_novelarchive,
                             search_ranobedb)
from .sources import search_openlibrary_metadata
from .watched import (_approved_source_record, _search_candidates,
                      get_setting, search_goodreads, set_setting)
from .wikidata import search_wikidata


logger = logging.getLogger(__name__)

CATEGORIES = (
    'Books', 'Comics', 'Manga', 'Anime', 'Videos', 'Games',
    'Light novels', 'Web serials', 'Fanfiction',
)

METADATA_CONNECTORS = {
    'Books': ('Goodreads', 'Open Library'),
    'Comics': ('Comic Vine', 'Taddy'),
    'Manga': ('AniList', 'MangaDex', 'MangaUpdates', 'Taddy'),
    'Anime': ('AniList', 'Wikidata'),
    'Videos': ('Wikidata',),
    'Games': ('Wikidata',),
    'Light novels': ('RanobeDB', 'LNORI', 'NovelArchive', 'Open Library'),
    'Web serials': ('NovelArchive', 'LNORI', 'RanobeDB'),
    'Fanfiction': (),
}

# These are sources that can return or fetch files. Metadata-only catalogs are
# deliberately excluded (for example, Open Library, Goodreads, AniList, and
# Comic Vine). Optional local sources appear in Settings only when configured.
DOWNLOAD_CONNECTORS = {
    'Books': ('LibGen', "Anna's Archive", 'LNORI', 'NovelArchive',
              'OPDS feeds', 'JSON catalogs', 'IRC'),
    'Comics': ('GetComics', 'Public-domain comic mirror', 'Taddy',
               'Comic RSS / Atom', 'IRC'),
    'Manga': ('MangaDex', 'Taddy', 'IRC'),
    'Anime': ('IRC',),
    'Videos': ('IRC',),
    'Games': ('IRC',),
    'Light novels': ('LNORI', 'NovelArchive', 'LibGen', "Anna's Archive",
                     'OPDS feeds', 'JSON catalogs', 'IRC'),
    'Web serials': ('LNORI', 'NovelArchive', 'RSS / Atom feeds', 'IRC'),
    'Fanfiction': ('FanFicFare', 'IRC'),
}

CATEGORY_CONNECTORS = {
    category: tuple(dict.fromkeys(METADATA_CONNECTORS.get(category, ()) +
                                  DOWNLOAD_CONNECTORS.get(category, ())))
    for category in CATEGORIES
}

SOURCE_KEY_LABELS = {
    'annas_archive': "Anna's Archive", 'comic_rss_atom': 'Comic RSS / Atom',
    'direct_file_url': 'Direct file URL', 'getcomics.org': 'GetComics',
    'irc': 'IRC', 'json_catalogs': 'JSON catalogs', 'libgen': 'LibGen',
    'lnori.com': 'LNORI', 'mangadex': 'MangaDex',
    'novelarchive.cc': 'NovelArchive', 'openlibrary': 'Open Library',
    'opds_feeds': 'OPDS feeds',
    'public_domain_comic_mirror': 'Public-domain comic mirror',
    'taddy': 'Taddy', 'webfiction_rss_atom': 'RSS / Atom feeds',
    'fanficfare': 'FanFicFare',
}

_PROVIDER_LABELS = {
    'anilist': 'AniList', 'anna\'s archive': "Anna's Archive", 'annas_archive': "Anna's Archive",
    'comic vine': 'Comic Vine', 'getcomics': 'GetComics', 'goodreads': 'Goodreads',
    'irc': 'IRC', 'json catalogs': 'JSON catalogs', 'libgen': 'LibGen', 'lnori': 'LNORI',
    'mangadex': 'MangaDex', 'mangaupdates': 'MangaUpdates',
    'novelarchive': 'NovelArchive', 'novelarchive metadata': 'NovelArchive',
    'novelarchive download source': 'NovelArchive', 'open library': 'Open Library',
    'openlibrary': 'Open Library', 'ranobedb': 'RanobeDB',
    'public-domain comic mirror': 'Public-domain comic mirror',
    'taddy': 'Taddy', 'wikidata': 'Wikidata', 'fanficfare': 'FanFicFare',
    'fanficfare story url': 'FanFicFare',
    'fanficfare story': 'FanFicFare',
}

_DOWNLOAD_FORMATS = EXTENSIONS
_CATEGORY_FORMATS = {
    'Books': BOOK_FORMATS | AUDIO_FORMATS,
    'Comics': COMIC_FORMATS | {'.pdf'},
    'Manga': COMIC_FORMATS | {'.pdf'},
    'Anime': VIDEO_FORMATS,
    'Videos': VIDEO_FORMATS,
    'Games': GAME_FORMATS | {'.zip', '.rar', '.7z'},
    'Light novels': BOOK_FORMATS | AUDIO_FORMATS,
    'Web serials': BOOK_FORMATS,
    'Fanfiction': BOOK_FORMATS,
}
_BOOK_FORMAT_ORDER = {
    '.epub': 0, '.pdf': 1, '.azw3': 2, '.mobi': 3, '.txt': 4,
    '.m4b': 5, '.mp3': 6, '.cbz': 7, '.cbr': 8,
}
_PRIORITY_SETTING_PREFIX = 'download_priority:'


def connected_download_sources(category, source_options=None):
    """Return supported download connectors that are configured for a category."""
    options = source_options or {}
    sources = list(DOWNLOAD_CONNECTORS.get(category, ()))
    if not options.get('opds_feeds'):
        sources = [source for source in sources if source != 'OPDS feeds']
    if not options.get('catalogs'):
        sources = [source for source in sources if source != 'JSON catalogs']
    from .irc import has_configured_source
    if not has_configured_source(options, category):
        sources = [source for source in sources if source != 'IRC']
    if not options.get('comic_mirror_base'):
        sources = [source for source in sources if source != 'Public-domain comic mirror']
    if options.get('libgen') is False:
        sources = [source for source in sources if source != 'LibGen']
    if options.get('annas') is False:
        sources = [source for source in sources if source != "Anna's Archive"]
    if not options.get('taddy_configured'):
        sources = [source for source in sources if source != 'Taddy']
    if not options.get('comic_rss_configured'):
        sources = [source for source in sources if source != 'Comic RSS / Atom']
    if not options.get('webfiction_rss_configured'):
        sources = [source for source in sources if source != 'RSS / Atom feeds']
    return sources


def _irc_files(query, category, source_options):
    """Search configured IRC indexes for file types relevant to one category."""
    options = dict(source_options or {})
    from .irc import has_configured_source, search_configured_sources
    if not has_configured_source(options, category):
        return [], []
    if not _approved_source_record(options, 'irc'):
        return [], ['IRC download source is NO in rights-evidence.json.']

    kinds = {
        'Books': 'book', 'Comics': 'comic', 'Manga': 'manga', 'Anime': 'anime',
        'Videos': 'video', 'Games': 'game', 'Light novels': 'lightnovel',
        'Web serials': 'webfiction', 'Fanfiction': 'fanfiction',
    }
    allowed = _CATEGORY_FORMATS.get(category, set())
    results, errors = search_configured_sources(query, options, category, allowed, limit=100)
    for item in results:
        item.update(provider='IRC', source_key='irc', kind=kinds.get(category, 'book'))
        if options.get('rights_evidence_path'):
            item['rights_evidence_path'] = options['rights_evidence_path']
        if item.get('irc') is not None:
            item['irc'].update(source_key='irc',
                               rights_evidence_path=options.get('rights_evidence_path'))
    return results, errors


def get_download_priority(db, category, source_options=None):
    available = connected_download_sources(category, source_options)
    try:
        saved = json.loads(get_setting(db, _PRIORITY_SETTING_PREFIX + category, '[]'))
    except (TypeError, ValueError):
        saved = []
    if not isinstance(saved, list):
        saved = []
    ordered = [source for source in saved if source in available]
    return ordered + [source for source in available if source not in ordered]


def save_download_priority(db, category, ordered_sources, source_options=None):
    available = connected_download_sources(category, source_options)
    requested = [source for source in ordered_sources if source in available]
    if len(requested) != len(available) or set(requested) != set(available):
        raise ValueError('Priority order must contain each connected download source exactly once')
    hidden_saved = []
    try:
        saved = json.loads(get_setting(db, _PRIORITY_SETTING_PREFIX + category, '[]'))
        if isinstance(saved, list):
            hidden_saved = [source for source in saved if source not in available]
    except (TypeError, ValueError):
        pass
    set_setting(db, _PRIORITY_SETTING_PREFIX + category,
                json.dumps(requested + hidden_saved, ensure_ascii=False))


def _provider_label(item, fallback=None):
    source_key = str(item.get('source_key') or '').casefold()
    if source_key in SOURCE_KEY_LABELS:
        return SOURCE_KEY_LABELS[source_key]
    raw = str(item.get('provider') or fallback or item.get('site') or 'Catalog').strip()
    return _PROVIDER_LABELS.get(raw.casefold(), raw)


def _catalog_rows(items, category, provider=None):
    rows = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item['provider'] = _provider_label(item, provider)
        item['source'] = str(item.get('source') or item['provider'])
        item['url'] = item.get('url') or item.get('source_url') or ''
        item['source_url'] = item.get('source_url') or item['url']
        item['category'] = category
        item_format = str(item.get('format') or '').casefold()
        downloadable = item_format in _DOWNLOAD_FORMATS and item.get('discovery_only') is not True
        item['source_role'] = 'download' if downloadable else 'metadata'
        item['downloadable'] = downloadable
        if not downloadable:
            if item_format and item_format not in {'link', 'catalog'}:
                item['media_format'] = item.get('format')
            item['format'] = 'catalog'
            item['discovery_only'] = True
            item['url'] = item.get('source_url') or item.get('url') or ''
        else:
            item['discovery_only'] = False
        role = 'Download' if downloadable else 'Metadata'
        item['display_source'] = f'{role} · {item["provider"]}'
        rows.append(item)
    return rows


def _title_key(item):
    text = unicodedata.normalize('NFKD', str(item.get('title') or '').casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def _download_sort_key(item, category, priority):
    source = str(item.get('source_key') or item.get('provider') or '').casefold()
    source_label = SOURCE_KEY_LABELS.get(source, _PROVIDER_LABELS.get(source, source))
    source_rank = {name.casefold(): index for index, name in enumerate(priority)}
    rank = source_rank.get(source_label.casefold(), len(source_rank) + 1)
    item_format = str(item.get('format') or '').casefold()
    format_rank = _BOOK_FORMAT_ORDER.get(item_format, len(_BOOK_FORMAT_ORDER))
    if category in {'Books', 'Light novels'}:
        return (format_rank, rank, item.get('title', '').casefold(), item.get('url', '').casefold())
    return (rank, format_rank, item.get('title', '').casefold(), item.get('url', '').casefold())


def _merge_same_title(results, category, download_priority):
    groups = {}
    for item in results:
        title = _title_key(item) or str(item.get('url') or item.get('source_url') or item.get('provider') or '')
        groups.setdefault(title, []).append(item)

    merged = []
    for items in groups.values():
        metadata, downloads = [], []
        metadata_seen, download_seen = set(), set()
        for item in items:
            if item.get('source_role') == 'download':
                irc = item.get('irc') or {}
                identity = item.get('url') or '|'.join(str(irc.get(field) or '') for field in
                    ('server', 'channel', 'bot', 'request', 'pack'))
                key = (str(identity).casefold(), str(item.get('format') or '').casefold())
                if key not in download_seen:
                    download_seen.add(key)
                    downloads.append(item)
            else:
                key = (str(item.get('provider') or '').casefold(),
                       str(item.get('source_url') or item.get('url') or '').casefold())
                if key not in metadata_seen:
                    metadata_seen.add(key)
                    metadata.append(item)

        downloads.sort(key=lambda item: _download_sort_key(item, category, download_priority))
        chosen = dict(downloads[0] if downloads else metadata[0])
        metadata_sources = list(dict.fromkeys(item.get('provider') for item in metadata if item.get('provider')))
        chosen['metadata_variants'] = [dict(item) for item in metadata]
        chosen['download_variants'] = [dict(item) for item in downloads]
        chosen['metadata_sources'] = metadata_sources
        chosen['available_formats'] = list(dict.fromkeys(
            str(item.get('format') or '').casefold() for item in downloads))
        if downloads:
            chosen['downloadable'] = True
            chosen['source_role'] = 'download'
            chosen['discovery_only'] = False
            chosen['format'] = downloads[0].get('format')
            chosen['url'] = downloads[0].get('url')
            chosen['provider'] = downloads[0].get('provider')
            chosen['source'] = downloads[0].get('source') or downloads[0].get('provider')
            chosen['display_source'] = f'Download · {downloads[0].get("provider") or "Source"}'
            if metadata:
                best_metadata = metadata[0]
                chosen['metadata_url'] = best_metadata.get('source_url') or best_metadata.get('url')
                chosen['metadata_provider'] = best_metadata.get('provider')
                chosen['source_url'] = chosen['metadata_url'] or chosen.get('source_url')
                if best_metadata.get('author') and best_metadata.get('author') != 'Unknown':
                    chosen['author'] = best_metadata['author']
                chosen['catalog_metadata'] = (best_metadata.get('catalog_metadata') or
                                              best_metadata.get('metadata') or {})
        else:
            chosen['downloadable'] = False
            chosen['source_role'] = 'metadata'
            chosen['discovery_only'] = True
            chosen['format'] = 'catalog'
            chosen['url'] = chosen.get('source_url') or chosen.get('url') or ''
            chosen['display_source'] = 'Metadata · ' + ', '.join(metadata_sources or [chosen.get('provider', 'Catalog')])
        chosen['category'] = category
        merged.append(chosen)
    return merged


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
    results, errors = [], []
    for source_key, provider, searcher in (
            ('lnori.com', 'LNORI', search_lnori),
            ('novelarchive.cc', 'NovelArchive', search_novelarchive)):
        approval = _approved_source_record(options, source_key)
        if not approval:
            continue
        try:
            for item in searcher(query, limit=10):
                item.setdefault('provider', provider)
                results.extend(resolve_catalog_file_links(item, approval))
        except Exception as exc:
            errors.append(f'{provider}: {exc}')
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
            ('manga download sources', lambda: _comic_files(query, source_options, 'manga')),
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
            ('IRC', lambda: _irc_files(query, category, source_options)),
        ]
    if category in {'Videos', 'Games'}:
        return [('Wikidata', lambda: _query_wikidata(query)),
                ('IRC', lambda: _irc_files(query, category, source_options))]
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
            ('LNORI metadata', lambda: search_lnori(query)),
            ('RanobeDB metadata', lambda: search_ranobedb(query)),
            ('web serial download sources', lambda: _webserial_files(query, source_options)),
            ('IRC', lambda: _irc_files(query, category, source_options)),
        ]
    if category == 'Fanfiction':
        return [('FanFicFare story URL', lambda: _fanfiction_url(query)),
                ('IRC', lambda: _irc_files(query, category, source_options))]
    raise ValueError(f'Unknown media category: {category}')


def search_media_category(query, category, source_options=None, credentials=None,
                          download_priority=None):
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
                        if category == 'Books':
                            row['kind'] = 'book'
                        elif category == 'Manga':
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
    priority = download_priority or DOWNLOAD_CONNECTORS.get(category, ())
    unique_titles = _merge_same_title(results, category, priority)
    return {'category': category, 'results': unique_titles[:150],
            'errors': errors, 'searched': [provider for provider, _ in tasks]}
