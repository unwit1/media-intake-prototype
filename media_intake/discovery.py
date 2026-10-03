"""Category-first catalog and source searches for the Add Anything dashboard."""
import json
import difflib
import logging
import os
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .book_identifiers import book_isbns, book_work_title_key
from .comics import search_anilist, search_comicvine, search_mangadex, search_taddy
from .comic_metadata import metadata_tasks as comic_metadata_tasks, group_comics, finish_group, sort_comics
from .core import (AUDIO_FORMATS, BOOK_FORMATS, COMIC_FORMATS, EXTENSIONS,
                   GAME_FORMATS, GAME_PACKAGE_FORMATS, SOFTWARE_FORMATS,
                   VIDEO_FORMATS)
from .download_connectors import (ANIME_DOWNLOAD_ADAPTERS,
                                  GENERAL_DOWNLOAD_ADAPTERS,
                                  resolve_meawfy_downloads,
                                  resolve_site_downloads,
                                  resolve_softarchive_downloads,
                                  formats_for_category,
                                  search_fosshub_foss, search_github_foss,
                                  search_internet_archive, search_meawfy,
                                  search_registered_download_site,
                                  search_softarchive, search_sourceforge_foss)
from .fanfiction import story_url_key
from .media_catalogs import (resolve_catalog_file_links, search_lnori,
                             search_mangaupdates, search_novelarchive,
                             search_ranobedb)
from .media_providers import (search_igdb_games, search_steam_games, steam_base_title,
                              search_tmdb, search_tvmaze)
from .sources import search_google_books_metadata, search_openlibrary_metadata
from .series_continuity import enrich_series_members, series_member_review_reason
from .watched import (_approved_source_record, _search_candidates,
                      _search_wikidata_series, get_setting,
                      search_goodreads, set_setting)
from .wikidata import search_wikidata, video_subtype_from_description


logger = logging.getLogger(__name__)

CATEGORIES = (
    'Books', 'Comics', 'Manga', 'Anime', 'Videos', 'Games', 'Software',
    'Light novels', 'Web serials', 'Fanfiction',
)

METADATA_CONNECTORS = {
    'Books': ('Goodreads', 'Open Library', 'Google Books', 'Wikidata'),
    'Comics': ('Comic Vine', 'Grand Comics Database', 'Metron', 'Wikidata', 'Taddy'),
    'Manga': ('AniList', 'MangaDex', 'MangaUpdates', 'Taddy'),
    'Anime': ('AniList', 'Wikidata'),
    'Videos': ('TMDB', 'TVMaze', 'Wikidata'),
    'Games': ('IGDB', 'Steam', 'Wikidata'),
    'Software': ('FOSShub',),
    'Light novels': ('RanobeDB', 'LNORI', 'NovelArchive', 'Open Library'),
    'Web serials': ('NovelArchive', 'LNORI', 'RanobeDB'),
    'Fanfiction': (),
}

# These are sources that can return or fetch files. Metadata-only catalogs are
# deliberately excluded (for example, Open Library, Goodreads, AniList, and
# Comic Vine). Optional local sources appear in Settings only when configured.
DOWNLOAD_CONNECTORS = {
    'Books': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive', 'LibGen', "Anna's Archive", 'LNORI', 'NovelArchive',
              'OPDS feeds', 'JSON catalogs'),
    'Comics': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive', 'GetComics', 'Public-domain comic mirror', 'Taddy',
               'Comic RSS / Atom'),
    'Manga': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive', 'MangaDex', 'Taddy'),
    'Anime': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive', 'Anime Tosho',
              'Tokyo Insider', 'AnimeOut', 'NoobSubs', 'Chauthanh'),
    'Videos': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive'),
    'Games': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive'),
    'Software': ('IRC', 'GitHub (FOSS)', 'SourceForge'),
    'Light novels': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive', 'LNORI', 'NovelArchive', 'LibGen', "Anna's Archive",
                     'OPDS feeds', 'JSON catalogs'),
    'Web serials': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive', 'LNORI', 'NovelArchive', 'RSS / Atom feeds'),
    'Fanfiction': ('IRC', 'Internet Archive', 'Meawfy', 'SoftArchive', 'FanFicFare'),
}

# These sites expose downloadable files (or queueable XDCC packs). Add them to
# the per-category priority list while keeping the Google CSE browser index out
# of download priorities because it only opens a search page.
for _source_key, _provider, _categories in GENERAL_DOWNLOAD_ADAPTERS:
    for _category in _categories:
        if _provider not in DOWNLOAD_CONNECTORS[_category]:
            DOWNLOAD_CONNECTORS[_category] += (_provider,)

CATEGORY_CONNECTORS = {
    category: tuple(dict.fromkeys(METADATA_CONNECTORS.get(category, ()) +
                                  DOWNLOAD_CONNECTORS.get(category, ())))
    for category in CATEGORIES
}

SOURCE_KEY_LABELS = {
    'annas_archive': "Anna's Archive", 'comic_rss_atom': 'Comic RSS / Atom',
    'direct_file_url': 'Direct file URL', 'getcomics.org': 'GetComics',
    'irc': 'IRC', 'json_catalogs': 'JSON catalogs', 'libgen': 'LibGen',
    'internet_archive': 'Internet Archive',
    'meawfy.com': 'Meawfy',
    'softarchive.download': 'SoftArchive',
    'animetosho.org': 'Anime Tosho', 'tokyoinsider.com': 'Tokyo Insider',
    'animeout.com': 'AnimeOut', 'noobsubs.com': 'NoobSubs',
    'chauthanh.info': 'Chauthanh',
    'github_foss': 'GitHub (FOSS)', 'sourceforge.net': 'SourceForge',
    'fosshub.com': 'FOSShub',
    'mediafiretrend.com': 'MediaFireTrend', 'xdcc.eu': 'XDCC.eu',
    'warezomen.com': 'WarezOmen', 'myabandonware.com': 'MyAbandonware',
    'oldgamesdownload.com': 'OldGamesDownload',
    'abandonwaregames.net': 'AbandonwareGames', 'gog-rev.com': 'GOG Revived',
    'ankergames.net': 'AnkerGames',
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
    'google books': 'Google Books', 'googlebooks': 'Google Books',
    'internet archive': 'Internet Archive', 'internet_archive': 'Internet Archive',
    'anime tos': 'Anime Tosho', 'anime tosho': 'Anime Tosho',
    'tokyo insider': 'Tokyo Insider', 'animeout': 'AnimeOut',
    'noobsubs': 'NoobSubs', 'chauthanh': 'Chauthanh',
    'meawfy': 'Meawfy', 'meawfy.com': 'Meawfy',
    'softarchive': 'SoftArchive', 'softarchive.download': 'SoftArchive',
    'irc': 'IRC', 'json catalogs': 'JSON catalogs', 'libgen': 'LibGen', 'lnori': 'LNORI',
    'mangadex': 'MangaDex', 'mangaupdates': 'MangaUpdates',
    'novelarchive': 'NovelArchive', 'novelarchive metadata': 'NovelArchive',
    'novelarchive download source': 'NovelArchive', 'open library': 'Open Library',
    'openlibrary': 'Open Library', 'ranobedb': 'RanobeDB',
    'public-domain comic mirror': 'Public-domain comic mirror',
    'fosshub': 'FOSShub', 'github (foss)': 'GitHub (FOSS)',
    'github_foss': 'GitHub (FOSS)', 'sourceforge': 'SourceForge',
    'sourceforge.net': 'SourceForge',
    'mediafiretrend.com': 'MediaFireTrend', 'xdcc.eu': 'XDCC.eu',
    'warezomen.com': 'WarezOmen', 'myabandonware.com': 'MyAbandonware',
    'oldgamesdownload.com': 'OldGamesDownload',
    'abandonwaregames.net': 'AbandonwareGames', 'gog-rev.com': 'GOG Revived',
    'ankergames.net': 'AnkerGames',
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
    'Games': GAME_PACKAGE_FORMATS,
    'Software': SOFTWARE_FORMATS,
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
    from .irc import (configured_search_sources, has_configured_source,
                      irc_source_priority_label)
    if not has_configured_source(options, category):
        sources = [source for source in sources if source != 'IRC']
    else:
        irc_index = sources.index('IRC') if 'IRC' in sources else None
        configured = configured_search_sources(options, category)
        irc_sources = [
            irc_source_priority_label(name, config)
            for name, config in configured
        ]
        if irc_index is not None and irc_sources:
            sources[irc_index:irc_index + 1] = irc_sources
        elif irc_index is not None:
            sources.pop(irc_index)
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
        'Software': 'software', 'Web serials': 'webfiction', 'Fanfiction': 'fanfiction',
    }
    allowed = formats_for_category(category)
    priority = options.get('download_priority')
    if not isinstance(priority, (list, tuple)):
        priority = (options.get('download_priorities') or {}).get(category)
    results, errors = search_configured_sources(
        query, options, category, allowed, limit=100, priority=priority)
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
    irc_sources = [source for source in available if source.startswith('IRC: ')]
    ordered_irc = []
    ordered_other = []
    for source in saved:
        if source == 'IRC':
            ordered_irc.extend(item for item in irc_sources if item not in ordered_irc)
        elif source in irc_sources and source not in ordered_irc:
            ordered_irc.append(source)
        elif source in available and source not in ordered_other:
            ordered_other.append(source)
    ordered_irc.extend(source for source in irc_sources if source not in ordered_irc)
    ordered_other.extend(source for source in available
                         if source not in irc_sources and source not in ordered_other)
    # IRC servers are the first source group for every category. Keep saved
    # ordering within that group and within the remaining connectors.
    return ordered_irc + ordered_other


def save_download_priority(db, category, ordered_sources, source_options=None):
    available = connected_download_sources(category, source_options)
    requested = [source for source in ordered_sources if source in available]
    if len(requested) != len(available) or set(requested) != set(available):
        raise ValueError('Priority order must contain each connected download source exactly once')
    irc_sources = [source for source in requested if source.startswith('IRC: ')]
    requested = irc_sources + [source for source in requested if source not in irc_sources]
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
    value = item.get('title') if isinstance(item, dict) else item
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def _download_sort_key(item, category, priority):
    source = str(item.get('source_key') or item.get('provider') or '').casefold()
    source_label = (str(item.get('download_priority_label') or '').strip() or
                    SOURCE_KEY_LABELS.get(source, _PROVIDER_LABELS.get(source, source)))
    source_rank = {name.casefold(): index for index, name in enumerate(priority)}
    rank = source_rank.get(source_label.casefold(), len(source_rank) + 1)
    item_format = str(item.get('format') or '').casefold()
    format_rank = _BOOK_FORMAT_ORDER.get(item_format, len(_BOOK_FORMAT_ORDER))
    if category in {'Books', 'Light novels'}:
        return (format_rank, rank, item.get('title', '').casefold(), item.get('url', '').casefold())
    return (rank, format_rank, item.get('title', '').casefold(), item.get('url', '').casefold())


def _merge_same_title(results, category, download_priority):
    groups = {}
    if category == 'Comics':
        groups = group_comics(results)
    elif category == 'Books':
        book_groups = []
        for item in results:
            kind = str(item.get('kind') or 'book').casefold()
            title = ((book_work_title_key(item.get('title')) if kind == 'book'
                      else _title_key(item)) or
                     str(item.get('url') or item.get('source_url') or item.get('provider') or ''))
            author = _title_key(item.get('author')) or 'unknown'
            isbns = set(book_isbns(item)) if kind == 'book' else set()
            matches = []
            for group in book_groups:
                if group['kind'] != kind:
                    continue
                title_match = title in group['titles']
                isbn_match = bool(isbns.intersection(group['isbns']))
                if not title_match and not isbn_match:
                    continue
                authors = group['authors']
                if author != 'unknown' and authors and author not in authors:
                    continue
                # An unknown-author entry can join only one identifiable work.
                # If catalogs report different authors for the same ISBN/title,
                # keep the rows separate for review instead of hiding the conflict.
                if author == 'unknown' and len(authors) > 1:
                    continue
                matches.append(group)
            if len(matches) == 1:
                group = matches[0]
                group['items'].append(item)
                group['titles'].add(title)
                group['isbns'].update(isbns)
                if author != 'unknown':
                    group['authors'].add(author)
            else:
                book_groups.append({'kind': kind, 'titles': {title}, 'authors':
                                    ({author} if author != 'unknown' else set()),
                                    'isbns': isbns, 'items': [item]})
        groups = [group['items'] for group in book_groups]
    else:
        for item in results:
            title = _title_key(item) or str(item.get('url') or item.get('source_url') or item.get('provider') or '')
            if category in {'Videos', 'Anime'}:
                metadata = item.get('catalog_metadata') or item.get('metadata') or {}
                subtype = str(item.get('video_subtype') or metadata.get('video_subtype') or
                              item.get('kind') or '').casefold()
                subtype = {
                    'anime_movie': 'movie',
                    'anime_series': 'tv_series',
                    'anime_short_series': 'tv_series',
                    'anime_web_series': 'web_series',
                }.get(subtype, subtype)
                # Catalogs disagree on whether a streaming show is a "web" or "TV"
                # series (TVMaze files Netflix shows as web series); it is one show.
                if subtype in {'web_series', 'miniseries'}:
                    subtype = 'tv_series'
                date_value = str(item.get('release_date') or metadata.get('premiere_date') or '')
                year = date_value[:4]
                key = (title, subtype, year)
                if year:
                    undated = (title, subtype, '')
                    if undated in groups:
                        groups[key] = groups.pop(undated) + [item]
                else:
                    matches = [candidate for candidate in groups
                               if isinstance(candidate, tuple) and len(candidate) == 3 and
                               candidate[:2] == (title, subtype)]
                    if len(matches) == 1:
                        key = matches[0]
            elif category == 'Games':
                metadata = item.get('catalog_metadata') or item.get('metadata') or {}
                subtype = str(item.get('game_subtype') or metadata.get('game_subtype') or '').casefold()
                # "X: Enhanced Edition" or "X Legendary Edition" is the same game as X.
                key = (_title_key(steam_base_title(item.get('title'))[0]) or title, subtype)
            else:
                key = title
            groups.setdefault(key, []).append(item)

    merged = []
    grouped_items = groups if isinstance(groups, list) else groups.values()
    for items in grouped_items:
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
        if category == 'Comics':
            chosen = finish_group(chosen, items)
        if category == 'Books' and chosen.get('kind') == 'book':
            catalog = chosen.get('catalog_metadata') or chosen.get('metadata') or {}
            series_values = catalog.get('series') or []
            if isinstance(series_values, str):
                series_values = [series_values]
            if series_values and not chosen.get('series'):
                match = re.search(r'^(.*?)(?:\s+(?:#|book|volume)\s*(\d+(?:\.\d+)?))\s*$',
                                  str(series_values[0]), flags=re.I)
                chosen['series'] = (match.group(1).strip() if match
                                    else str(series_values[0]).strip())
                if match and not chosen.get('series_index'):
                    value = float(match.group(2))
                    chosen['series_index'] = int(value) if value.is_integer() else value
                    chosen['series_position_source'] = 'Open Library series field'
        merged.append(chosen)
    return sort_comics(merged) if category == 'Comics' else merged


def _series_name_key(value):
    words = _title_key({'title': value}).split()
    if words and words[0] in {'a', 'an', 'the'}:
        words = words[1:]
    return ' '.join(words)


def _series_match_score(query, title):
    query_key = _series_name_key(query)
    title_key = _series_name_key(title)
    if not query_key or not title_key:
        return 0.0
    if query_key == title_key:
        return 1.0
    ratio = difflib.SequenceMatcher(None, query_key, title_key).ratio()
    query_words, title_words = set(query_key.split()), set(title_key.split())
    overlap = len(query_words & title_words) / max(1, len(query_words | title_words))
    return max(ratio, overlap * 0.9)


def _title_relevance(query, title):
    """0 exact, 1 prefix, 2 every query word present, 3 fuzzy-only match."""
    query_key, title_key = _series_name_key(query), _series_name_key(title)
    if not query_key or not title_key:
        return 3
    if title_key == query_key:
        return 0
    if title_key.startswith(query_key + ' '):
        return 1
    if set(query_key.split()) <= set(title_key.split()):
        return 2
    return 3


def _sort_by_title_relevance(results, query):
    """Stable sort so catalogs' fuzzy matches ("Cane" for "Arcane") follow real hits."""
    return sorted(results or [], key=lambda row: _title_relevance(query, row.get('title')))


def _promote_matching_series_results(results, query):
    """Place matching series records before work-level hits without reordering peers."""
    series_rows, other_rows = [], []
    for index, row in enumerate(results or []):
        entity_type = str(row.get('entity_type') or '').casefold()
        kind = str(row.get('kind') or '').casefold()
        if kind in {'series', 'franchise', 'continuity'} or entity_type in {
                'series', 'franchise', 'continuity'}:
            score = _series_match_score(query, row.get('title') or row.get('series'))
            # Connector searches can return fuzzy matches; only elevate a series
            # when the query itself substantially identifies that series.
            if score >= 0.60:
                series_rows.append((score, index, row))
                continue
        other_rows.append((index, row))
    series_rows.sort(key=lambda value: (-value[0], value[1]))
    return ([row for _score, _index, row in series_rows] +
            [row for _index, row in other_rows])


def _series_position_key(member):
    value = (member or {}).get('series_index')
    if value in (None, ''):
        value = (member or {}).get('series_position')
    if value in (None, ''):
        return None
    try:
        position = Decimal(str(value).strip())
        return position.normalize() if position.is_finite() else None
    except (InvalidOperation, ValueError):
        return _series_name_key(value) or None


def _merge_series_member(members, incoming, default_provider, review_members=None):
    member = dict(incoming)
    title = str(member.get('title') or '').strip()
    if not title:
        return
    member['provider'] = str(member.get('provider') or default_provider or 'Catalog')

    def evidence(row):
        catalog = row.get('catalog_metadata') or row.get('metadata') or {}
        return {
            'provider': row.get('provider') or member['provider'],
            'title': row.get('title'), 'author': row.get('author'),
            'source_url': row.get('source_url') or row.get('url'),
            'release_date': row.get('release_date'),
            'publication_status': row.get('publication_status'),
            'series_index': row.get('series_index'),
            'series_position': row.get('series_position'),
            'series_position_source': row.get('series_position_source'),
            'catalog_metadata': dict(catalog) if isinstance(catalog, dict) else {},
        }

    incoming_evidence = evidence(member)
    title_key = _series_name_key(book_work_title_key(title))
    author_key = _title_key({'title': member.get('author')}) or 'unknown'
    incoming_isbns = set(book_isbns(member))

    def compatible_author(row):
        row_author = _title_key({'title': row.get('author')}) or 'unknown'
        return (row_author in {'', 'unknown'} or author_key == 'unknown' or
                row_author == author_key)

    existing = next((row for row in members
                     if compatible_author(row) and (
                         _series_name_key(book_work_title_key(row.get('title'))) == title_key or
                         bool(incoming_isbns.intersection(book_isbns(row))))), None)
    if existing is None and review_members is not None and author_key != 'unknown':
        position_key = _series_position_key(member)
        if position_key is not None:
            position_conflict = next((row for row in members
                                      if (_title_key({'title': row.get('author')}) or 'unknown') ==
                                      author_key and
                                      _series_position_key(row) == position_key), None)
            if position_conflict:
                candidate = dict(member)
                candidate['needs_review'] = True
                candidate['review_reason'] = (
                    f"This title differs from '{position_conflict.get('title')}', but "
                    'the author and series position match another catalog entry.')
                candidate['identity_conflict'] = {
                    'title': position_conflict.get('title'),
                    'author': position_conflict.get('author'),
                    'series_index': position_conflict.get('series_index'),
                    'series_position': position_conflict.get('series_position'),
                    'provider': position_conflict.get('provider'),
                    'source_url': position_conflict.get('source_url'),
                    'metadata_variants': list(position_conflict.get('metadata_variants') or []),
                }
                _merge_series_member(review_members, candidate, member['provider'])
                return
    if existing is None:
        member.setdefault('metadata_sources', [member['provider']])
        member['metadata_variants'] = [incoming_evidence]
        member.setdefault('metadata_conflicts', [])
        member.setdefault('catalog_sources', {})
        member.setdefault('catalog_ids', {})
        source_url = member.get('source_url') or member.get('url')
        if source_url:
            member['catalog_sources'].setdefault(member['provider'], []).append(source_url)
        catalog = member.get('catalog_metadata') or member.get('metadata') or {}
        catalog_id = str(catalog.get('catalog_id') or catalog.get('qid') or '').strip()
        if catalog_id:
            member['catalog_ids'].setdefault(member['provider'], []).append(catalog_id)
        members.append(member)
        return

    variants = existing.setdefault('metadata_variants', [
        evidence(existing)])
    if not any(value.get('provider') == incoming_evidence['provider'] and
               value.get('source_url') == incoming_evidence['source_url'] and
               value.get('catalog_metadata') == incoming_evidence['catalog_metadata']
               for value in variants):
        variants.append(incoming_evidence)
    conflicts = existing.setdefault('metadata_conflicts', [])
    for field in ('author', 'release_date', 'series_index'):
        old_value, new_value = existing.get(field), member.get(field)
        if field == 'author' and any(
                _title_key({'title': value}) in {'', 'unknown'}
                for value in (old_value, new_value)):
            continue
        if old_value in (None, '') or new_value in (None, '') or str(old_value) == str(new_value):
            continue
        conflict = {
            'field': field,
            'values': [
                {'value': old_value, 'provider': existing.get('provider')},
                {'value': new_value, 'provider': member.get('provider')},
            ],
        }
        if not any(value.get('field') == field and value.get('values') == conflict['values']
                   for value in conflicts):
            conflicts.append(conflict)

    if (not existing.get('author') or _title_key({'title': existing.get('author')}) == 'unknown') and member.get('author'):
        existing['author'] = member['author']
    if not existing.get('release_date') and member.get('release_date'):
        existing['release_date'] = member['release_date']
    if (member.get('series_index') is not None and
            (existing.get('series_index') is None or
             (member.get('series_position_source') == 'Wikidata P1545' and
              existing.get('series_position_source') != 'Wikidata P1545'))):
        for key in ('series_index', 'series_position', 'series_position_source'):
            if member.get(key) is not None:
                existing[key] = member[key]
    if member['provider'] not in existing.setdefault('metadata_sources', []):
        existing['metadata_sources'].append(member['provider'])
    source_map = existing.setdefault('catalog_sources', {})
    source_url = member.get('source_url') or member.get('url')
    if source_url:
        urls = source_map.setdefault(member['provider'], [])
        if source_url not in urls:
            urls.append(source_url)
    catalog = member.get('catalog_metadata') or member.get('metadata') or {}
    catalog_id = str(catalog.get('catalog_id') or catalog.get('qid') or '').strip()
    if catalog_id:
        ids = existing.setdefault('catalog_ids', {}).setdefault(member['provider'], [])
        if catalog_id not in ids:
            ids.append(catalog_id)


def _expand_book_series_results(results, query):
    """Put all strong matching series first, followed by their ordered member rows."""
    series_rows = [row for row in results
                   if row.get('kind') == 'series' and
                   (row.get('books') or any(variant.get('books')
                                             for variant in row.get('metadata_variants') or []))]
    scored = [( _series_match_score(query, row.get('title')), row) for row in series_rows]
    scored = [(score, row) for score, row in scored if score >= 0.76]
    if not scored:
        return None, None
    scored.sort(key=lambda value: (-value[0], _series_name_key(value[1].get('title'))))
    best_score, best = scored[0]
    series_key = _series_name_key(best.get('title'))
    matched = [row for score, row in scored
               if _series_name_key(row.get('title')) == series_key and score >= best_score - 0.12]
    bundle = dict(best)
    bundle['kind'] = 'series'
    bundle['books'] = []
    bundle['catalog_sources'] = {}
    bundle['catalog_ids'] = {}
    members = []
    review_members = []
    for row in matched:
        variants = row.get('metadata_variants') or [row]
        for variant in variants:
            provider = str(variant.get('provider') or row.get('provider') or 'Catalog')
            if provider not in bundle.setdefault('metadata_sources', []):
                bundle['metadata_sources'].append(provider)
            source_url = variant.get('source_url') or variant.get('url')
            if source_url:
                urls = bundle['catalog_sources'].setdefault(provider, [])
                if source_url not in urls:
                    urls.append(source_url)
            catalog = variant.get('catalog_metadata') or variant.get('metadata') or {}
            catalog_id = str(catalog.get('catalog_id') or catalog.get('qid') or '').strip()
            if catalog_id:
                ids = bundle['catalog_ids'].setdefault(provider, [])
                if catalog_id not in ids:
                    ids.append(catalog_id)
            for book in variant.get('books') or []:
                _merge_series_member(members, book, provider,
                                     review_members=review_members)
            for candidate in variant.get('review_members') or []:
                _merge_series_member(review_members, candidate, provider)
    members, series_plan = enrich_series_members(bundle.get('title'), members)
    accepted_members, packaging_candidates = [], []
    for member in members:
        reason = series_member_review_reason(bundle.get('title'), member)
        if reason:
            candidate = dict(member)
            candidate['needs_review'] = True
            candidate['review_reason'] = reason
            packaging_candidates.append(candidate)
        else:
            accepted_members.append(member)
    members = accepted_members
    for candidate in packaging_candidates:
        _merge_series_member(review_members, candidate,
                             candidate.get('provider') or 'Catalog')
    bundle['books'] = members
    bundle['review_members'] = review_members
    if series_plan:
        bundle['series_plan'] = series_plan
    bundle['catalog_metadata'] = dict(bundle.get('catalog_metadata') or {})
    bundle['catalog_metadata']['member_count'] = len(members)
    bundle['catalog_metadata']['review_member_count'] = len(review_members)
    if not members:
        members = []
    for member in members:
        member['series'] = str(bundle.get('title') or '')
    members.sort(key=lambda member: (
        member.get('series_index') is None,
        member.get('series_index') if member.get('series_index') is not None else float('inf'),
        member.get('release_date') or '9999-99-99',
        _title_key(member)))
    providers = list(bundle.get('metadata_sources') or [])
    series_result = dict(bundle)
    series_result.update({
        'kind': 'series', 'entity_type': 'series',
        'series': bundle.get('title') or '',
        'format': 'catalog', 'source_role': 'metadata', 'downloadable': False,
        'discovery_only': True, 'category': 'Books',
        'url': bundle.get('source_url') or bundle.get('url') or '',
        'source': bundle.get('provider') or (providers[0] if providers else 'Catalog'),
        'display_source': 'Metadata · ' + ', '.join(providers),
        'metadata_sources': providers,
        'metadata_variants': list(bundle.get('metadata_variants') or [dict(best)]),
        'download_variants': [], 'available_formats': [],
        'series_bundle': bundle,
    })
    rows = [series_result]
    for member in members:
        providers = member.get('metadata_sources') or [member.get('provider') or 'Catalog']
        row = dict(member)
        row.update({
            'kind': 'book', 'series': bundle.get('title') or '',
            'format': ('announced' if member.get('publication_status') ==
                       'announced; release date unconfirmed' else 'catalog'),
            'source_role': 'metadata', 'downloadable': False,
            'discovery_only': True, 'category': 'Books',
            'url': member.get('source_url') or member.get('url') or '',
            'source': member.get('provider') or providers[0],
            'display_source': 'Metadata · ' + ', '.join(providers),
            'metadata_sources': list(providers),
            'metadata_variants': list(member.get('metadata_variants') or [dict(member)]),
            'download_variants': [], 'available_formats': [],
            'series_bundle': bundle,
        })
        rows.append(row)
    bundle['matching_series_count'] = 1
    seen_series = {series_key}
    for _score, candidate in scored[1:]:
        candidate_key = _series_name_key(candidate.get('title'))
        if candidate_key in seen_series:
            continue
        seen_series.add(candidate_key)
        alternate_rows, _alternate_bundle = _expand_book_series_results([candidate], query)
        if alternate_rows:
            rows.extend(alternate_rows)
            bundle['matching_series_count'] += 1
    return rows, bundle


def _query_wikidata(query, category=None):
    results = search_wikidata(query, limit=20)
    normalized = []
    for item in results:
        item['description'] = item.get('description') or ''
        description = item['description'].casefold()
        description_subtype = video_subtype_from_description(description)
        if category in {'Videos', 'Anime'}:
            if (category == 'Anime' and
                    not any(term in description for term in ('anime', 'japanese animated')) and
                    description_subtype not in {'anime_oad', 'anime_ova'}):
                continue
            video_terms = ('film', 'movie', 'television', 'tv series', 'web series',
                           'animated series', 'anime', 'short film', 'miniseries',
                           'animation dvd', 'animation disc', 'original video animation',
                           'independent animation', 'animated short', 'animation short')
            if 'media franchise' in description or 'film series' in description:
                subtype, entity_type = 'franchise', 'franchise'
            else:
                if not any(term in description for term in video_terms) and not description_subtype:
                    continue
                entity_type = ('series' if any(term in description for term in (
                    'television series', 'tv series', 'web series', 'miniseries',
                    'mini-series', 'limited series')) else 'work')
                if description_subtype in {'anime_oad', 'anime_ova', 'independent_animation',
                                           'animated_short', 'short_film', 'animated_film'}:
                    subtype = description_subtype
                elif 'anime' in description or 'japanese animated' in description:
                    subtype = 'anime'
                elif description_subtype:
                    subtype = description_subtype
                elif 'web series' in description:
                    subtype = 'web_series'
                elif 'miniseries' in description:
                    subtype = 'miniseries'
                elif ('television' in description or 'tv series' in description or
                      'animated series' in description):
                    subtype = 'tv_series'
                else:
                    subtype = 'movie'
            item.update(kind='video', entity_type=entity_type, video_subtype=subtype)
        elif category == 'Games':
            game_terms = ('video game', 'computer game', 'game franchise', 'video game series')
            if not any(term in description for term in game_terms) or 'compilation' in description:
                continue  # compilations bundle games already listed on their own
            entity_type = ('franchise' if 'franchise' in description or 'series' in description
                           else 'work')
            item.update(kind='game', entity_type=entity_type,
                        game_subtype='franchise' if entity_type == 'franchise' else 'base_game')
        elif category == 'Comics':
            if not any(term in description for term in ('comic book', 'comic series', 'comic strip', 'webcomic',
                                                        'graphic novel', 'comics series')):
                continue
            if re.search(r'^(?:(?:american|british|japanese|canadian|fictional)\s+)*(?:comic(?:s| book)?\s+)?(?:character|writer|artist|publisher)\b', description):
                continue
            collected = (any(term in description for term in ('graphic novel', 'collected edition', 'trade paperback')) or
                         bool(re.search(r'\b(?:first|second|third|\d+(?:st|nd|rd|th)?)\s+volume\s+(?:in|of)\b', description)))
            numbered_issue = re.search(r'(?:#|\bissue\s*(?:no\.?\s*)?)\d+', str(item.get('title') or ''), re.I)
            item.update(kind='webcomic' if 'webcomic' in description else 'comic',
                        comic_entity='collection' if collected else 'issue' if numbered_issue else 'series')
        item['catalog_metadata'] = {
            'wikidata_qid': item.get('qid'), 'description': item['description'],
            'entity_type': item.get('entity_type', 'work'),
            'video_subtype': item.get('video_subtype'),
            'game_subtype': item.get('game_subtype'),
            'external_ids': [{'provider': 'wikidata', 'external_id': item.get('qid'),
                              'source_url': item.get('source_url')}],
        }
        item['external_ids'] = item['catalog_metadata']['external_ids']
        item['format'] = 'catalog'
        item['source'] = 'Wikidata'
        item['url'] = item.get('source_url') or ''
        item['discovery_only'] = True
        normalized.append(item)
    return normalized


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
    archive_items, archive_errors = _internet_archive_files(
        query, 'Web serials', source_options)
    results.extend(archive_items)
    errors.extend(archive_errors)
    for source_key, provider, searcher, resolver in (
            ('meawfy.com', 'Meawfy', search_meawfy, resolve_meawfy_downloads),
            ('softarchive.download', 'SoftArchive', search_softarchive,
             resolve_softarchive_downloads)):
        found, source_errors = _general_download_site_files(
            query, 'Web serials', source_options, source_key, provider, searcher, resolver)
        results.extend(found)
        errors.extend(source_errors)
    return results, errors


def _internet_archive_files(query, category, source_options):
    options = dict(source_options or {})
    approval = _approved_source_record(options, 'internet_archive')
    if not approval:
        return [], []
    try:
        results = search_internet_archive(query, category=category, limit=20)
        for item in results:
            item['source_role'] = 'download'
            item['downloadable'] = True
            item['category'] = category
            if options.get('rights_evidence_path'):
                item['rights_evidence_path'] = options['rights_evidence_path']
            if approval.get('rights_work_key'):
                item['rights_work_key'] = approval['rights_work_key']
        return results, []
    except Exception as exc:
        logger.warning('Internet Archive search failed for %r in %s: %s',
                       query, category, exc)
        return [], [f'Internet Archive: {exc}']


def _general_download_site_files(query, category, source_options, source_key,
                                 provider, searcher, resolver):
    options = dict(source_options or {})
    approval = _approved_source_record(options, source_key)
    if not approval:
        return [], []
    allowed = _CATEGORY_FORMATS.get(category, set())
    results, errors = [], []
    try:
        for hit in searcher(query):
            try:
                items = resolver(hit, allowed_formats=allowed)
                for item in items:
                    item['source_role'] = 'download'
                    item['downloadable'] = True
                    item['category'] = category
                    item.setdefault('provider', provider)
                    if options.get('rights_evidence_path'):
                        item['rights_evidence_path'] = options['rights_evidence_path']
                    if approval.get('rights_work_key'):
                        item['rights_work_key'] = approval['rights_work_key']
                results.extend(items)
            except Exception as exc:
                errors.append(f'{provider} detail page: {exc}')
    except Exception as exc:
        errors.append(f'{provider} search: {exc}')
    return results, errors


def _meawfy_files(query, category, source_options):
    return _general_download_site_files(
        query, category, source_options, 'meawfy.com', 'Meawfy',
        search_meawfy, resolve_meawfy_downloads)


def _softarchive_files(query, category, source_options):
    return _general_download_site_files(
        query, category, source_options, 'softarchive.download', 'SoftArchive',
        search_softarchive, resolve_softarchive_downloads)


def _registered_download_site_files(query, category, source_options):
    options = dict(source_options or {})
    allowed = _CATEGORY_FORMATS.get(category, set())
    results, errors = [], []
    for source_key, provider, categories in GENERAL_DOWNLOAD_ADAPTERS:
        if category not in categories:
            continue
        approval = _approved_source_record(options, source_key)
        if not approval:
            continue
        try:
            hits = search_registered_download_site(
                query, source_key, limit=30, category=category,
                allowed_formats=allowed)
            for hit in hits:
                if hit.get('discovery_only') and not hit.get('detail_url'):
                    hit['category'] = category
                    if category == 'Games':
                        hit['kind'] = 'game'
                    hit['display_source'] = f"Search page · {provider}"
                    results.append(hit)
                    continue
                if source_key == 'xdcc.eu':
                    items = [hit]
                else:
                    items = resolve_site_downloads(hit, source_key,
                                                   allowed_formats=allowed)
                for item in items:
                    item['source_role'] = 'download'
                    item['downloadable'] = True
                    item['category'] = category
                    item.setdefault('provider', provider)
                    if category == 'Games':
                        item.setdefault('kind', 'game')
                    if options.get('rights_evidence_path'):
                        item['rights_evidence_path'] = options['rights_evidence_path']
                    if approval.get('rights_work_key'):
                        item['rights_work_key'] = approval['rights_work_key']
                results.extend(items)
        except Exception as exc:
            logger.warning('%s search failed for %r: %s', provider, query, exc)
            errors.append(f'{provider}: {exc}')
    return results, errors


def _foss_download_files(query, source_options, source_key, provider, searcher):
    options = dict(source_options or {})
    approval = _approved_source_record(options, source_key)
    if not approval:
        return [], []
    try:
        results = searcher(query)
        for item in results:
            item['category'] = 'Software'
            item['downloadable'] = True
            if options.get('rights_evidence_path'):
                item['rights_evidence_path'] = options['rights_evidence_path']
            if approval.get('rights_work_key'):
                item['rights_work_key'] = approval['rights_work_key']
        return results, []
    except Exception as exc:
        logger.warning('%s FOSS search failed for %r: %s', provider, query, exc)
        return [], [f'{provider}: {exc}']


def _tasks(query, category, source_options, credentials=None):
    credentials = credentials or {}
    if category == 'Books':
        return [
            ('Goodreads', lambda: search_goodreads(query, limit=60)),
            ('Open Library', lambda: search_openlibrary_metadata(query, limit=40)),
            ('Google Books', lambda: search_google_books_metadata(query, limit=30)),
            ('Wikidata', lambda: _search_wikidata_series(query)),
            ('book download sources', lambda: _search_candidates(
                {'kind': 'book', 'title': query, 'author': 'Unknown',
                 'metadata': json.dumps({'sources': source_options or {}})}, source_options)),
        ]
    if category == 'Comics':
        tasks = comic_metadata_tasks(query, credentials)
        tasks.extend([('Wikidata', lambda: _query_wikidata(query, 'Comics')),
                      ('comic download sources', lambda: _comic_files(query, source_options, 'comic'))])
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
        tmdb_key = str(credentials.get('tmdb_api_key') or os.environ.get('TMDB_API_KEY', '')).strip()
        tasks = [
            ('AniList', lambda: search_anilist(query, media_type='ANIME')),
            ('TVMaze', lambda: search_tvmaze(query)),
            ('Wikidata', lambda: _query_wikidata(query, 'Anime')),
        ]
        if tmdb_key:
            tasks.extend([('TMDB movies', lambda: search_tmdb(query, tmdb_key, 'movie')),
                          ('TMDB TV', lambda: search_tmdb(query, tmdb_key, 'tv'))])
        tasks.append(('Internet Archive', lambda: _internet_archive_files(
            query, 'Anime', source_options)))
        tasks.append(('Meawfy', lambda: _meawfy_files(query, 'Anime', source_options)))
        tasks.append(('SoftArchive', lambda: _softarchive_files(query, 'Anime', source_options)))
        for source_key, provider, searcher, resolver in ANIME_DOWNLOAD_ADAPTERS:
            tasks.append((provider, lambda source_key=source_key, provider=provider,
                          searcher=searcher, resolver=resolver:
                          _general_download_site_files(
                              query, 'Anime', source_options, source_key, provider,
                              searcher, resolver)))
        tasks.append(('IRC', lambda: _irc_files(query, category, source_options)))
        return tasks
    if category == 'Videos':
        tmdb_key = str(credentials.get('tmdb_api_key') or os.environ.get('TMDB_API_KEY', '')).strip()
        tasks = [('TVMaze', lambda: search_tvmaze(query)),
                 ('Wikidata', lambda: _query_wikidata(query, 'Videos'))]
        if tmdb_key:
            tasks.extend([
                ('TMDB movies', lambda: search_tmdb(query, tmdb_key, 'movie')),
                ('TMDB TV', lambda: search_tmdb(query, tmdb_key, 'tv')),
            ])
        tasks.append(('Internet Archive', lambda: _internet_archive_files(
            query, 'Videos', source_options)))
        tasks.append(('Meawfy', lambda: _meawfy_files(query, 'Videos', source_options)))
        tasks.append(('SoftArchive', lambda: _softarchive_files(query, 'Videos', source_options)))
        tasks.append(('IRC', lambda: _irc_files(query, category, source_options)))
        return tasks
    if category == 'Games':
        client_id = str(credentials.get('igdb_client_id') or os.environ.get('IGDB_CLIENT_ID', '')).strip()
        client_secret = str(credentials.get('igdb_client_secret') or
                            os.environ.get('IGDB_CLIENT_SECRET', '')).strip()
        tasks = [('Steam', lambda: search_steam_games(query)),
                 ('Wikidata', lambda: _query_wikidata(query, 'Games'))]
        if client_id and client_secret:
            tasks.append(('IGDB', lambda: search_igdb_games(query, client_id, client_secret)))
        tasks.append(('Internet Archive', lambda: _internet_archive_files(
            query, 'Games', source_options)))
        tasks.append(('Meawfy', lambda: _meawfy_files(query, 'Games', source_options)))
        tasks.append(('SoftArchive', lambda: _softarchive_files(query, 'Games', source_options)))
        tasks.append(('IRC', lambda: _irc_files(query, category, source_options)))
        return tasks
    if category == 'Software':
        return [
            ('FOSShub', lambda: search_fosshub_foss(query)),
            ('SourceForge', lambda: _foss_download_files(
                query, source_options, 'sourceforge.net', 'SourceForge',
                search_sourceforge_foss)),
            ('GitHub (FOSS)', lambda: _foss_download_files(
                query, source_options, 'github_foss', 'GitHub (FOSS)',
                search_github_foss)),
            ('IRC', lambda: _irc_files(query, category, source_options)),
        ]
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
                ('Internet Archive', lambda: _internet_archive_files(
                    query, 'Fanfiction', source_options)),
                ('Meawfy', lambda: _meawfy_files(query, 'Fanfiction', source_options)),
                ('SoftArchive', lambda: _softarchive_files(query, 'Fanfiction', source_options)),
                ('IRC', lambda: _irc_files(query, category, source_options))]
    raise ValueError(f'Unknown media category: {category}')


def search_media_category(query, category, source_options=None, credentials=None,
                          download_priority=None, _allow_query_repair=True):
    """Search every configured metadata and download connector for one category.

    Returns normalized results plus connector errors so the GUI can show partial
    results without hiding which providers failed.
    """
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    category = str(category or '').strip()
    source_options = dict(source_options or {})
    priority = download_priority or connected_download_sources(category, source_options)
    source_options['download_priority'] = list(priority)
    tasks = _tasks(query, category, source_options, credentials)
    if category not in {'Books', 'Light novels'} and any(
            category in categories and _approved_source_record(source_options, source_key)
           for source_key, _provider, categories in GENERAL_DOWNLOAD_ADAPTERS):
        tasks.append(('additional approved download sites',
                      lambda: _registered_download_site_files(
                          query, category, source_options)))
    results, errors = [], []
    if not tasks:
        return {'category': category, 'results': [], 'errors': [], 'searched': []}
    # A bounded pool overlaps independent metadata/download connectors without
    # launching one thread per configured site or saturating the UI machine.
    with ThreadPoolExecutor(max_workers=min(10, len(tasks))) as pool:
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
                    if category == 'Anime' and row.get('provider') not in {'AniList', 'Wikidata'}:
                        catalog = row.get('catalog_metadata') or row.get('metadata') or {}
                        genres = {str(value).casefold() for value in catalog.get('genres') or []}
                        genre_ids = {str(value) for value in catalog.get('genre_ids') or []}
                        original_language = str(catalog.get('original_language') or '').casefold()
                        anime_catalog_match = ('anime' in genres or
                                               ('ja' == original_language and '16' in genre_ids))
                        if not anime_catalog_match:
                            continue
                        row['kind'] = 'anime'
                        row['video_subtype'] = row.get('video_subtype') or 'anime'
                    results.append(row)
            except Exception as exc:
                logger.warning('%s category search failed for %r: %s', provider, query, exc)
                errors.append(f'{provider}: {exc}')
    unique_titles = _merge_same_title(results, category, priority)
    if category in {'Videos', 'Anime', 'Games'}:
        unique_titles = _sort_by_title_relevance(unique_titles, query)
    if category == 'Comics':
        unique_titles = sort_comics(unique_titles, query)
    result_limit = 500 if category == 'Comics' else 150
    response = {'category': category, 'results': unique_titles[:result_limit],
                'errors': list(dict.fromkeys(errors)),
                'searched': [provider for provider, _ in tasks]}
    if category == 'Comics':
        response['comic_summary'] = {
            entity: sum(row.get('comic_entity') == entity for row in response['results'])
            for entity in ('issue', 'series', 'collection')}
        response['comic_summary']['truncated'] = len(unique_titles) > result_limit
    if category == 'Books':
        series_rows, series_bundle = _expand_book_series_results(unique_titles, query)
        if series_bundle:
            response['results'] = series_rows[:5000]
            response['series_name'] = series_bundle.get('title')
            response['series_member_count'] = len(series_bundle.get('books') or [])
            response['series_review_count'] = len(series_bundle.get('review_members') or [])
            response['series_candidate_count'] = int(
                series_bundle.get('matching_series_count') or 1)
            if series_bundle.get('query_correction'):
                response['query_correction'] = series_bundle['query_correction']
        elif not unique_titles and _allow_query_repair:
            # Repair the common doubled-letter typo only after a true zero-hit
            # search; valid doubled letters such as "book" remain untouched.
            corrected = re.sub(r'([a-z])\1+', r'\1', query, flags=re.I)
            if corrected and corrected.casefold() != query.casefold():
                logger.info('Retrying empty Books search %r with repeated-letter correction %r',
                            query, corrected)
                retry = search_media_category(
                    corrected, category, source_options, credentials,
                    download_priority, _allow_query_repair=False)
                response = retry
                response['errors'] = list(dict.fromkeys(errors + retry.get('errors', [])))
                response['searched'] = list(dict.fromkeys(
                    [provider for provider, _ in tasks] + retry.get('searched', [])))
                if retry.get('results'):
                    response['query_correction'] = {'from': query, 'to': corrected}
    response['results'] = _promote_matching_series_results(response.get('results'), query)
    return response
