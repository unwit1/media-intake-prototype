"""First-run setup and saved settings shared by the GUI, the CLI and scheduled runs.

The desktop app used to be the only place that prepared a data folder (logging,
the graph-change cursor, the IRC source list, the per-source options for watch
scans). A command-line or scheduled run on a clean ``--root`` skipped all of
that and behaved differently. Everything the two entry points have in common now
lives here, and both call it.
"""
import json
import logging
import os
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
import urllib.parse

from .core import connect
from .rights_evidence import ensure_rights_evidence
from .watched import get_setting, set_setting

LOG_NAME = 'media_intake'
EXAMPLE_IRC_CONFIG = Path(__file__).resolve().parent.parent / 'examples' / 'irc-search.json'


def configure_logging(root):
    """Rotating diagnostics log at ``<root>/logs/media-intake.log`` (the file the GUI's Open logs shows)."""
    folder = Path(root) / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    log_file = folder / 'media-intake.log'
    logger = logging.getLogger(LOG_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=4, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    logger.addHandler(handler)
    return logger, log_file


def ensure_workspace(root, *, logging_enabled=True):
    """Open (creating on first use) the data folder at ``root`` and return its database.

    A clean folder and a long-used one end up in the same state: tables and
    indexes, the media-graph change cursor, the settings file, and the log."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ensure_rights_evidence(root / 'rights-evidence.json')
    if logging_enabled:
        configure_logging(root)
    db = connect(root)
    if get_setting(db, 'media_graph_change_cursor') is None:
        latest = db.execute('SELECT COALESCE(MAX(id),0) AS id FROM media_graph_changes').fetchone()['id']
        set_setting(db, 'media_graph_change_cursor', latest)
    return db


def library_folder(db, root):
    return get_setting(db, 'library_folder', str(Path(root) / 'downloads')).strip() or str(Path(root) / 'downloads')


def split_paths(value):
    return [part.strip() for part in str(value or '').split(';') if part.strip()]


def default_irc_config():
    return str(EXAMPLE_IRC_CONFIG) if EXAMPLE_IRC_CONFIG.is_file() else ''


def load_irc_sources(db, irc_configs_text):
    """Saved IRC sources, after importing legacy JSON config paths and applying presets.

    Returns ``(sources, remaining_config_text)``; the text is what is left of
    ``irc_configs_text`` once each config that could be imported is removed."""
    from .discovery import CATEGORIES
    try:
        sources = json.loads(get_setting(db, 'watch_irc_sources', '[]'))
    except (TypeError, ValueError):
        sources = []
    sources = [source for source in sources if isinstance(source, dict)] if isinstance(sources, list) else []
    remaining, migrated = [], False
    for config_path in split_paths(irc_configs_text):
        try:
            config = json.loads(Path(config_path).read_text(encoding='utf-8'))
            if not isinstance(config, dict) or not config.get('server') or not config.get('channel'):
                raise ValueError('IRC config needs server and channel')
            sources.append({
                'id': uuid.uuid4().hex,
                'name': Path(config_path).stem or config['server'],
                'server': config['server'], 'port': int(config.get('port', 6697)),
                'tls': bool(config.get('tls', True)),
                'verify_tls': bool(config.get('verify_tls', True)),
                'nick': config.get('nick') or 'MediaIntake',
                'channel': config['channel'],
                'password_env': config.get('password_env') or '',
                'timeout': int(config.get('timeout', 180)),
                'categories': list(CATEGORIES), 'enabled': True,
            })
            migrated = True
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            remaining.append(config_path)
    remaining_text = ';'.join(remaining) if migrated else str(irc_configs_text or '')
    if migrated:
        set_setting(db, 'watch_irc_configs', remaining_text)
    from .irc import ensure_ebook_presets
    try:
        applied = json.loads(get_setting(db, 'irc_presets_applied', '[]'))
    except (TypeError, ValueError):
        applied = []
    sources, applied, _added = ensure_ebook_presets(sources, applied)
    set_setting(db, 'irc_presets_applied', json.dumps(applied))
    set_setting(db, 'watch_irc_sources', json.dumps(sources, ensure_ascii=False))
    return sources, remaining_text


def feed_flags(db, taddy_configured=False):
    """Which kinds of feed the saved watches already use (decides which feed connectors a scan enables)."""
    comic_rss_configured = False
    webfiction_rss_configured = False
    for row in db.execute('SELECT kind,source_url,metadata FROM watched_items'):
        try:
            metadata = json.loads(row['metadata'] or '{}')
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        comic = metadata.get('comic') or {}
        serial = metadata.get('serial') or {}
        if not isinstance(comic, dict):
            comic = {}
        if not isinstance(serial, dict):
            serial = {}
        comic_feed = str(comic.get('feed_url') or row['source_url'] or '')
        comic_host = (urllib.parse.urlsplit(comic_feed).hostname or '').casefold()
        if comic.get('taddy_id') or comic.get('sss_url') or comic_host.endswith('taddy.org'):
            taddy_configured = True
        elif (row['kind'] in {'comic', 'comic_issue', 'manga', 'webcomic', 'webtoon'}
              and comic_feed and not comic.get('mangadex_id')
              and comic_host not in {'mangadex.org', 'www.mangadex.org'}):
            comic_rss_configured = True
        serial_feed = str(serial.get('feed_url') or row['source_url'] or '')
        serial_host = (urllib.parse.urlsplit(serial_feed).hostname or '').casefold()
        if row['kind'] == 'webfiction' and serial_feed and not serial_host.endswith('novelarchive.cc'):
            webfiction_rss_configured = True
    return {'taddy_configured': taddy_configured, 'comic_rss_configured': comic_rss_configured,
            'webfiction_rss_configured': webfiction_rss_configured}


def api_key_options(environ=None):
    """Metadata API credentials a scan uses, from the environment (never stored in the database)."""
    environ = os.environ if environ is None else environ
    get = lambda name: str(environ.get(name, '')).strip()
    options = {'tmdb_api_key': get('TMDB_API_KEY'), 'youtube_api_key': get('YOUTUBE_API_KEY'),
               'igdb_client_id': get('IGDB_CLIENT_ID'), 'igdb_client_secret': get('IGDB_CLIENT_SECRET')}
    if get('COMICVINE_API_KEY'):
        options['comicvine_api_key'] = get('COMICVINE_API_KEY')
    return options


def watch_source_options(db, root, environ=None):
    """The ``source_options`` a watch scan runs with, built from the folder's saved settings.

    This is what the GUI's scan uses when nothing has been edited in the window,
    so a command-line or scheduled scan of the same folder searches the same sources."""
    environ = os.environ if environ is None else environ
    sources, remaining = load_irc_sources(db, get_setting(db, 'watch_irc_configs', default_irc_config()))
    taddy = bool(str(environ.get('TADDY_USER_ID', '')).strip() and str(environ.get('TADDY_API_KEY', '')).strip())
    options = {
        'rights_evidence_path': str(Path(root) / 'rights-evidence.json'),
        'libgen': get_setting(db, 'watch_libgen', '1') == '1',
        'libgen_base': get_setting(db, 'watch_libgen_base', '').strip(),
        'annas': get_setting(db, 'watch_annas', '1') == '1',
        'annas_base': get_setting(db, 'watch_annas_base', '').strip(),
        'opds_feeds': split_paths(get_setting(db, 'watch_opds_feeds', '')),
        'catalogs': split_paths(get_setting(db, 'watch_catalogs', '')),
        'irc_configs': split_paths(remaining),
        'irc_sources': list(sources),
        'comic_mirror_base': get_setting(db, 'watch_comic_mirror_base', '').strip(),
        **feed_flags(db, taddy),
    }
    options.update(api_key_options(environ))
    return options
