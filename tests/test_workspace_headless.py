"""The GUI, the CLI and scheduled runs prepare and use a data folder the same way."""
import contextlib
import datetime as dt
import io
import json
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from media_intake import __main__ as cli
from media_intake import source_health, workspace
from media_intake.core import connect, list_items
from media_intake.watched import add_watch, get_setting, process_due_watches, set_setting

FIXTURES = json.loads((Path(__file__).parent / 'fixtures' / 'source_catalog' / 'queue_items.json').read_text(encoding='utf-8'))


def run_cli(*argv):
    out = io.StringIO()
    with patch.object(sys, 'argv', ['media_intake', *argv]), contextlib.redirect_stdout(out):
        cli.main()
    return json.loads(out.getvalue())


def import_gui():
    """The real module where Tk exists; a stand-in for it on a machine without Tk."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        for name in ('tkinter', 'tkinter.filedialog', 'tkinter.messagebox', 'tkinter.simpledialog', 'tkinter.ttk'):
            module = types.ModuleType(name)
            module.__getattr__ = lambda attr, _m=MagicMock(): getattr(_m, attr)
            sys.modules.setdefault(name, module)
        sys.modules['tkinter'].filedialog = sys.modules['tkinter.filedialog']
        sys.modules['tkinter'].messagebox = sys.modules['tkinter.messagebox']
        sys.modules['tkinter'].simpledialog = sys.modules['tkinter.simpledialog']
        sys.modules['tkinter'].ttk = sys.modules['tkinter.ttk']
    from media_intake import gui
    return gui


class Var:
    """Stands in for a Tk variable: only .get()/.set() are used by the settings code."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'data'
        self.addCleanup(logging.getLogger(workspace.LOG_NAME).handlers.clear)


class FirstRunTests(Tmp):
    def test_clean_workspace_is_fully_prepared(self):
        with closing(workspace.ensure_workspace(self.root)) as db:
            self.assertTrue((self.root / 'intake.sqlite3').is_file())
            self.assertTrue((self.root / 'rights-evidence.json').is_file())
            self.assertEqual(get_setting(db, 'media_graph_change_cursor'), '0')
            logging.getLogger(workspace.LOG_NAME).info('first run')
            for handler in logging.getLogger(workspace.LOG_NAME).handlers:
                handler.flush()
            self.assertIn('first run', (self.root / 'logs' / 'media-intake.log').read_text(encoding='utf-8'))

    def test_existing_workspace_is_left_as_it_was(self):
        with closing(workspace.ensure_workspace(self.root)) as db:
            set_setting(db, 'library_folder', str(self.root / 'lib'))
            set_setting(db, 'media_graph_change_cursor', 41)
            row_id = add_watch(db, 'book', 'Kept Book', 'Writer')
        with closing(workspace.ensure_workspace(self.root)) as db:
            self.assertEqual(get_setting(db, 'media_graph_change_cursor'), '41')
            self.assertEqual(workspace.library_folder(db, self.root), str(self.root / 'lib'))
            self.assertEqual(db.execute('SELECT title FROM watched_items WHERE id=?', (row_id,)).fetchone()['title'], 'Kept Book')

    def test_a_database_from_an_older_release_is_upgraded_not_rejected(self):
        self.root.mkdir(parents=True)
        old = sqlite3.connect(self.root / 'intake.sqlite3')
        old.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL, '
                    "state TEXT NOT NULL DEFAULT 'queued', file_path TEXT, sha256 TEXT, error TEXT, "
                    'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        older = dict(FIXTURES['legacy_metadata'], url='https://yts.mx/torrent/download/ABC123')
        old.execute('INSERT INTO items(url, metadata) VALUES (?, ?)', (older['url'], json.dumps(older)))
        old.commit()
        old.close()
        with closing(workspace.ensure_workspace(self.root)) as db:
            self.assertEqual(len(list_items(db)), 1)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(items)')}
            self.assertTrue({'progress_bytes', 'lease_until'} <= columns)

    def test_the_cli_prepares_the_folder_like_the_window(self):
        self.assertEqual(run_cli('--root', str(self.root), 'list'), [])
        self.assertTrue((self.root / 'logs').is_dir())
        self.assertTrue((self.root / 'rights-evidence.json').is_file())
        with closing(connect(self.root)) as db:
            self.assertEqual(get_setting(db, 'media_graph_change_cursor'), '0')


class WatchSourceParityTests(Tmp):
    def _gui_options(self, db, environ):
        gui = import_gui()
        text = get_setting(db, 'watch_irc_configs', workspace.default_irc_config())
        fake = types.SimpleNamespace(
            db=db, rights_evidence_path=self.root / 'rights-evidence.json',
            watch_libgen=Var(get_setting(db, 'watch_libgen', '1') == '1'),
            watch_libgen_base=Var(get_setting(db, 'watch_libgen_base', '')),
            watch_annas=Var(get_setting(db, 'watch_annas', '1') == '1'),
            watch_annas_base=Var(get_setting(db, 'watch_annas_base', '')),
            watch_opds_feeds=Var(get_setting(db, 'watch_opds_feeds', '')),
            watch_catalogs=Var(get_setting(db, 'watch_catalogs', '')),
            watch_irc_configs=Var(text),
            watch_comic_mirror_base=Var(get_setting(db, 'watch_comic_mirror_base', '')),
            taddy_user_id=environ.get('TADDY_USER_ID', ''), taddy_api_key=environ.get('TADDY_API_KEY', ''),
            _saved_watch_sources={})
        fake.irc_sources = gui.MediaIntakeApp._load_irc_sources(fake)
        return gui.MediaIntakeApp._watch_sources(fake)

    def _assert_same(self, db, environ):
        headless = workspace.watch_source_options(db, self.root, environ)
        window = self._gui_options(db, environ)
        extras = set(headless) - set(window)
        self.assertEqual(extras, {'tmdb_api_key', 'youtube_api_key', 'igdb_client_id', 'igdb_client_secret'} | (
            {'comicvine_api_key'} & extras))
        self.assertEqual({key: headless[key] for key in window}, window)

    def test_clean_workspace_scans_the_same_sources_as_the_window(self):
        with closing(workspace.ensure_workspace(self.root)) as db:
            self._assert_same(db, {})

    def test_configured_workspace_scans_the_same_sources_as_the_window(self):
        with closing(workspace.ensure_workspace(self.root)) as db:
            for key, value in (('watch_libgen', '0'), ('watch_annas_base', 'https://annas.example.test'),
                               ('watch_opds_feeds', 'https://a.example/opds;https://b.example/opds'),
                               ('watch_catalogs', 'one.json;two.json'), ('watch_comic_mirror_base', 'https://mirror.example')):
                set_setting(db, key, value)
            add_watch(db, 'webfiction', 'A Serial', metadata={'serial': {'feed_url': 'https://serial.example/feed.xml'}})
            add_watch(db, 'comic', 'A Comic', metadata={'comic': {'feed_url': 'https://comic.example/rss'}})
            self._assert_same(db, {'TADDY_USER_ID': 'u', 'TADDY_API_KEY': 'k'})
            flags = workspace.watch_source_options(db, self.root, {})
            self.assertTrue(flags['webfiction_rss_configured'] and flags['comic_rss_configured'])
            self.assertFalse(flags['libgen'])

    def test_credentials_come_from_the_environment_only(self):
        with closing(workspace.ensure_workspace(self.root)) as db:
            options = workspace.watch_source_options(db, self.root, {'TMDB_API_KEY': ' t ', 'COMICVINE_API_KEY': 'c'})
            self.assertEqual((options['tmdb_api_key'], options['comicvine_api_key']), ('t', 'c'))
            self.assertNotIn('comicvine_api_key', workspace.watch_source_options(db, self.root, {}))
            self.assertIsNone(db.execute("SELECT 1 FROM gui_settings WHERE value LIKE '%TMDB%'").fetchone())


class ScheduledRunTests(Tmp):
    def test_process_watches_on_a_clean_root_needs_no_window(self):
        result = run_cli('--root', str(self.root), 'process-watches')
        self.assertEqual(result, {'library': str(self.root / 'downloads'), 'checked': 0})

    def test_process_watches_uses_the_same_options_library_and_force_flag(self):
        with closing(workspace.ensure_workspace(self.root)) as db:
            set_setting(db, 'library_folder', str(self.root / 'books'))
            set_setting(db, 'watch_libgen', '0')
        with patch('media_intake.watched.process_due_watches', return_value=3) as scan:
            result = run_cli('--root', str(self.root), 'process-watches', '--force')
        self.assertEqual(result, {'library': str(self.root / 'books'), 'checked': 3})
        args, kwargs = scan.call_args
        self.assertEqual((args[1], kwargs['force']), (str(self.root / 'books'), True))
        self.assertFalse(kwargs['source_options']['libgen'])
        self.assertEqual(run_cli('--root', str(self.root), 'process-watches', '--library', str(self.root / 'x')).get('library'), str(self.root / 'x'))

    def _due_watch(self, db):
        title = 'Example Episode S01E01'
        add_watch(db, 'video_episode', title, release_date=dt.date.today().isoformat(),
                  metadata={'entity_type': 'work', 'video_subtype': 'episode', 'catalog_status': 'released', 'sources': {}})
        return {'title': title, 'format': '.torrent', 'source': 'https://nyaa.si/view/1',
                'url': 'https://nyaa.si/download/1.torrent', 'provider': 'Nyaa'}

    def test_a_scheduled_scan_queues_a_catalogued_source_with_its_identity(self):
        with closing(workspace.ensure_workspace(self.root)) as db:
            candidate = self._due_watch(db)
            done = str(self.root / 'downloads' / 'Example Episode.mkv')
            with patch('media_intake.watched._search_candidates', return_value=([candidate], [])), \
                    patch('media_intake.watched.download', return_value={'file_path': done}):
                checked = process_due_watches(db, str(self.root / 'downloads'), force=True,
                                              source_options=workspace.watch_source_options(db, self.root, {}))
            self.assertEqual(checked, 1)
            self.assertEqual(db.execute('SELECT state, file_path FROM watched_items').fetchone()['state'], 'downloaded')
            metadata = json.loads(list_items(db)[0]['metadata'])
            self.assertEqual((metadata['source_key'], metadata['source_name']), ('nyaa', 'Nyaa'))

    def test_a_failing_download_keeps_its_error_and_records_health_under_the_canonical_key(self):
        from media_intake.core import download
        with closing(workspace.ensure_workspace(self.root)) as db:
            candidate = self._due_watch(db)
            with patch('media_intake.watched._search_candidates', return_value=([candidate], [])), \
                    patch('media_intake.watched.download', side_effect=lambda d, i, *a, **k: download(d, i, *a, **k)), \
                    patch('media_intake.core._download', side_effect=ValueError('HTTP 403')):
                process_due_watches(db, str(self.root / 'downloads'), force=True)
            watch = db.execute('SELECT state, last_error FROM watched_items').fetchone()
            self.assertNotEqual(watch['state'], 'downloaded')
            self.assertIn('HTTP 403', watch['last_error'] or '')
            self.assertEqual(source_health.summary(db, 'nyaa')['last_error'], 'HTTP 403')
            self.assertEqual([s['source_key'] for s in source_health.summaries(db)], ['nyaa'])


if __name__ == '__main__':
    unittest.main()
