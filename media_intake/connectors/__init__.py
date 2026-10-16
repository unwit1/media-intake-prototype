"""Source connectors added from the PirateBot source review, as native Media Intake sources.

``CONNECTORS`` describes each source: what it is, which categories it serves and
what it needs (account, API key, mirror address). ``search_sources`` runs the
enabled, approved connectors for one category in parallel; one slow or broken
site never blocks or fails the others. ``resolve_download`` turns a queued row
into the actual file, magnet or NZB when the download starts.

File-capable sources obey the same rights-evidence.json approval as every other
download connector: they are searched and downloaded only after the user sets
their entry to YES. Metadata sources (AniDB, WorldCat) and the AIO Search browser
link need no approval.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import logging
import os
import time

from .base import ConnectorSpec, SearchPage, SourceError, clean_base
from . import catalogs, libraries, torrent_sites, usenet
from .releases import dedupe_releases

logger = logging.getLogger(__name__)

ALL = frozenset({'Books', 'Comics', 'Manga', 'Anime', 'Videos', 'Games', 'Software', 'Light novels',
                 'Web serials', 'Fanfiction'})

_SPECS = (
    ConnectorSpec('thepiratebay', 'The Pirate Bay', 'torrent',
                  frozenset({'Videos', 'Anime', 'Games', 'Software', 'Books', 'Comics', 'Manga',
                             'Light novels'}),
                  'General torrent index, searched through its JSON API (apibay).',
                  default_base='https://apibay.org', settings=(('base', 'API address', False),),
                  jackett_indexer='thepiratebay', pages=False),
    ConnectorSpec('1337x', '1337x', 'torrent', frozenset({'Videos', 'Anime', 'Games', 'Software', 'Books'}),
                  'General torrent index. Often behind a browser check; route it through Jackett with '
                  'FlareSolverr if direct search is blocked.',
                  default_base='https://1337x.to', settings=(('base', 'Site address', False),),
                  jackett_indexer='1337x'),
    ConnectorSpec('rutracker', 'RuTracker', 'torrent',
                  frozenset({'Videos', 'Anime', 'Games', 'Software', 'Books', 'Comics', 'Manga'}),
                  'Large Russian-language tracker with many international releases. Searching needs a free '
                  'account; magnets come from its public API.',
                  default_base='https://rutracker.org',
                  settings=(('base', 'Site address', False), ('username', 'Username', False),
                            ('password', 'Password', True)),
                  jackett_indexer='rutracker', needs=('username', 'password')),
    ConnectorSpec('nyaa', 'Nyaa', 'torrent', frozenset({'Anime', 'Manga', 'Light novels'}),
                  'Anime, manga and light-novel torrents, searched through its RSS feed.',
                  default_base='https://nyaa.si',
                  settings=(('base', 'Site address', False),
                            ('filter', 'Filter (0 = all, 1 = no remakes, 2 = trusted only)', False)),
                  jackett_indexer='nyaasi'),
    ConnectorSpec('yts', 'YTS', 'torrent', frozenset({'Videos'}),
                  'Movie torrents in small encodes, searched through its JSON API.',
                  default_base='https://yts.mx', settings=(('base', 'Site address', False),),
                  jackett_indexer='yts'),
    ConnectorSpec('eztvx', 'EZTVx', 'torrent', frozenset({'Videos'}),
                  'TV episode torrents. Searches by show name, or by IMDb ID (tt…) through its API.',
                  default_base='https://eztvx.to', settings=(('base', 'Site address', False),),
                  jackett_indexer='eztv', pages=False),
    ConnectorSpec('fitgirl', 'FitGirl Repacks', 'torrent', frozenset({'Games'}),
                  'Compressed PC game repacks. Searched through the site\'s WordPress API; each release '
                  'queues as a torrent.',
                  default_base='https://fitgirl-repacks.site', settings=(('base', 'Site address', False),)),
    ConnectorSpec('nzbgeek', 'NZBGeek', 'usenet',
                  frozenset({'Videos', 'Anime', 'Books', 'Comics', 'Games', 'Software'}),
                  'Usenet indexer (paid membership). Needs your API key, a Usenet provider and SABnzbd or '
                  'NZBGet to download.',
                  default_base='https://api.nzbgeek.info',
                  settings=(('base', 'API address', False), ('api_key', 'API key', True)), needs=('api_key',)),
    ConnectorSpec('drunkenslug', 'DrunkenSlug', 'usenet',
                  frozenset({'Videos', 'Anime', 'Books', 'Comics', 'Games', 'Software'}),
                  'Invite-only Usenet indexer. Needs your API key, a Usenet provider and SABnzbd or NZBGet.',
                  default_base='https://api.drunkenslug.com',
                  settings=(('base', 'API address', False), ('api_key', 'API key', True)), needs=('api_key',)),
    ConnectorSpec('z-library', 'Z-Library', 'direct', frozenset({'Books', 'Light novels'}),
                  'Ebook library. Needs a Z-Library account and your personal domain; daily download limits '
                  'apply.',
                  default_base='',
                  settings=(('base', 'Personal domain (https://…)', False), ('username', 'Email', False),
                            ('password', 'Password', True), ('user_id', 'or User ID', False),
                            ('user_key', 'and User key', True)),
                  needs=('base',)),
    ConnectorSpec('magzdb', 'MagzDB', 'direct', frozenset({'Books'}),
                  'Magazine archive: finds magazine titles and their issues (mostly PDF).',
                  default_base='https://magzdb.org', settings=(('base', 'Site address', False),), pages=False),
    ConnectorSpec('vimm', "Vimm's Lair", 'direct', frozenset({'Games'}),
                  'Classic console game archive. Some titles have downloads disabled at publishers\' request.',
                  default_base='https://vimm.net',
                  settings=(('base', 'Site address', False), ('system', 'Only this system (optional, e.g. SNES)',
                                                              False)), pages=False),
    ConnectorSpec('anidb', 'AniDB', 'metadata', frozenset({'Anime'}),
                  'Anime database. Titles are matched against AniDB\'s daily title list; details need a '
                  'registered AniDB API client name.',
                  settings=(('client', 'API client name (optional)', False),
                            ('clientver', 'API client version', False))),
    ConnectorSpec('worldcat', 'WorldCat', 'metadata', frozenset({'Books', 'Light novels', 'Comics'}),
                  'Library catalogue of the world\'s libraries. Shows results here with an OCLC WorldCat Search '
                  'API key; otherwise opens WorldCat in your browser.',
                  settings=(('client_id', 'OCLC API key', False), ('client_secret', 'OCLC secret', True)),
                  needs=('client_id', 'client_secret')),
    ConnectorSpec('aiosearch', 'AIO Search', 'browser', ALL,
                  'All-in-one search launcher. It runs other sites\' searches in a browser page, so it '
                  'opens in your browser.',
                  settings=(('template', 'Search address ({query} is replaced)', False),), pages=False),
)
CONNECTORS = {spec.key: spec for spec in _SPECS}
BY_LABEL = {spec.label: spec for spec in _SPECS}
FILE_SOURCE_KEYS = tuple(spec.key for spec in _SPECS if spec.file_capable)
METADATA_SOURCE_KEYS = tuple(spec.key for spec in _SPECS if not spec.file_capable)

_SEARCHERS = {
    'thepiratebay': torrent_sites.search_tpb, '1337x': torrent_sites.search_1337x,
    'rutracker': torrent_sites.search_rutracker, 'nyaa': torrent_sites.search_nyaa,
    'yts': torrent_sites.search_yts, 'eztvx': torrent_sites.search_eztvx,
    'fitgirl': torrent_sites.search_fitgirl, 'nzbgeek': usenet.search_newznab,
    'drunkenslug': usenet.search_newznab, 'z-library': libraries.search_zlibrary,
    'magzdb': libraries.search_magzdb, 'vimm': libraries.search_vimm, 'anidb': catalogs.search_anidb,
    'worldcat': catalogs.search_worldcat, 'aiosearch': catalogs.search_aiosearch,
}
_RESOLVERS = {
    '1337x': torrent_sites.resolve_1337x, 'rutracker': torrent_sites.resolve_rutracker,
    'z-library': libraries.resolve_zlibrary, 'magzdb': libraries.resolve_magzdb, 'vimm': libraries.resolve_vimm,
}
SEARCH_TIMEOUT = 45
# A browser-launcher row in every category's results would be noise; it stays available as a
# single-source search and can be switched on in Settings → Sources.
OFF_BY_DEFAULT = frozenset({'aiosearch'})
USENET_CLIENT_FIELDS = (('client', 'Program (sabnzbd or nzbget)', False), ('url', 'Address', False),
                        ('api_key', 'SABnzbd API key', True), ('username', 'NZBGet username', False),
                        ('password', 'NZBGet password', True), ('category', 'Category to use (optional)', False),
                        ('nzb_mode', 'Without a program: "fail" or "save" the .nzb file', False))


def connectors_for(category):
    return [spec for spec in _SPECS if category in spec.categories]


# --------------------------------------------------------------------------- settings
_SESSION_SECRETS = {}


def _secret_name(key, field):
    return 'src_' + ''.join(char if char.isalnum() else '_' for char in f'{key}_{field}')


def _env_name(key, field):
    return 'MEDIA_INTAKE_' + ''.join(char if char.isalnum() else '_' for char in f'{key}_{field}').upper()


def load_source_config(db, key, cache_dir=None):
    """Read one source's saved settings; secrets come from protected storage or the environment."""
    from ..kindle import get_local_secret
    from ..watched import get_setting
    fields = USENET_CLIENT_FIELDS if key == 'usenet_client' else CONNECTORS[key].settings
    config = {}
    for field, _label, secret in fields:
        value = os.environ.get(_env_name(key, field), '')
        if not value and secret:
            value = _SESSION_SECRETS.get((key, field), '')
            if not value and db is not None:
                try:
                    value = get_local_secret(db, _secret_name(key, field)) or ''
                except Exception:
                    value = ''
        elif not value and db is not None:
            value = get_setting(db, f'source:{key}:{field}', '') or ''
        config[field] = value
    if key != 'usenet_client':
        default = '0' if key in OFF_BY_DEFAULT else '1'
        config['enabled'] = (get_setting(db, f'source:{key}:enabled', default) if db is not None else default) != '0'
        config['access'] = (get_setting(db, f'source:{key}:access', 'auto') if db is not None else 'auto') or 'auto'
    if cache_dir:
        config['cache_dir'] = str(cache_dir)
    return config


def save_source_config(db, key, values):
    """Persist settings. Secrets use Windows DPAPI where available, otherwise last for this session only.

    Returns the names of secret fields kept only for this session.
    """
    from ..kindle import set_local_secret
    from ..watched import set_setting
    fields = USENET_CLIENT_FIELDS if key == 'usenet_client' else CONNECTORS[key].settings
    session_only = []
    for field, _label, secret in fields:
        if field not in values:
            continue
        value = str(values[field] or '').strip() if not secret else str(values[field] or '')
        if secret:
            _SESSION_SECRETS[(key, field)] = value
            try:
                set_local_secret(db, _secret_name(key, field), value)
            except Exception:
                if value:
                    session_only.append(field)
        else:
            set_setting(db, f'source:{key}:{field}', value)
    for field in ('enabled', 'access'):
        if field in values and key != 'usenet_client':
            value = values[field]
            if field == 'enabled':
                value = '1' if value in (True, '1', 1, 'true') else '0'
            elif value not in ('auto', 'direct', 'jackett'):
                raise ValueError('Access must be auto, direct or jackett')
            set_setting(db, f'source:{key}:{field}', value)
    return session_only


def load_all_configs(db, cache_dir=None, jackett=None):
    configs = {key: load_source_config(db, key, cache_dir) for key in CONNECTORS}
    configs['usenet_client'] = load_source_config(db, 'usenet_client')
    if jackett:
        configs['jackett'] = dict(jackett)
    return configs


def missing_settings(spec, config):
    missing = [field for field in spec.needs if not str(config.get(field) or '').strip()]
    if spec.key == 'z-library' and not missing and not (
            (config.get('username') and config.get('password')) or (config.get('user_id') and config.get('user_key'))):
        missing.append('account')
    return missing


# --------------------------------------------------------------------------- search
def _jackett_ready(configs):
    jackett = (configs or {}).get('jackett') or {}
    return bool(jackett.get('jackett_url') and jackett.get('jackett_api_key')), jackett


def _search_via_jackett(spec, query, category, configs):
    from ..jackett import JackettError, search_jackett
    ready, jackett = _jackett_ready(configs)
    if not ready:
        raise SourceError(spec.label, 'it is set to search through Jackett, but Jackett is not set up.', 'config',
                          'Add the Jackett address and API key in Settings → Downloads.')
    try:
        hits = search_jackett(query, jackett['jackett_url'], jackett['jackett_api_key'], category=category,
                              indexer=spec.jackett_indexer, limit=100)
    except JackettError as exc:
        text = str(exc)
        hint = (f'Add the "{spec.jackett_indexer}" indexer in your Jackett dashboard.'
                if 'HTTP 404' in text or 'not configured' in text.casefold() else '')
        raise SourceError(spec.label, f'Jackett could not search it ({text}).', 'unreachable', hint) from exc
    rows = []
    for hit in hits:
        row = torrent_sites.torrent_row(spec, category, name=hit['title'], magnet=hit.get('magnet') or '',
                                        torrent_url=hit.get('torrent_url') or '', info_hash=hit.get('info_hash'),
                                        size=hit.get('size'), seeders=hit.get('seeders'), peers=hit.get('peers'),
                                        published=hit.get('published') or '', details_url=hit.get('details_url'))
        row['provider'] = f'{spec.label} (via Jackett)'
        row['display_source'] = row['display_source'].replace(spec.label, row['provider'], 1)
        row['via'] = 'jackett'
        from ..jackett import redact
        row['source'] = redact(row['source'])
        rows.append(row)
    return SearchPage(rows, 1, False, 'jackett')


def search_one(key, query, category, page=1, configs=None, opener=None):
    """Search one connector; honours its access mode (direct, Jackett, or direct then Jackett)."""
    spec = CONNECTORS[key]
    configs = configs or {}
    config = dict(configs.get(key) or {})
    query = str(query or '').strip()
    if not query:
        raise SourceError(spec.label, 'enter something to search for.', 'config')
    access = config.get('access') or 'auto'
    if spec.role == 'torrent' and spec.jackett_indexer and access == 'jackett':
        return _search_via_jackett(spec, query, category, configs)
    if spec.default_base or spec.key == 'z-library':
        config['base'] = clean_base(spec.label, config.get('base'), spec.default_base)
    if key == 'eztvx' and str(config.get('imdb_id') or '').strip():
        query = config['imdb_id']
    try:
        return _SEARCHERS[key](spec, query, category, page, config, opener)
    except SourceError as exc:
        ready, _jackett = _jackett_ready(configs)
        if (spec.role == 'torrent' and spec.jackett_indexer and access == 'auto' and ready and page == 1
                and exc.kind in ('blocked', 'unreachable', 'auth')):
            logger.info('%s direct search failed (%s); trying Jackett', spec.label, exc)
            result = _search_via_jackett(spec, query, category, configs)
            result.notice = f'{spec.label}: direct search failed ({exc.kind}); used Jackett instead.'
            return result
        raise


def search_sources(query, category, options=None, keys=None, pages=None, opener=None, timeout=SEARCH_TIMEOUT,
                   health=None):
    """Search every enabled connector for ``category`` in parallel.

    Returns {'rows', 'errors', 'skipped', 'next_pages', 'notices', 'searched'}. A connector
    that is disabled, unapproved or not set up is listed in ``skipped`` with the reason
    instead of failing; a connector that fails is listed in ``errors`` and the rest still
    return their results.
    """
    from ..watched import _approved_source_record
    options = dict(options or {})
    configs = options.get('source_configs') or {}
    pages = dict(pages or {})
    selected = [spec for spec in connectors_for(category) if keys is None or spec.key in keys]
    if pages:
        selected = [spec for spec in selected if spec.key in pages]
    runnable, skipped, approvals = [], {}, {}
    for spec in selected:
        config = configs.get(spec.key) or {}
        if config.get('enabled', spec.key not in OFF_BY_DEFAULT) is False:
            skipped[spec.key] = 'turned off in Settings → Sources'
            continue
        if spec.file_capable:
            approval = _approved_source_record(options, spec.key)
            if not approval:
                skipped[spec.key] = 'downloads not approved in rights-evidence.json'
                continue
            approvals[spec.key] = approval
        missing = missing_settings(spec, config)
        if missing and not (spec.role == 'torrent' and config.get('access') == 'jackett'):
            skipped[spec.key] = 'needs ' + ', '.join(missing).replace('_', ' ') + ' in Settings → Sources'
            continue
        runnable.append(spec)
    result = {'rows': [], 'errors': [], 'skipped': skipped, 'next_pages': {}, 'notices': [],
              'searched': [spec.label for spec in runnable]}
    if not runnable:
        return result
    pool = ThreadPoolExecutor(max_workers=min(8, len(runnable)), thread_name_prefix='source-connector')
    try:
        futures = [(spec, pool.submit(search_one, spec.key, query, category, pages.get(spec.key, 1), configs,
                                      opener)) for spec in runnable]
        deadline = time.monotonic() + timeout
        for spec, future in futures:
            try:
                page = future.result(timeout=max(0.1, deadline - time.monotonic()))
            except FutureTimeout:
                result['errors'].append(f'{spec.label}: the site took longer than {timeout} seconds to answer. '
                                        'Other sources are shown; try it again later.')
                if health:
                    health(spec.key, False, 'search timed out')
                continue
            except SourceError as exc:
                result['errors'].append(str(exc))
                if health:
                    health(spec.key, False, str(exc))
                continue
            except Exception as exc:  # a connector bug must not break the other sources
                logger.exception('%s search failed', spec.label)
                result['errors'].append(f'{spec.label}: unexpected problem reading results ({exc}).')
                if health:
                    health(spec.key, False, str(exc))
                continue
            if health:
                health(spec.key, True, None)
            if page.notice:
                result['notices'].append(page.notice)
            if page.has_more and spec.pages:
                result['next_pages'][spec.key] = page.page + 1
            for row in page.rows:
                row['connector_row'] = True
                row.setdefault('category', category)
                if spec.file_capable:
                    if options.get('rights_evidence_path'):
                        row['rights_evidence_path'] = options['rights_evidence_path']
                    if approvals[spec.key].get('rights_work_key'):
                        row['rights_work_key'] = approvals[spec.key]['rights_work_key']
                if category == 'Anime' and spec.role != 'metadata':
                    # Category search keeps only rows that look like anime; these were searched as anime.
                    row.setdefault('catalog_metadata', {}).setdefault('genres', ['anime'])
                result['rows'].append(row)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    torrents = [row for row in result['rows'] if row.get('transfer') == 'torrent']
    others = [row for row in result['rows'] if row.get('transfer') != 'torrent']
    result['rows'] = dedupe_releases(torrents) + others
    return result


# --------------------------------------------------------------------------- downloads
def resolve_download(item, configs=None, opener=None):
    """Ask the source for the real file/magnet of a queued row. Returns the updated item."""
    key = item.get('resolve_connector')
    if not key or key not in _RESOLVERS:
        return item
    spec = CONNECTORS[key]
    config = dict((configs or {}).get(key) or {})
    if spec.default_base or key == 'z-library':
        config['base'] = clean_base(spec.label, config.get('base'), spec.default_base)
    resolved = _RESOLVERS[key](spec, item, config, opener)
    updated = dict(item, **{k: v for k, v in resolved.items() if k != 'url'})
    updated['resolved_url'] = resolved['url']
    if resolved['url'].startswith('magnet:'):
        updated['url_resolved_magnet'] = True
    return updated
