"""Small Tkinter desktop front end for the media intake queue."""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import sqlite3
import traceback
import uuid
import datetime as dt
import tkinter as tk
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from logging.handlers import RotatingFileHandler

from .core import connect, download, enqueue, list_items
from .comics import (list_comicvine_issues, mangadex_id_from_url, search_anilist,
                     search_comicvine, search_mangadex, search_taddy)
from .comic_sources import search_getcomics, search_public_domain_comic_mirror
from .download_connectors import (ANIME_DOWNLOAD_ADAPTERS,
                                  GENERAL_DOWNLOAD_ADAPTERS,
                                  resolve_softarchive_downloads,
                                  resolve_site_downloads,
                                  formats_for_category,
                                  resolve_meawfy_downloads,
                                  search_fosshub_foss, search_github_foss,
                                  search_internet_archive, search_meawfy,
                                  search_google_cse_browser,
                                  search_registered_download_site,
                                  search_softarchive, search_sourceforge_foss)
from .discovery import (CATEGORIES, METADATA_CONNECTORS, SOURCE_KEY_LABELS,
                       connected_download_sources,
                       get_download_priority, save_download_priority)
from .fmhy import candidates
from .irc import discover_irc_channels, enqueue_irc
from .library_catalog import parse_goodreads_export, standardize_author, standardize_title
from .epub_conversion import convert_library_books_to_epub
from .kindle import (deliver_to_kindle, find_kindle_documents_folder,
                     get_kindle_email_targets,
                     get_kindle_smtp_password,
                     get_local_secret, set_local_secret,
                     get_setting as get_kindle_setting,
                     pending_kindle_delivery_ids,
                     retry_pending_kindle_deliveries,
                     set_kindle_smtp_password,
                     set_setting as set_kindle_setting)
from .media_catalogs import (search_lnori, search_mangaupdates, search_novelarchive,
                             search_ranobedb)
from .rights_evidence import (effective_work_rights, ensure_rights_evidence,
                              load_rights_evidence, set_work_rights,
                              source_download_rights)
from .serial import discover_feed, enqueue_serial
from .sources import (resolve_detail, search_forum_threads,
                      search_google_books_metadata, search_openlibrary_metadata,
                      search_site)
from .watched import (_apply_wikidata_watch_policy, add_from_goodreads, add_from_goodreads_metadata,
                      add_watch, assign_watch_profile, get_setting, import_goodreads_list, list_watches,
                      fetch_goodreads, parse_goodreads_html, parse_goodreads_search_html,
                      process_due_watches, remove_watch, set_watch_release_date,
                      search_book_metadata, set_setting, sync_watch_profile_settings)
from .media_graph import (DEFAULT_WATCH_POLICY, capture_graph_snapshot, get_media_graph, get_watch_policy,
                          list_graph_reviews, list_media_changes, resolve_graph_review,
                          set_reviewed_media_release_date, set_reviewed_media_series_index,
                          set_reviewed_media_status,
                          set_reviewed_release_date, set_watch_policy)
from .media_resolver import (refresh_graph as refresh_media_graph,
                             resolve as resolve_media_query)
from .connectors import CONNECTORS as SOURCE_CONNECTORS, connectors_for, load_all_configs, missing_settings
from .sources_ui import (ReleasePicker, SourcesSettingsPanel, connector_options, format_summary,
                         queue_rows, single_source_choices, source_summary, spec_for_choice,
                         wants_release_picker)
from .wikidata import import_wikidata_entity, search_wikidata


class MediaIntakeApp:
    def __init__(self, root, data_root=None):
        self.root = root
        self.root.title('Media Intake')
        self.root.geometry('1050x680')
        self.data_root = Path(data_root or _default_data_root())
        self.rights_evidence_path = ensure_rights_evidence(self.data_root / 'rights-evidence.json')
        self._configure_logging()
        self.db = connect(self.data_root)
        if get_setting(self.db, 'media_graph_change_cursor') is None:
            latest_change = self.db.execute('SELECT COALESCE(MAX(id),0) AS id FROM media_graph_changes').fetchone()['id']
            set_setting(self.db, 'media_graph_change_cursor', latest_change)
        self.default_irc_config = Path(__file__).resolve().parent.parent / 'examples' / 'irc-search.json'
        irc_default = str(self.default_irc_config) if self.default_irc_config.is_file() else ''
        self.library_folder = tk.StringVar(value=get_setting(self.db, 'library_folder', str(self.data_root / 'downloads')))
        self.kindle_auto_send = tk.BooleanVar(
            value=get_kindle_setting(self.db, 'kindle_auto_send', '0') == '1')
        self.kindle_delivery_method = tk.StringVar(
            value=get_kindle_setting(self.db, 'kindle_delivery_method', 'USB').upper())
        self.kindle_target_format = tk.StringVar(
            value=get_kindle_setting(self.db, 'kindle_target_format', 'AZW3').upper())
        saved_email_format = get_kindle_setting(self.db, 'kindle_email_format', 'EPUB').upper()
        self.kindle_email_format = tk.StringVar(
            value='AZW3 (experimental)' if saved_email_format == 'AZW3'
            else 'EPUB (recommended)')
        self.kindle_device_path = tk.StringVar(
            value=get_kindle_setting(self.db, 'kindle_device_path', ''))
        configured_converter = get_kindle_setting(self.db, 'kindle_converter_path', '')
        if configured_converter and not Path(configured_converter).name.casefold().startswith('kindling-cli'):
            configured_converter = ''
        self.kindle_converter_path = tk.StringVar(
            value=configured_converter)
        self.kindle_email_to = tk.StringVar(value=get_kindle_setting(self.db, 'kindle_email_to', ''))
        self.kindle_email_targets = get_kindle_email_targets(self.db)
        self.kindle_target_rows = []
        self.kindle_targets_container = None
        self.kindle_smtp_host = tk.StringVar(value=get_kindle_setting(self.db, 'kindle_smtp_host', ''))
        self.kindle_smtp_port = tk.StringVar(value=get_kindle_setting(self.db, 'kindle_smtp_port', '587'))
        self.kindle_smtp_security = tk.StringVar(
            value=get_kindle_setting(self.db, 'kindle_smtp_security', 'STARTTLS').upper())
        self.kindle_smtp_from = tk.StringVar(value=get_kindle_setting(self.db, 'kindle_smtp_from', ''))
        self.kindle_smtp_username = tk.StringVar(
            value=get_kindle_setting(self.db, 'kindle_smtp_username', ''))
        self.kindle_smtp_password = tk.StringVar(value='')
        self.kindle_settings_status = tk.StringVar(value='')
        self._reload_kindle_smtp_password()
        self._kindle_retry_running = False
        self.results = []
        self.queue_rows = []
        self.library_rows = []
        self.library_display_by_id = {}
        self.library_filter = {'kind': 'all'}
        self.library_profiles = self._load_library_profiles()
        self.active_profile_id = tk.StringVar(
            value=get_setting(self.db, 'active_library_profile_id', '') or '')
        self.active_profile_name = tk.StringVar(value='No profile')
        self.library_profile_view = tk.StringVar(value='ALL')
        self.active_profile_box = None
        self.library_profile_box = None
        self.profile_view = None
        self.profile_name_entry = None
        self.profile_folder_entry = None
        self.profile_auto_email = tk.BooleanVar(value=False)
        self.profile_kindle_targets = None
        self._editing_profile_id = None
        self.library_tree_filters = {}
        self._download_batch_active = False
        self._seen_kindle_delivery_notifications = self._current_kindle_delivery_notification_ids()
        self.status = tk.StringVar(value='Ready')
        self.query = tk.StringVar()
        self.source = tk.StringVar(value='All connected in category')
        self.search_category = tk.StringVar(value='Books')
        self.search_category_info = tk.StringVar()
        self.settings_category = None
        self.priority_view = None
        self.priority_status = tk.StringVar(value='')
        self.site_base = tk.StringVar()
        self.title = tk.StringVar()
        self.author = tk.StringVar()
        self.url = tk.StringVar()
        self.format = tk.StringVar(value='.epub')
        self.watch_url = tk.StringVar()
        self.watch_title = tk.StringVar()
        self.watch_author = tk.StringVar()
        self.watch_release_date = tk.StringVar()
        self.watch_kind = tk.StringVar(value='Auto')
        self.watch_feed_url = tk.StringVar()
        self.watch_announcement_feeds = tk.StringVar()
        self.watch_selector = tk.StringVar(value='article,main')
        self.watch_comic_profile = {}
        self.watch_lightnovel_profile = {}
        self.watch_comic_mirror_base = tk.StringVar(value=get_setting(self.db, 'watch_comic_mirror_base', ''))
        self.comicvine_api_key = os.environ.get('COMICVINE_API_KEY', '').strip()
        self.metron_api_token = os.environ.get('METRON_API_TOKEN', '').strip()
        self.comicvine_key_entry = tk.StringVar(value=self.comicvine_api_key)
        self.metron_token_entry = tk.StringVar(value=self.metron_api_token)
        self.taddy_user_id = os.environ.get('TADDY_USER_ID', '').strip()
        self.taddy_api_key = os.environ.get('TADDY_API_KEY', '').strip()
        self.tmdb_api_key = os.environ.get('TMDB_API_KEY', '').strip()
        self.youtube_api_key = os.environ.get('YOUTUBE_API_KEY', '').strip()
        self.igdb_client_id = os.environ.get('IGDB_CLIENT_ID', '').strip()
        self.igdb_client_secret = os.environ.get('IGDB_CLIENT_SECRET', '').strip()
        self.tmdb_key_entry = tk.StringVar(value=self.tmdb_api_key)
        self.youtube_key_entry = tk.StringVar(value=self.youtube_api_key)
        self.igdb_client_id_entry = tk.StringVar(value=self.igdb_client_id)
        self.igdb_client_secret_entry = tk.StringVar(value=self.igdb_client_secret)
        try:
            saved_hardcover_token = get_local_secret(self.db, 'hardcover_api_token')
        except Exception:
            saved_hardcover_token = ''
            self.logger.warning('Saved Hardcover API token could not be unlocked; re-enter it in Settings.')
        self.hardcover_api_token = saved_hardcover_token
        self.hardcover_api_token_entry = tk.StringVar(value=saved_hardcover_token)
        self.google_books_api_key_entry = tk.StringVar(value=self._load_google_books_key())
        self.metadata_credential_status = tk.StringVar(value='')
        self.watch_goodreads_profile = {}
        self.watch_libgen = tk.BooleanVar(value=get_setting(self.db, 'watch_libgen', '1') == '1')
        self.watch_annas = tk.BooleanVar(value=get_setting(self.db, 'watch_annas', '1') == '1')
        self.watch_libgen_base = tk.StringVar(value=get_setting(self.db, 'watch_libgen_base', ''))
        self.watch_annas_base = tk.StringVar(value=get_setting(self.db, 'watch_annas_base', ''))
        self.watch_opds_feeds = tk.StringVar(value=get_setting(self.db, 'watch_opds_feeds', ''))
        self.watch_catalogs = tk.StringVar(value=get_setting(self.db, 'watch_catalogs', ''))
        self.watch_irc_configs = tk.StringVar(value=get_setting(self.db, 'watch_irc_configs', irc_default))
        self.irc_sources = self._load_irc_sources()
        self.irc_sources_view = None
        self.watch_rows = []
        self.watch_scan_running = False
        self._saved_watch_sources = None
        self._forum_download_watch = False
        self._forum_download_folders = []
        self._forum_download_baseline = {}
        self._forum_download_stability = {}
        self._embedded_browser_process = None
        self._shortlink_browser_process = None
        self._shortlink_captures_seen = None
        self._forum_import_in_progress = False
        self._forum_query = ''
        self._forum_last_query = ''
        self._goodreads_pending_search_query = ''
        self._goodreads_pending_search_url = ''
        self._title_suggestion_after = None
        self._title_suggestion_query = ''
        self._title_suggestion_popup = None
        self._title_suggestion_list = None
        self._title_suggestion_results = []
        self._book_metadata_cache = {}
        self._scroll_regions = []
        self._watch_add_dialog = None
        self.external_dl_settings = self._load_external_download_settings()
        self._recover_interrupted_downloads()
        self._shortlink_target_item = None
        self._clipboard_last = None
        self._clipboard_offer = None
        self.clipboard_capture = tk.BooleanVar(value=str(get_setting(self.db, 'clipboard_capture') or '') == '1')
        self._build()
        self.root.bind_all('<MouseWheel>', self._scroll_mousewheel, add='+')
        self.root.bind_all('<Button-4>', lambda event: self._scroll_mousewheel(event, units=-3), add='+')
        self.root.bind_all('<Button-5>', lambda event: self._scroll_mousewheel(event, units=3), add='+')
        self.refresh_queue()
        self.refresh_watches()
        self.root.after(1000, self._poll_queue)
        self.root.after(1500, self._schedule_watch_scan)
        self._running_book_batches = set()
        self.root.after(2500, self._resume_book_batches)
        self.root.after(2000, self._poll_clipboard)
        self._autostart_ids = set()
        self._podcast_check_running = False
        self.root.after(15_000, self._schedule_podcast_checks)

    def _configure_logging(self):
        self.log_folder = self.data_root / 'logs'
        self.log_folder.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_folder / 'media-intake.log'
        self.logger = logging.getLogger('media_intake')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            handler.close()
        handler = RotatingFileHandler(self.log_file, maxBytes=2_000_000,
                                      backupCount=4, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(name)s: %(message)s'))
        self.logger.addHandler(handler)

    @staticmethod
    def _consume_graph_changes(db):
        cursor_text = get_setting(db, 'media_graph_change_cursor', '0') or '0'
        try:
            cursor_id = int(cursor_text)
        except (TypeError, ValueError):
            cursor_id = 0
        rows = db.execute('''SELECT c.*,e.title AS entity_title,
                r.relationship_type,t.title AS target_title
            FROM media_graph_changes AS c
            LEFT JOIN media_entities AS e ON e.id=c.entity_id
            LEFT JOIN media_relationships AS r ON r.id=c.relationship_id
            LEFT JOIN media_entities AS t ON t.id=r.target_entity_id
            WHERE c.id>? ORDER BY c.id LIMIT 1000''', (cursor_id,)).fetchall()
        if not rows:
            return []
        events = []
        labels = {'entity_added': 'new work', 'relationship_added': 'new relationship',
                  'release_added': 'new release', 'release_updated': 'release update'}
        for row in rows:
            change_type = str(row['change_type'] or '')
            label = labels.get(change_type)
            after = {}
            try:
                after = json.loads(row['after_json']) if row['after_json'] else {}
            except (TypeError, ValueError):
                pass
            if change_type == 'entity_updated':
                before = {}
                try:
                    before = json.loads(row['before_json']) if row['before_json'] else {}
                except (TypeError, ValueError):
                    pass
                changed = [field for field in ('title', 'release_date', 'status')
                           if before.get(field) != after.get(field)]
                if changed:
                    label = 'metadata update'
            if not label:
                continue
            title = (after.get('metadata', {}).get('title') if isinstance(after, dict) else None)
            title = title or row['target_title'] or row['entity_title'] or 'Watched media'
            events.append((label, str(title)))
        set_setting(db, 'media_graph_change_cursor', rows[-1]['id'])
        return list(dict.fromkeys(events))

    @staticmethod
    def _graph_change_summary(events):
        if not events:
            return ''
        shown = ', '.join(f'{label}: {title}' for label, title in events[:3])
        extra = f' (+{len(events) - 3} more)' if len(events) > 3 else ''
        return f'Graph updates: {shown}{extra}'

    def _build(self):
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='Data folder:').pack(side='left')
        ttk.Entry(toolbar, textvariable=tk.StringVar(value=str(self.data_root)), width=58, state='readonly').pack(side='left', padx=6)
        ttk.Button(toolbar, text='Choose…', command=self.choose_root).pack(side='left')
        ttk.Button(toolbar, text='Open logs', command=self.open_logs).pack(side='left', padx=6)
        ttk.Button(toolbar, text='Back up database…', command=self.backup_database).pack(side='left')
        rights_actions = ttk.Frame(toolbar)
        rights_actions.pack(side='right', padx=(8, 0))
        ttk.Button(rights_actions, text='Open rights file', command=self.open_rights_evidence).pack(side='left')
        ttk.Button(rights_actions, text='Edit rights evidence', command=self.edit_watch_rights).pack(side='left', padx=(6, 0))
        ttk.Label(toolbar, textvariable=self.status).pack(side='right')

        tabs = ttk.Notebook(self.root)
        self.main_tabs = tabs
        tabs.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        search_tab, search_content = self._scrollable_frame(tabs, padding=8)
        queue_tab, queue_content = self._scrollable_frame(tabs, padding=8)
        library_tab, library_content = self._scrollable_frame(tabs, padding=8)
        watched_tab, watched_content = self._scrollable_frame(tabs, padding=8)
        webcomics_tab, webcomics_content = self._scrollable_frame(tabs, padding=8)
        podcasts_tab, podcasts_content = self._scrollable_frame(tabs, padding=8)
        settings_tab, settings_content = self._scrollable_frame(tabs, padding=8)
        tabs.add(search_tab, text='Search and add')
        tabs.add(queue_tab, text='Queue')
        tabs.add(library_tab, text='Library')
        tabs.add(watched_tab, text='Watched')
        tabs.add(webcomics_tab, text='Webcomics')
        tabs.add(podcasts_tab, text='Podcasts')
        tabs.add(settings_tab, text='Settings')
        self._build_search(search_content)
        self._build_queue(queue_content)
        self._build_library(library_content)
        self._build_watched(watched_content)
        from .webcomics.ui import WebcomicsPanel
        self.webcomics_panel = WebcomicsPanel(self, webcomics_content)
        from .podcasts_ui import PodcastsPanel
        self.podcasts_panel = PodcastsPanel(self, podcasts_content)
        self._build_settings(settings_content)
        tabs.bind('<<NotebookTabChanged>>', self._on_main_tab_changed)

    def _load_library_profiles(self):
        try:
            profiles = json.loads(get_setting(self.db, 'library_profiles', '[]') or '[]')
        except (TypeError, ValueError):
            profiles = []
        return [dict(profile) for profile in profiles
                if isinstance(profile, dict) and profile.get('id') and profile.get('name')]

    def _scrollable_frame(self, parent, padding=0):
        """Return a frame whose contents can scroll vertically in a small window."""
        outer = ttk.Frame(parent)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        content = ttk.Frame(canvas, padding=padding)
        window = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _event, area=canvas:
                     area.configure(scrollregion=area.bbox('all')))
        canvas.bind('<Configure>', lambda event, area=canvas, item=window:
                    area.itemconfigure(item, width=event.width))
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self._scroll_regions.append((canvas, content))
        return outer, content

    def _scroll_mousewheel(self, event, units=None):
        """Route the wheel to the innermost scrollable page under the pointer."""
        widget_path = getattr(event.widget, '_w', '')
        matches = [(len(content._w), canvas) for canvas, content in self._scroll_regions
                   if widget_path == content._w or widget_path.startswith(content._w + '.')]
        if not matches:
            return
        canvas = max(matches, key=lambda item: item[0])[1]
        amount = units
        if amount is None:
            amount = -int(event.delta / 120) if event.delta else 0
        if amount:
            canvas.yview_scroll(amount, 'units')
            return 'break'

    def _on_main_tab_changed(self, _event=None):
        if not self.main_tabs:
            return
        selected = self.main_tabs.select()
        if selected:
            label = self.main_tabs.tab(selected, 'text')
            if label == 'Settings':
                self._render_download_priority()
            elif label == 'Library':
                self.refresh_library()
            elif label == 'Webcomics':
                self.webcomics_panel.refresh()

    def choose_root(self):
        chosen = filedialog.askdirectory(initialdir=str(self.data_root), title='Choose Media Intake data folder')
        if not chosen: return
        self.data_root = Path(chosen)
        self.rights_evidence_path = ensure_rights_evidence(self.data_root / 'rights-evidence.json')
        self._configure_logging()
        self.db.close()
        self.db = connect(self.data_root)
        if get_setting(self.db, 'media_graph_change_cursor') is None:
            latest_change = self.db.execute('SELECT COALESCE(MAX(id),0) AS id FROM media_graph_changes').fetchone()['id']
            set_setting(self.db, 'media_graph_change_cursor', latest_change)
        self._saved_watch_sources = None
        self.library_profiles = self._load_library_profiles()
        self.active_profile_id.set(get_setting(self.db, 'active_library_profile_id', '') or '')
        try:
            self.hardcover_api_token = get_local_secret(self.db, 'hardcover_api_token')
        except Exception:
            self.hardcover_api_token = ''
            self.logger.warning('Saved Hardcover API token could not be unlocked for the selected data folder.')
        self.hardcover_api_token_entry.set(self.hardcover_api_token)
        self.google_books_api_key_entry.set(self._load_google_books_key())
        self._render_library_profiles()
        self._refresh_profile_selectors()
        self.library_folder.set(get_setting(self.db, 'library_folder', str(self.data_root / 'downloads')))
        self.kindle_auto_send.set(get_kindle_setting(self.db, 'kindle_auto_send', '0') == '1')
        self.kindle_delivery_method.set(
            get_kindle_setting(self.db, 'kindle_delivery_method', 'USB').upper())
        self.kindle_target_format.set(get_kindle_setting(self.db, 'kindle_target_format', 'AZW3').upper())
        saved_email_format = get_kindle_setting(self.db, 'kindle_email_format', 'EPUB').upper()
        self.kindle_email_format.set(
            'AZW3 (experimental)' if saved_email_format == 'AZW3'
            else 'EPUB (recommended)')
        self.kindle_device_path.set(get_kindle_setting(self.db, 'kindle_device_path', ''))
        configured_converter = get_kindle_setting(self.db, 'kindle_converter_path', '')
        if configured_converter and not Path(configured_converter).name.casefold().startswith('kindling-cli'):
            configured_converter = ''
        self.kindle_converter_path.set(configured_converter)
        self.kindle_email_to.set(get_kindle_setting(self.db, 'kindle_email_to', ''))
        self.kindle_email_targets = get_kindle_email_targets(self.db)
        if self.kindle_targets_container is not None:
            self._render_kindle_email_targets()
        self.kindle_smtp_host.set(get_kindle_setting(self.db, 'kindle_smtp_host', ''))
        self.kindle_smtp_port.set(get_kindle_setting(self.db, 'kindle_smtp_port', '587'))
        self.kindle_smtp_security.set(
            get_kindle_setting(self.db, 'kindle_smtp_security', 'STARTTLS').upper())
        self.kindle_smtp_from.set(get_kindle_setting(self.db, 'kindle_smtp_from', ''))
        self.kindle_smtp_username.set(get_kindle_setting(self.db, 'kindle_smtp_username', ''))
        self._reload_kindle_smtp_password()
        self._seen_kindle_delivery_notifications = self._current_kindle_delivery_notification_ids()
        self.watch_libgen.set(get_setting(self.db, 'watch_libgen', '1') == '1')
        self.watch_annas.set(get_setting(self.db, 'watch_annas', '1') == '1')
        self.watch_libgen_base.set(get_setting(self.db, 'watch_libgen_base', ''))
        self.watch_annas_base.set(get_setting(self.db, 'watch_annas_base', ''))
        self.watch_opds_feeds.set(get_setting(self.db, 'watch_opds_feeds', ''))
        self.watch_catalogs.set(get_setting(self.db, 'watch_catalogs', ''))
        self.watch_comic_mirror_base.set(get_setting(self.db, 'watch_comic_mirror_base', ''))
        irc_default = str(self.default_irc_config) if self.default_irc_config.is_file() else ''
        self.watch_irc_configs.set(get_setting(self.db, 'watch_irc_configs', irc_default))
        self.irc_sources = self._load_irc_sources()
        self._render_irc_sources()
        self.refresh_queue()
        self.refresh_watches()

    def _build_search(self, parent):
        controls = ttk.Frame(parent)
        controls.pack(fill='x')
        ttk.Label(controls, text='Search').pack(side='left')
        entry = ttk.Entry(controls, textvariable=self.query, width=42)
        entry.pack(side='left', padx=6)
        entry.bind('<Return>', lambda _event: self.search())
        ttk.Label(controls, text='Category').pack(side='left')
        category = ttk.Combobox(controls, textvariable=self.search_category, state='readonly', width=15,
                                values=('Auto-detect',) + CATEGORIES)
        category.pack(side='left', padx=6)
        category.bind('<<ComboboxSelected>>', lambda _event: self._update_search_category_info())
        ttk.Button(controls, text='Search all connected sources', command=self.search).pack(side='left')
        advanced = ttk.Frame(parent)
        advanced.pack(fill='x', pady=(4, 0))
        ttk.Label(advanced, text='Search one source (metadata or download)').pack(side='left')
        ttk.Combobox(advanced, textvariable=self.source, state='readonly', width=27,
                     values=['All connected in category', 'Jackett (torrents)', 'Open Library (metadata)', 'Google Books (metadata)', 'FOSShub (metadata)', 'SourceForge (FOSS download)', 'GitHub (FOSS download)', 'Internet Archive (download)', 'Meawfy (download)', 'SoftArchive (download)', 'Anime Tosho (download)', 'Tokyo Insider (download)', 'AnimeOut (download)', 'NoobSubs (download)', 'Chauthanh (download)', 'LibGen (download)', "Anna's Archive (download)",
                             'Google CSE (browser search)', 'MediaFireTrend (download)', 'XDCC.eu (download)', 'WarezOmen (download)',
                             'MyAbandonware (download)', 'OldGamesDownload (download)', 'AbandonwareGames (download)', 'GOG Revived (download)', 'AnkerGames (download)',
                             'FMHY directory', 'Snahp forum threads', 'NovelArchive (metadata)', 'LNORI (metadata)',
                             'RanobeDB (metadata)', 'MangaUpdates (metadata)', 'GetComics (download)',
                             'Public-domain comic mirror (download)'] + single_source_choices()).pack(side='left', padx=6)
        ttk.Entry(advanced, textvariable=self.site_base, width=28).pack(side='left', padx=4)
        ttk.Button(advanced, text='Snahp browser helper', command=self.open_forum_helper).pack(side='left', padx=6)
        ttk.Label(parent, textvariable=self.search_category_info,
                  wraplength=920, justify='left').pack(anchor='w', pady=(4, 6))
        self._update_search_category_info()
        ttk.Label(parent, text='Optional site base URL (LibGen, Anna’s Archive, or a separate public-domain comic mirror)').pack(anchor='w', pady=(4, 6))

        columns = ('category', 'title', 'author', 'series', 'series_order', 'format', 'source')
        results_frame = ttk.Frame(parent)
        results_frame.pack(fill='both', expand=True)
        self.results_view = ttk.Treeview(results_frame, columns=columns, show='headings', selectmode='extended')
        for col, label, width in (
                ('category', 'Category', 90), ('title', 'Title', 205),
                ('author', 'Author', 135), ('series', 'Series', 150),
                ('series_order', 'Series order', 80),
                ('format', 'Format', 60), ('source', 'Source', 190)):
            self.results_view.heading(col, text=label)
            self.results_view.column(col, width=width, anchor='w')
        results_y = ttk.Scrollbar(results_frame, orient='vertical', command=self.results_view.yview)
        results_x = ttk.Scrollbar(parent, orient='horizontal', command=self.results_view.xview)
        self.results_view.configure(yscrollcommand=results_y.set, xscrollcommand=results_x.set)
        self.results_view.pack(side='left', fill='both', expand=True)
        results_y.pack(side='right', fill='y')
        results_x.pack(fill='x')
        self.results_view.bind('<Double-1>', lambda _event: self.activate_result())
        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Queue selection / entire series',
                   command=self.queue_result).pack(side='left')
        ttk.Button(buttons, text='Watch selected (all series books)',
                   command=self.add_selected_search_to_watched).pack(side='left', padx=6)
        ttk.Button(buttons, text='Open selected link', command=self.open_selected_link).pack(side='left')
        ttk.Button(buttons, text='Send to download manager',
                   command=self.send_selected_results_to_downloader).pack(side='left', padx=6)
        ttk.Button(buttons, text='Choose release…', command=self.choose_release).pack(side='left')
        self.load_more_button = ttk.Button(buttons, text='Load more results', command=self.load_more_results,
                                           state='disabled')
        self.load_more_button.pack(side='left', padx=6)

        add = ttk.LabelFrame(parent, text='Add a direct file')
        add.pack(fill='x', pady=(12, 0))
        for label, variable, width in (('Title', self.title, 22), ('Author', self.author, 20), ('URL', self.url, 46), ('Format', self.format, 8)):
            ttk.Label(add, text=label).pack(side='left', padx=(6, 2))
            ttk.Entry(add, textvariable=variable, width=width).pack(side='left')
        ttk.Button(add, text='Add to queue', command=self.add_direct).pack(side='left', padx=6)
        ttk.Button(add, text='Resolve shortlink', command=self.resolve_shortlink_url).pack(side='left')

    def _build_queue(self, parent):
        columns = ('id', 'title', 'author', 'format', 'state', 'source', 'progress', 'file')
        queue_frame = ttk.Frame(parent)
        queue_frame.pack(fill='both', expand=True)
        self.queue_view = ttk.Treeview(queue_frame, columns=columns, show='headings', selectmode='browse')
        for col, width in (('id', 45), ('title', 175), ('author', 125), ('format', 60),
                           ('state', 85), ('source', 190), ('progress', 245), ('file', 220)):
            self.queue_view.heading(col, text='Source / connector' if col == 'source' else col.title())
            self.queue_view.column(col, width=width, anchor='w')
        queue_y = ttk.Scrollbar(queue_frame, orient='vertical', command=self.queue_view.yview)
        queue_x = ttk.Scrollbar(parent, orient='horizontal', command=self.queue_view.xview)
        self.queue_view.configure(yscrollcommand=queue_y.set, xscrollcommand=queue_x.set)
        self.queue_view.pack(side='left', fill='both', expand=True)
        queue_y.pack(side='right', fill='y')
        queue_x.pack(fill='x')
        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Refresh', command=self.refresh_queue).pack(side='left')
        ttk.Button(buttons, text='Download / resume selected', command=self.download_selected).pack(side='left', padx=6)
        ttk.Button(buttons, text='Pause', command=self.pause_selected_download).pack(side='left')
        ttk.Button(buttons, text='Cancel', command=self.cancel_selected_download).pack(side='left', padx=6)
        ttk.Button(buttons, text='Pause all', command=self.pause_all_downloads).pack(side='left')
        ttk.Button(buttons, text='Stop seeding', command=self.stop_selected_seeding).pack(side='left', padx=6)
        ttk.Button(buttons, text='Source health', command=self.open_source_health).pack(side='left')
        self.queue_view.bind('<Button-3>', self._queue_context_menu)
        self.download_batch_button = ttk.Button(
            buttons, text='Download queued',
            command=self.download_queued)
        self.download_batch_button.pack(side='left')
        ttk.Button(buttons, text='Send to download manager',
                   command=self.send_queue_item_to_downloader).pack(side='left', padx=6)

    def _build_library(self, parent):
        profile_row = ttk.Frame(parent)
        profile_row.pack(fill='x', pady=(0, 6))
        ttk.Label(profile_row, text='Profile view').pack(side='left')
        self.library_profile_box = ttk.Combobox(
            profile_row, textvariable=self.library_profile_view,
            values=('ALL',), state='readonly', width=28)
        self.library_profile_box.pack(side='left', padx=6)
        self.library_profile_box.bind('<<ComboboxSelected>>',
                                      self._on_library_profile_view_selected)
        ttk.Label(profile_row, text='ALL shows every profile in the directory tree.').pack(
            side='left', padx=6)
        ttk.Label(parent, text=('Books are saved as local EPUB copies when conversion is supported; original files are kept. '
                                'Browse by author, series, or Goodreads list below.' )).pack(
                                    anchor='w', pady=(0, 6))
        browser = ttk.Panedwindow(parent, orient='horizontal')
        browser.pack(fill='both', expand=True)
        navigation = ttk.Frame(browser, width=250)
        listing = ttk.Frame(browser)
        browser.add(navigation, weight=1)
        browser.add(listing, weight=5)
        ttk.Label(navigation, text='Browse by author or Goodreads list',
                  font=('TkDefaultFont', 9, 'bold')).pack(anchor='w', pady=(0, 4))
        self.library_author_tree = ttk.Treeview(
            navigation, show='tree', selectmode='browse', height=20)
        navigation_scroll = ttk.Scrollbar(
            navigation, orient='vertical', command=self.library_author_tree.yview)
        self.library_author_tree.configure(yscrollcommand=navigation_scroll.set)
        self.library_author_tree.pack(side='left', fill='both', expand=True)
        navigation_scroll.pack(side='right', fill='y')
        self.library_author_tree.bind('<<TreeviewSelect>>', self._on_library_folder_select)

        columns = ('title', 'author', 'series', 'series_order', 'format',
                   'source', 'state', 'kindle', 'file')
        library_table = ttk.Frame(listing)
        library_table.pack(fill='both', expand=True)
        self.library_view = ttk.Treeview(library_table, columns=columns, show='headings',
                                         selectmode='browse')
        for column, label, width in (
                ('title', 'Title', 210), ('author', 'Author', 155),
                ('series', 'Series', 180), ('series_order', 'Series order', 82),
                ('format', 'Format / EPUB', 125), ('source', 'Downloaded from', 180),
                ('state', 'State', 80), ('kindle', 'Kindle delivery', 190),
                ('file', 'Library file', 280)):
            self.library_view.heading(column, text=label)
            self.library_view.column(column, width=width, anchor='w')
        library_scroll = ttk.Scrollbar(listing, orient='horizontal',
                                       command=self.library_view.xview)
        library_y = ttk.Scrollbar(library_table, orient='vertical', command=self.library_view.yview)
        self.library_view.configure(xscrollcommand=library_scroll.set,
                                    yscrollcommand=library_y.set)
        self.library_view.pack(side='left', fill='both', expand=True)
        library_y.pack(side='right', fill='y')
        library_scroll.pack(fill='x')
        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Refresh', command=self.refresh_library).pack(side='left')
        ttk.Button(buttons, text='Import Goodreads list (CSV)',
                   command=self.import_goodreads_list_csv).pack(side='left', padx=6)
        ttk.Button(buttons, text='Convert books to EPUB',
                   command=self.convert_library_to_epub).pack(side='left')
        ttk.Button(buttons, text='Open file', command=self.open_selected_library_file).pack(side='left', padx=6)
        ttk.Button(buttons, text='Open containing folder',
                   command=self.open_selected_library_folder).pack(side='left')
        ttk.Button(buttons, text='Send selected to Kindle',
                   command=self.send_selected_to_kindle).pack(side='left', padx=6)
        self.library_view.bind('<Button-3>', self._library_context_menu)

    def _build_settings(self, parent):
        pages = ttk.Notebook(parent)
        pages.pack(fill='both', expand=True)
        priority_page, priority_content = self._scrollable_frame(pages, padding=8)
        irc_page, irc_content = self._scrollable_frame(pages, padding=8)
        metadata_page, metadata_content = self._scrollable_frame(pages, padding=8)
        kindle_page, kindle_content = self._scrollable_frame(pages, padding=8)
        profiles_page, profiles_content = self._scrollable_frame(pages, padding=8)
        pages.add(priority_page, text='Download priority')
        pages.add(irc_page, text='IRC sources')
        pages.add(metadata_page, text='Metadata APIs')
        pages.add(kindle_page, text='Kindle')
        pages.add(profiles_page, text='Profiles')
        external_page, external_content = self._scrollable_frame(pages, padding=8)
        pages.add(external_page, text='Downloads')
        sources_page, sources_content = self._scrollable_frame(pages, padding=8)
        pages.add(sources_page, text='Sources')
        self.sources_panel = SourcesSettingsPanel(self, sources_content)
        self._build_external_download_settings(external_content)
        self._build_irc_sources(irc_content)
        self._build_metadata_api_settings(metadata_content)
        self._build_kindle_settings(kindle_content)
        self._build_profile_settings(profiles_content)

        ttk.Label(priority_content, text='Download source priority',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        ttk.Label(
            priority_content,
            text=('Choose a media category, then move its connected download sources into priority order. '
                  'Metadata repositories are searched separately and never need download approval here. '
                  'Configured IRC servers stay at the top and follow their server order. Books prefer EPUB, then PDF, '
                  'AZW3, MOBI, TXT, and audiobook formats; source priority breaks ties.'),
            wraplength=900, justify='left').pack(anchor='w', pady=(4, 10))

        body = ttk.Frame(priority_content)
        body.pack(fill='both', expand=True)
        category_frame = ttk.LabelFrame(body, text='Media categories')
        category_frame.pack(side='left', fill='y', padx=(0, 10))
        self.settings_category = tk.Listbox(category_frame, exportselection=False, width=22,
                                            height=min(len(CATEGORIES), 14))
        self.settings_category.pack(fill='y', expand=True, padx=6, pady=6)
        for category in CATEGORIES:
            self.settings_category.insert('end', category)
        self.settings_category.bind('<<ListboxSelect>>', self._select_priority_category)

        sources_frame = ttk.LabelFrame(body, text='Connected download sources (top is highest priority)')
        sources_frame.pack(side='left', fill='both', expand=True)
        self.priority_view = ttk.Treeview(
            sources_frame, columns=('rank', 'source'), show='headings', selectmode='browse', height=12)
        self.priority_view.heading('rank', text='Priority')
        self.priority_view.heading('source', text='Download source')
        self.priority_view.column('rank', width=75, stretch=False, anchor='center')
        self.priority_view.column('source', width=400, anchor='w')
        self.priority_view.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        controls = ttk.Frame(sources_frame)
        controls.pack(side='left', fill='y', padx=8, pady=6)
        ttk.Button(controls, text='Move up', command=lambda: self._move_priority(-1)).pack(fill='x', pady=(0, 5))
        ttk.Button(controls, text='Move down', command=lambda: self._move_priority(1)).pack(fill='x', pady=5)
        ttk.Button(controls, text='Reset order', command=self._reset_priority).pack(fill='x', pady=(12, 5))
        ttk.Label(priority_content, textvariable=self.priority_status).pack(anchor='w', pady=(8, 0))
        self.settings_category.selection_set(0)
        self._render_download_priority()

    def _build_profile_settings(self, parent):
        ttk.Label(parent, text='Library profiles',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        ttk.Label(parent, text=(
            'Each profile has its own library folder and can send completed books to selected Kindle addresses. '
            'Choose the active profile at the top of Watched before adding titles, series, authors, or lists.'),
            wraplength=850, justify='left').pack(anchor='w', pady=(4, 10))
        body = ttk.Frame(parent)
        body.pack(fill='both', expand=True)
        list_frame = ttk.LabelFrame(body, text='Profiles')
        list_frame.pack(side='left', fill='both', expand=True, padx=(0, 10))
        columns = ('name', 'folder', 'delivery')
        self.profile_view = ttk.Treeview(list_frame, columns=columns, show='headings',
                                         selectmode='browse', height=10)
        for column, label, width in (('name', 'Name', 130), ('folder', 'Library folder', 310),
                                     ('delivery', 'Kindle delivery', 160)):
            self.profile_view.heading(column, text=label)
            self.profile_view.column(column, width=width, anchor='w')
        profile_scroll = ttk.Scrollbar(list_frame, orient='vertical',
                                       command=self.profile_view.yview)
        self.profile_view.configure(yscrollcommand=profile_scroll.set)
        self.profile_view.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        profile_scroll.pack(side='right', fill='y', padx=(0, 6), pady=6)
        self.profile_view.bind('<<TreeviewSelect>>', self._load_selected_profile)

        form = ttk.LabelFrame(body, text='Profile settings', padding=8)
        form.pack(side='left', fill='both', expand=True)
        self.profile_name_entry = tk.StringVar()
        self.profile_folder_entry = tk.StringVar()
        name_row = ttk.Frame(form)
        name_row.pack(fill='x', pady=3)
        ttk.Label(name_row, text='Profile name', width=18, anchor='w').pack(side='left')
        ttk.Entry(name_row, textvariable=self.profile_name_entry).pack(side='left', fill='x', expand=True)
        folder_row = ttk.Frame(form)
        folder_row.pack(fill='x', pady=3)
        ttk.Label(folder_row, text='Library folder', width=18, anchor='w').pack(side='left')
        ttk.Entry(folder_row, textvariable=self.profile_folder_entry).pack(side='left', fill='x', expand=True)
        ttk.Button(folder_row, text='Choose', command=self._choose_profile_folder).pack(side='left', padx=(5, 0))
        ttk.Checkbutton(form, text='Automatically email completed books to these Kindles',
                        variable=self.profile_auto_email).pack(anchor='w', pady=(8, 3))
        ttk.Label(form, text='Kindles (select one or more)').pack(anchor='w')
        target_frame = ttk.Frame(form)
        target_frame.pack(fill='both', expand=True, pady=(3, 6))
        self.profile_kindle_targets = tk.Listbox(target_frame, selectmode='multiple',
                                                 exportselection=False, height=6)
        targets_scroll = ttk.Scrollbar(target_frame, orient='vertical',
                                       command=self.profile_kindle_targets.yview)
        self.profile_kindle_targets.configure(yscrollcommand=targets_scroll.set)
        self.profile_kindle_targets.pack(side='left', fill='both', expand=True)
        targets_scroll.pack(side='right', fill='y')
        self._render_profile_kindle_choices()
        buttons = ttk.Frame(form)
        buttons.pack(fill='x')
        ttk.Button(buttons, text='New profile', command=self._new_library_profile).pack(side='left')
        ttk.Button(buttons, text='Save profile', command=self._save_library_profile).pack(side='left', padx=5)
        ttk.Button(buttons, text='Remove profile', command=self._remove_library_profile).pack(side='left')
        self._render_library_profiles()
        self._refresh_profile_selectors()

    def _render_profile_kindle_choices(self, selected_emails=()):
        target_list = self.profile_kindle_targets
        if target_list is None or not target_list.winfo_exists():
            return
        target_list.delete(0, 'end')
        self._profile_target_options = []
        for target in self.kindle_email_targets:
            if not target.get('email'):
                continue
            self._profile_target_options.append(dict(target))
            label = f"{target.get('name') or 'Kindle'} — {target['email']}"
            target_list.insert('end', label)
            if target['email'].casefold() in {str(email).casefold() for email in selected_emails}:
                target_list.selection_set('end')

    def _render_library_profiles(self):
        if not self.profile_view or not self.profile_view.winfo_exists():
            return
        for row in self.profile_view.get_children():
            self.profile_view.delete(row)
        for profile in self.library_profiles:
            targets = profile.get('kindle_targets') or []
            delivery = f"Email to {len(targets)} Kindle(s)" if profile.get('auto_email') else 'Manual delivery'
            self.profile_view.insert('', 'end', iid=str(profile['id']), values=(
                profile['name'], profile.get('library_folder') or '', delivery))

    def _refresh_profile_selectors(self):
        names = [profile['name'] for profile in self.library_profiles]
        if self.active_profile_box and self.active_profile_box.winfo_exists():
            self.active_profile_box.configure(values=('No profile', *names))
        active = next((profile for profile in self.library_profiles
                       if str(profile['id']) == str(self.active_profile_id.get())), None)
        self.active_profile_name.set(active['name'] if active else 'No profile')
        if not active:
            self.active_profile_id.set('')
        if self.library_profile_box and self.library_profile_box.winfo_exists():
            self.library_profile_box.configure(values=('ALL', *names))
        if self.library_profile_view.get() not in {'ALL', *names}:
            self.library_profile_view.set('ALL')

    def _on_active_profile_selected(self, _event=None):
        name = self.active_profile_name.get()
        profile = next((item for item in self.library_profiles if item['name'] == name), None)
        self.active_profile_id.set(str(profile['id']) if profile else '')
        set_setting(self.db, 'active_library_profile_id', self.active_profile_id.get())

    def _on_library_profile_view_selected(self, _event=None):
        self.library_filter = {'kind': 'all'}
        self._render_library_navigation()
        self._render_library_rows()

    def _new_library_profile(self):
        self._editing_profile_id = None
        self.profile_name_entry.set('')
        self.profile_folder_entry.set(self.library_folder.get().strip())
        self.profile_auto_email.set(False)
        self._render_profile_kindle_choices()
        if self.profile_view:
            self.profile_view.selection_remove(self.profile_view.selection())

    def _choose_profile_folder(self):
        selected = filedialog.askdirectory(
            initialdir=self.profile_folder_entry.get().strip() or self.library_folder.get().strip() or str(self.data_root),
            title='Choose this profile library folder')
        if selected:
            self.profile_folder_entry.set(selected)

    def _load_selected_profile(self, _event=None):
        selected = self.profile_view.selection() if self.profile_view else ()
        if not selected:
            return
        profile = next((item for item in self.library_profiles
                        if str(item['id']) == selected[0]), None)
        if not profile:
            return
        self._editing_profile_id = str(profile['id'])
        self.profile_name_entry.set(profile.get('name') or '')
        self.profile_folder_entry.set(profile.get('library_folder') or '')
        self.profile_auto_email.set(bool(profile.get('auto_email')))
        targets = profile.get('kindle_targets') or []
        emails = [item.get('email') if isinstance(item, dict) else item for item in targets]
        self._render_profile_kindle_choices(emails)

    def _save_library_profile(self):
        name = self.profile_name_entry.get().strip()
        folder = self.profile_folder_entry.get().strip()
        if not name:
            messagebox.showinfo('Library profile', 'Enter a profile name.')
            return
        if not folder:
            messagebox.showinfo('Library profile', 'Choose a library folder for this profile.')
            return
        if any(profile['name'].casefold() == name.casefold() and
               str(profile['id']) != str(self._editing_profile_id)
               for profile in self.library_profiles):
            messagebox.showinfo('Library profile', 'Profile names must be unique.')
            return
        selected = self.profile_kindle_targets.curselection() if self.profile_kindle_targets else ()
        targets = [dict(self._profile_target_options[index]) for index in selected]
        if self.profile_auto_email.get() and not targets:
            messagebox.showinfo('Library profile', 'Select at least one Kindle for automatic email delivery.')
            return
        profile_id = self._editing_profile_id or uuid.uuid4().hex
        profile = {'id': profile_id, 'name': name, 'library_folder': folder,
                   'auto_email': bool(self.profile_auto_email.get()),
                   'kindle_targets': targets}
        existing = next((index for index, item in enumerate(self.library_profiles)
                         if str(item['id']) == str(profile_id)), None)
        if existing is None:
            self.library_profiles.append(profile)
        else:
            self.library_profiles[existing] = profile
        set_setting(self.db, 'library_profiles', json.dumps(self.library_profiles, ensure_ascii=False))
        sync_watch_profile_settings(self.db)
        self._editing_profile_id = profile_id
        self._render_library_profiles()
        self._refresh_profile_selectors()
        self.refresh_watches()
        self.refresh_library()

    def _remove_library_profile(self):
        selected = self.profile_view.selection() if self.profile_view else ()
        profile_id = selected[0] if selected else self._editing_profile_id
        if not profile_id:
            return
        profile = next((item for item in self.library_profiles
                        if str(item['id']) == str(profile_id)), None)
        if not profile or not messagebox.askyesno(
                'Remove library profile',
                f"Remove '{profile['name']}' from profile settings? Downloaded files stay in their folders."):
            return
        self.library_profiles = [item for item in self.library_profiles
                                 if str(item['id']) != str(profile_id)]
        for row in self.db.execute('SELECT id,metadata FROM watched_items').fetchall():
            try:
                metadata = json.loads(row['metadata'] or '{}')
            except (TypeError, ValueError):
                continue
            ids = metadata.get('library_profile_ids') or []
            if not isinstance(ids, list):
                ids = [ids]
            remaining = [value for value in ids if str(value) != str(profile_id)]
            if remaining != ids:
                metadata['library_profile_ids'] = remaining
                self.db.execute('UPDATE watched_items SET metadata=? WHERE id=?',
                                (json.dumps(metadata, ensure_ascii=False), row['id']))
        self.db.commit()
        set_setting(self.db, 'library_profiles', json.dumps(self.library_profiles, ensure_ascii=False))
        if self.active_profile_id.get() == str(profile_id):
            self.active_profile_id.set('')
            set_setting(self.db, 'active_library_profile_id', '')
        sync_watch_profile_settings(self.db)
        self._editing_profile_id = None
        self._new_library_profile()
        self._render_library_profiles()
        self._refresh_profile_selectors()
        self.refresh_watches()
        self.refresh_library()

    def _build_kindle_settings(self, parent):
        ttk.Label(parent, text='Kindle delivery',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        ttk.Label(
            parent,
            text=('Automatic delivery uses the selected route after a book download completes. USB copies to a '
                  'mounted Kindle Documents folder. Email submits a personal document to your device address; '
                  'Amazon may take time to process it, and this app cannot confirm final library delivery.'),
            wraplength=900, justify='left').pack(anchor='w', pady=(4, 10))
        method_row = ttk.Frame(parent)
        method_row.pack(fill='x', pady=(0, 4))
        ttk.Label(method_row, text='Delivery method').pack(side='left')
        method_box = ttk.Combobox(
            method_row, textvariable=self.kindle_delivery_method,
            values=('USB', 'Email'), state='readonly', width=14)
        method_box.pack(side='left', padx=8)
        method_box.bind('<<ComboboxSelected>>', lambda _event: self._save_kindle_settings())
        ttk.Checkbutton(
            parent, text='Automatically send completed book downloads using this method',
            variable=self.kindle_auto_send, command=self._save_kindle_settings).pack(anchor='w')

        format_row = ttk.Frame(parent)
        format_row.pack(fill='x', pady=(10, 4))
        ttk.Label(format_row, text='USB Kindle format').pack(side='left')
        format_box = ttk.Combobox(format_row, textvariable=self.kindle_target_format,
                                  values=('AZW3', 'MOBI'), state='readonly', width=12)
        format_box.pack(side='left', padx=8)
        format_box.bind('<<ComboboxSelected>>', lambda _event: self._save_kindle_settings())
        ttk.Label(format_row, text='Used only for USB copies.').pack(side='left')

        device_row = ttk.Frame(parent)
        device_row.pack(fill='x', pady=4)
        ttk.Label(device_row, text='Kindle folder').pack(side='left')
        ttk.Entry(device_row, textvariable=self.kindle_device_path, width=72).pack(
            side='left', padx=8, fill='x', expand=True)
        device_entry = device_row.winfo_children()[1]
        device_entry.bind('<FocusOut>', lambda _event: self._save_kindle_settings())
        ttk.Button(device_row, text='Detect Kindle', command=self.detect_kindle_device).pack(side='left')
        ttk.Button(device_row, text='Choose folder', command=self.choose_kindle_folder).pack(side='left', padx=(6, 0))

        converter_row = ttk.Frame(parent)
        converter_row.pack(fill='x', pady=4)
        ttk.Label(converter_row, text='AZW3 converter').pack(side='left')
        ttk.Entry(converter_row, textvariable=self.kindle_converter_path, width=72).pack(
            side='left', padx=8, fill='x', expand=True)
        converter_entry = converter_row.winfo_children()[1]
        converter_entry.bind('<FocusOut>', lambda _event: self._save_kindle_settings())
        ttk.Button(converter_row, text='Browse', command=self.choose_kindle_converter).pack(side='left')
        ttk.Label(parent, text=('The app bundles Kindling for EPUB/OPF to AZW3 conversion. Leave the path blank '
                                'to use the bundled tool, or select kindling-cli.exe. Windows Kindles are detected by their volume label; '
                                'leave Kindle folder blank for automatic detection or select the device root / Documents folder.'),
                  wraplength=900, justify='left').pack(anchor='w', pady=(6, 4))

        email = ttk.LabelFrame(parent, text='Send to Kindle email (wireless)', padding=6)
        email.pack(fill='x', pady=(8, 2))
        ttk.Label(
            email,
            text=('Add each Kindle address to Amazon\'s Approved Personal Document Email List. Each device gets '
                  'a separate email with the same attachment. EPUB is recommended; Amazon converts EPUB. '
                  'AZW3 email is experimental and may not be imported by Amazon. PDF and TXT are also supported. '
                  'Amazon allows 50 MB per email. The SMTP '
                  'password is encrypted for this Windows user before it is saved.'),
            wraplength=880, justify='left').pack(anchor='w', pady=(0, 4))

        email_format_row = ttk.Frame(email)
        email_format_row.pack(fill='x', pady=(2, 5))
        ttk.Label(email_format_row, text='Email attachment format').pack(side='left')
        email_format_box = ttk.Combobox(
            email_format_row, textvariable=self.kindle_email_format,
            values=('EPUB (recommended)', 'AZW3 (experimental)'),
            state='readonly', width=24)
        email_format_box.pack(side='left', padx=8)
        email_format_box.bind('<<ComboboxSelected>>',
                              lambda _event: self._save_kindle_settings())

        def add_email_field(container, label, variable, width=52, secret=False):
            row = ttk.Frame(container)
            row.pack(fill='x', pady=2)
            ttk.Label(row, text=label, width=19, anchor='w').pack(side='left')
            entry = ttk.Entry(row, textvariable=variable, width=width,
                              show='*' if secret else '')
            entry.pack(side='left', fill='x', expand=True)
            entry.bind('<FocusOut>', lambda _event: self._save_kindle_settings())
            return entry

        ttk.Label(email, text='Kindle devices', font=('TkDefaultFont', 9, 'bold')).pack(
            anchor='w', pady=(2, 1))
        target_header = ttk.Frame(email)
        target_header.pack(fill='x')
        ttk.Label(target_header, text='Device name', width=22, anchor='w').pack(side='left')
        ttk.Label(target_header, text='Kindle email address', anchor='w').pack(side='left')
        self.kindle_targets_container = ttk.Frame(email)
        self.kindle_targets_container.pack(fill='x')
        self._render_kindle_email_targets()
        ttk.Button(email, text='Add another Kindle',
                   command=self._add_kindle_email_target).pack(anchor='w', pady=(2, 5))

        smtp_row = ttk.Frame(email)
        smtp_row.pack(fill='x', pady=2)
        ttk.Label(smtp_row, text='SMTP server', width=19, anchor='w').pack(side='left')
        host_entry = ttk.Entry(smtp_row, textvariable=self.kindle_smtp_host, width=36)
        host_entry.pack(side='left', fill='x', expand=True)
        host_entry.bind('<FocusOut>', lambda _event: self._save_kindle_settings())
        ttk.Label(smtp_row, text='Port').pack(side='left', padx=(8, 3))
        port_entry = ttk.Entry(smtp_row, textvariable=self.kindle_smtp_port, width=7)
        port_entry.pack(side='left')
        port_entry.bind('<FocusOut>', lambda _event: self._save_kindle_settings())
        security_box = ttk.Combobox(
            smtp_row, textvariable=self.kindle_smtp_security,
            values=('STARTTLS', 'SSL'), state='readonly', width=10)
        security_box.pack(side='left', padx=(8, 0))
        security_box.bind('<<ComboboxSelected>>', lambda _event: self._save_kindle_settings())

        add_email_field(email, 'Sender email', self.kindle_smtp_from)
        add_email_field(email, 'SMTP username', self.kindle_smtp_username)
        add_email_field(email, 'SMTP password / app password', self.kindle_smtp_password,
                        secret=True)
        email_buttons = ttk.Frame(email)
        email_buttons.pack(fill='x', pady=(4, 0))
        ttk.Button(email_buttons, text='Save email settings',
                   command=self._save_kindle_settings).pack(side='left')
        ttk.Button(email_buttons, text='Amazon email setup help',
                   command=self._open_kindle_email_help).pack(side='left', padx=6)
        ttk.Label(email_buttons, text='Use your mail provider\'s SMTP credentials; app passwords may be required.')\
            .pack(side='left', padx=6)
        ttk.Label(parent, textvariable=self.kindle_settings_status).pack(anchor='w', pady=(4, 0))

    def _reload_kindle_smtp_password(self):
        try:
            self.kindle_smtp_password.set(get_kindle_smtp_password(self.db))
        except Exception:
            self.kindle_smtp_password.set('')
            self.logger.warning('Saved Kindle SMTP password could not be unlocked; re-enter it in Settings.')
            self.kindle_settings_status.set(
                'Saved email password could not be unlocked; re-enter it for this Windows user.')

    def _render_kindle_email_targets(self):
        if self.kindle_targets_container is None:
            return
        for child in self.kindle_targets_container.winfo_children():
            child.destroy()
        self.kindle_target_rows = []
        targets = self.kindle_email_targets or [{'name': 'Kindle 1', 'email': ''}]
        for target in targets:
            self._add_kindle_email_target(
                target.get('name', ''), target.get('email', ''), save=False)
        if getattr(self, 'profile_kindle_targets', None) is not None:
            selected = self.profile_kindle_targets.curselection()
            emails = [self._profile_target_options[index]['email']
                      for index in selected
                      if index < len(getattr(self, '_profile_target_options', []))]
            self._render_profile_kindle_choices(emails)

    def _add_kindle_email_target(self, name='', email='', save=False):
        if self.kindle_targets_container is None:
            return
        row = ttk.Frame(self.kindle_targets_container)
        row.pack(fill='x', pady=1)
        name_var = tk.StringVar(value=name)
        email_var = tk.StringVar(value=email)
        name_entry = ttk.Entry(row, textvariable=name_var, width=20)
        name_entry.pack(side='left', padx=(0, 5))
        address_entry = ttk.Entry(row, textvariable=email_var, width=48)
        address_entry.pack(side='left', fill='x', expand=True)
        values = {'name': name_var, 'email': email_var, 'frame': row}
        self.kindle_target_rows.append(values)
        name_entry.bind('<FocusOut>', lambda _event: self._save_kindle_settings())
        address_entry.bind('<FocusOut>', lambda _event: self._save_kindle_settings())
        ttk.Button(row, text='Remove', width=8,
                   command=lambda entry=values: self._remove_kindle_email_target(entry)).pack(
                       side='left', padx=(5, 0))
        if save:
            self._save_kindle_settings()

    def _remove_kindle_email_target(self, target_row):
        if target_row in self.kindle_target_rows:
            self.kindle_target_rows.remove(target_row)
            target_row['frame'].destroy()
        if not self.kindle_target_rows:
            self._add_kindle_email_target('Kindle 1', '', save=False)
        self._save_kindle_settings()

    @staticmethod
    def _open_kindle_email_help():
        webbrowser.open('https://digprjsurvey.amazon.com/csad/help/node/G7NECT4B4ZWHQ8WV')

    def _save_kindle_settings(self):
        set_kindle_setting(self.db, 'kindle_auto_send',
                           '1' if self.kindle_auto_send.get() else '0')
        method = self.kindle_delivery_method.get().upper()
        set_kindle_setting(self.db, 'kindle_delivery_method',
                           method if method in {'USB', 'EMAIL'} else 'USB')
        target = self.kindle_target_format.get().upper()
        set_kindle_setting(self.db, 'kindle_target_format',
                           target if target in {'AZW3', 'MOBI'} else 'AZW3')
        email_format = ('AZW3' if self.kindle_email_format.get().upper().startswith('AZW3')
                        else 'EPUB')
        set_kindle_setting(self.db, 'kindle_email_format', email_format)
        set_kindle_setting(self.db, 'kindle_device_path', self.kindle_device_path.get().strip())
        set_kindle_setting(self.db, 'kindle_converter_path',
                           self.kindle_converter_path.get().strip())
        targets = [{'name': entry['name'].get().strip(),
                    'email': entry['email'].get().strip()}
                   for entry in self.kindle_target_rows
                   if entry['email'].get().strip()]
        self.kindle_email_targets = targets
        set_kindle_setting(self.db, 'kindle_email_targets',
                           json.dumps(targets, ensure_ascii=False))
        primary_email = targets[0]['email'] if targets else ''
        self.kindle_email_to.set(primary_email)
        set_kindle_setting(self.db, 'kindle_email_to', primary_email)
        set_kindle_setting(self.db, 'kindle_smtp_host', self.kindle_smtp_host.get().strip())
        set_kindle_setting(self.db, 'kindle_smtp_port', self.kindle_smtp_port.get().strip() or '587')
        security = self.kindle_smtp_security.get().upper()
        set_kindle_setting(self.db, 'kindle_smtp_security',
                           security if security in {'STARTTLS', 'SSL'} else 'STARTTLS')
        set_kindle_setting(self.db, 'kindle_smtp_from', self.kindle_smtp_from.get().strip())
        set_kindle_setting(self.db, 'kindle_smtp_username', self.kindle_smtp_username.get().strip())
        try:
            set_kindle_smtp_password(self.db, self.kindle_smtp_password.get())
        except Exception:
            self.logger.exception('Could not securely save Kindle email password')
            self.kindle_settings_status.set(
                'Could not protect the email password; it was not saved. Check app logs.')
            return
        self.kindle_settings_status.set(
            ('Automatic Kindle delivery is on' if self.kindle_auto_send.get()
             else 'Automatic Kindle delivery is off') +
            f' ({self.kindle_delivery_method.get().lower()} route).')

    def detect_kindle_device(self):
        documents = find_kindle_documents_folder()
        if not documents:
            self.kindle_settings_status.set(
                'No mounted Kindle volume was detected. Connect it by USB or choose its Documents folder.')
            return
        self.kindle_device_path.set(str(documents))
        self._save_kindle_settings()
        self.kindle_settings_status.set(f'Kindle Documents folder found: {documents}')

    def choose_kindle_folder(self):
        chosen = filedialog.askdirectory(
            initialdir=self.kindle_device_path.get().strip() or str(self.data_root),
            title='Choose the Kindle root or Documents folder')
        if chosen:
            self.kindle_device_path.set(chosen)
            self._save_kindle_settings()

    def choose_kindle_converter(self):
        configured_text = self.kindle_converter_path.get().strip()
        configured = Path(configured_text).expanduser() if configured_text else None
        initialdir = (str(configured if configured.is_dir() else configured.parent)
                      if configured else None)
        chosen = filedialog.askopenfilename(
            initialdir=initialdir,
            title='Choose the Kindling AZW3 converter',
            filetypes=(('Kindling converter', 'kindling-cli.exe'), ('All files', '*.*')))
        if chosen:
            self.kindle_converter_path.set(chosen)
            self._save_kindle_settings()

    def _build_metadata_api_settings(self, parent):
        ttk.Label(parent, text='Optional metadata API credentials',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        ttk.Label(parent, text=(
            'TMDB expands movie and television metadata. IGDB adds game metadata and release classifications. '
            'YouTube Data API enables complete public-playlist snapshots and removal tracking. Credentials '
            'Comic Vine and Metron add issue catalogs. Credentials stay in memory for this app session; '
            'set COMICVINE_API_KEY, METRON_API_TOKEN, TMDB_API_KEY, YOUTUBE_API_KEY, IGDB_CLIENT_ID, and '
            'IGDB_CLIENT_SECRET in the launch environment to reuse them after restart. Hardcover API tokens '
            'are encrypted for this Windows account and used to refresh watched Hardcover lists. Google Books '
            'works without a key but shares a small public quota and often answers "429 Too Many Requests"; a '
            'free Books API key from the Google Cloud console gives this app its own quota and is saved encrypted '
            'for this Windows account.'),
            wraplength=850, justify='left').pack(anchor='w', pady=(4, 12))
        form = ttk.Frame(parent)
        form.pack(anchor='nw', fill='x')
        for index, (label, variable, secret) in enumerate((
                ('TMDB API key', self.tmdb_key_entry, True),
                ('YouTube Data API key', self.youtube_key_entry, True),
                ('IGDB Twitch client ID', self.igdb_client_id_entry, False),
                ('IGDB Twitch client secret', self.igdb_client_secret_entry, True),
                ('Comic Vine API key', self.comicvine_key_entry, True),
                ('Metron API token', self.metron_token_entry, True),
                ('Hardcover API token', self.hardcover_api_token_entry, True),
                ('Google Books API key', self.google_books_api_key_entry, True))):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky='w', padx=(0, 10), pady=5)
            ttk.Entry(form, textvariable=variable, width=64,
                      show='*' if secret else '').grid(row=index, column=1, sticky='ew', pady=5)
        form.columnconfigure(1, weight=1)
        buttons = ttk.Frame(parent)
        buttons.pack(anchor='w', pady=(10, 4))
        ttk.Button(buttons, text='Apply to this session', command=self.apply_metadata_credentials).pack(side='left')
        ttk.Button(buttons, text='Clear session credentials', command=self.clear_metadata_credentials).pack(side='left', padx=6)
        ttk.Label(parent, textvariable=self.metadata_credential_status).pack(anchor='w', pady=(4, 0))
        self._update_metadata_credential_status()

    def apply_metadata_credentials(self):
        self.comicvine_api_key = self.comicvine_key_entry.get().strip()
        self.metron_api_token = self.metron_token_entry.get().strip()
        self.tmdb_api_key = self.tmdb_key_entry.get().strip()
        self.youtube_api_key = self.youtube_key_entry.get().strip()
        self.igdb_client_id = self.igdb_client_id_entry.get().strip()
        self.igdb_client_secret = self.igdb_client_secret_entry.get().strip()
        self.hardcover_api_token = self.hardcover_api_token_entry.get().strip()
        try:
            set_local_secret(self.db, 'hardcover_api_token', self.hardcover_api_token)
        except Exception as exc:
            self.metadata_credential_status.set(
                f'Could not securely save Hardcover token: {exc}')
            return
        google_books_key = self.google_books_api_key_entry.get().strip()
        try:
            set_local_secret(self.db, 'google_books_api_key', google_books_key)
        except Exception as exc:
            self.metadata_credential_status.set(f'Could not securely save the Google Books key: {exc}')
            return
        self._use_google_books_key(google_books_key)
        self._update_metadata_credential_status()
        self._update_search_category_info()

    def _load_google_books_key(self):
        """The saved key (encrypted for this Windows user), else GOOGLE_BOOKS_API_KEY from the environment."""
        try:
            saved = get_local_secret(self.db, 'google_books_api_key')
        except Exception:
            saved = ''
            self.logger.warning('Saved Google Books key could not be unlocked; re-enter it in Settings.')
        if saved:
            self._use_google_books_key(saved)
        return saved or os.environ.get('GOOGLE_BOOKS_API_KEY', '').strip()

    @staticmethod
    def _use_google_books_key(key):
        # Book searches run in worker threads and read the key from the environment.
        if key:
            os.environ['GOOGLE_BOOKS_API_KEY'] = key
        else:
            os.environ.pop('GOOGLE_BOOKS_API_KEY', None)

    def clear_metadata_credentials(self):
        self.comicvine_api_key = ''
        self.metron_api_token = ''
        self.comicvine_key_entry.set('')
        self.metron_token_entry.set('')
        self.tmdb_api_key = ''
        self.youtube_api_key = ''
        self.igdb_client_id = ''
        self.igdb_client_secret = ''
        self.hardcover_api_token = ''
        self.tmdb_key_entry.set('')
        self.youtube_key_entry.set('')
        self.igdb_client_id_entry.set('')
        self.igdb_client_secret_entry.set('')
        self.hardcover_api_token_entry.set('')
        try:
            set_local_secret(self.db, 'hardcover_api_token', '')
        except Exception:
            self.logger.warning('Could not clear the saved Hardcover token.')
        self.google_books_api_key_entry.set('')
        try:
            set_local_secret(self.db, 'google_books_api_key', '')
        except Exception:
            self.logger.warning('Could not clear the saved Google Books key.')
        self._use_google_books_key('')
        self._update_metadata_credential_status()
        self._update_search_category_info()

    def _update_metadata_credential_status(self):
        configured = []
        if self.comicvine_api_key:
            configured.append('Comic Vine')
        if self.metron_api_token:
            configured.append('Metron')
        if self.tmdb_api_key:
            configured.append('TMDB')
        if self.youtube_api_key:
            configured.append('YouTube Data API')
        if self.igdb_client_id and self.igdb_client_secret:
            configured.append('IGDB')
        if self.hardcover_api_token:
            configured.append('Hardcover')
        if os.environ.get('GOOGLE_BOOKS_API_KEY'):
            configured.append('Google Books')
        self.metadata_credential_status.set(
            'Configured for this session: ' + (', '.join(configured) if configured else 'none'))

    def _load_irc_sources(self):
        try:
            sources = json.loads(get_setting(self.db, 'watch_irc_sources', '[]'))
        except (TypeError, ValueError):
            sources = []
        sources = [source for source in sources if isinstance(source, dict)] if isinstance(sources, list) else []
        remaining_paths = []
        migrated = False
        for config_path in (value.strip() for value in self.watch_irc_configs.get().split(';')):
            if not config_path:
                continue
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
                remaining_paths.append(config_path)
        if migrated:
            self.watch_irc_configs.set(';'.join(remaining_paths))
            set_setting(self.db, 'watch_irc_configs', ';'.join(remaining_paths))
        from .irc import ensure_ebook_presets
        try:
            applied = json.loads(get_setting(self.db, 'irc_presets_applied', '[]'))
        except (TypeError, ValueError):
            applied = []
        sources, applied, _added = ensure_ebook_presets(sources, applied)
        set_setting(self.db, 'irc_presets_applied', json.dumps(applied))
        set_setting(self.db, 'watch_irc_sources', json.dumps(sources, ensure_ascii=False))
        return sources

    def _build_irc_sources(self, parent):
        ttk.Label(parent, text='IRC search sources',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        ttk.Label(parent,
                  text=('Each row is one server, nickname, and channel. Add a server, connect, and check the '
                        'channels it should search. Assign the media categories it can search. '
                        'Sources are tried from top to bottom for that category; the next IRC source is queried only '
                        'when the earlier source has no matching files. Passwords stay in environment variables.'),
                  wraplength=900, justify='left').pack(anchor='w', pady=(4, 10))
        body = ttk.Frame(parent)
        body.pack(fill='both', expand=True)
        columns = ('priority', 'name', 'server', 'nickname', 'channel', 'categories')
        self.irc_sources_view = ttk.Treeview(body, columns=columns, show='headings',
                                             selectmode='browse', height=13)
        for column, width in (('priority', 65), ('name', 130), ('server', 145),
                              ('nickname', 105), ('channel', 120), ('categories', 280)):
            self.irc_sources_view.heading(column, text='Priority' if column == 'priority' else column.title())
            self.irc_sources_view.column(column, width=width, anchor='w', stretch=column == 'categories')
        self.irc_sources_view.pack(side='left', fill='both', expand=True)
        buttons = ttk.Frame(body)
        buttons.pack(side='left', fill='y', padx=(8, 0))
        ttk.Button(buttons, text='Add source', command=self.add_irc_source).pack(fill='x', pady=(0, 5))
        ttk.Button(buttons, text='Edit selected', command=self.edit_irc_source).pack(fill='x', pady=5)
        ttk.Button(buttons, text='Remove selected', command=self.remove_irc_source).pack(fill='x', pady=5)
        ttk.Separator(buttons).pack(fill='x', pady=8)
        ttk.Button(buttons, text='Move up', command=lambda: self.move_irc_source(-1)).pack(fill='x', pady=5)
        ttk.Button(buttons, text='Move down', command=lambda: self.move_irc_source(1)).pack(fill='x', pady=5)
        self._render_irc_sources()

    def _render_irc_sources(self, selected_id=None):
        if not self.irc_sources_view:
            return
        for row in self.irc_sources_view.get_children():
            self.irc_sources_view.delete(row)
        for index, source in enumerate(self.irc_sources, 1):
            categories = [category for category in CATEGORIES
                          if category in (source.get('categories') or [])]
            name = source.get('name') or source.get('server') or ''
            self.irc_sources_view.insert(
                '', 'end', iid=str(source.get('id') or index - 1),
                values=(index, name, source.get('server') or '', source.get('nick') or '', source.get('channel') or '',
                        ', '.join(categories)))
        if selected_id and self.irc_sources_view.exists(str(selected_id)):
            self.irc_sources_view.selection_set(str(selected_id))
            self.irc_sources_view.see(str(selected_id))

    def _selected_irc_source_index(self):
        if not self.irc_sources_view:
            return None
        selected = self.irc_sources_view.selection()
        if not selected:
            return None
        selected_id = selected[0]
        return next((index for index, source in enumerate(self.irc_sources)
                     if str(source.get('id') or index) == selected_id), None)

    def _save_irc_sources(self, selected_id=None):
        self.irc_sources = list(self.irc_sources)
        set_setting(self.db, 'watch_irc_sources', json.dumps(self.irc_sources, ensure_ascii=False))
        self._saved_watch_sources = None
        self._watch_sources()
        self._render_irc_sources(selected_id)
        self._render_download_priority()

    def add_irc_source(self):
        self._edit_irc_source_dialog()

    def edit_irc_source(self):
        index = self._selected_irc_source_index()
        if index is None:
            return
        self._edit_irc_source_dialog(index)

    def _edit_irc_source_dialog(self, edit_index=None):
        existing = dict(self.irc_sources[edit_index]) if edit_index is not None else {}
        dialog = tk.Toplevel(self.root)
        dialog.title('Edit IRC source' if existing else 'Add IRC source')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        form = ttk.Frame(dialog, padding=12)
        form.pack(fill='both', expand=True)
        values = {
            'name': tk.StringVar(value=existing.get('name', '')),
            'server': tk.StringVar(value=existing.get('server', '')),
            'port': tk.StringVar(value=str(existing.get('port', 6697))),
            'nick': tk.StringVar(value=existing.get('nick', 'MediaIntake')),
            'channel': tk.StringVar(value=existing.get('channel', '')),
            'password_env': tk.StringVar(value=existing.get('password_env', '')),
        }
        tls = tk.BooleanVar(value=existing.get('tls', True))
        verify_tls = tk.BooleanVar(value=existing.get('verify_tls', True))
        labels = (('Name (optional)', 'name'), ('Server', 'server'), ('Port', 'port'),
                  ('Nickname', 'nick'), ('Channel(s)', 'channel'),
                  ('Password environment variable (optional)', 'password_env'))
        for row, (label, key) in enumerate(labels):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=(0, 10), pady=4)
            if key == 'channel':
                channel_row = ttk.Frame(form)
                channel_row.grid(row=row, column=1, sticky='ew', pady=4)
                ttk.Entry(channel_row, textvariable=values[key], width=34).pack(
                    side='left', fill='x', expand=True)
                ttk.Button(
                    channel_row, text='Connect & list channels',
                    command=lambda: self._connect_and_list_irc_channels(
                        dialog, values, tls, verify_tls)).pack(side='left', padx=(6, 0))
            else:
                ttk.Entry(form, textvariable=values[key], width=43).grid(
                    row=row, column=1, sticky='ew', pady=4)
        ttk.Label(form, text='Enter channels separated by commas, or connect and choose from the server list. '
                            'Each checked channel is saved as a separate searchable source.',
                  wraplength=440, justify='left').grid(
            row=len(labels), column=1, sticky='w', pady=(0, 6))
        form.columnconfigure(1, weight=1)
        security = ttk.Frame(form)
        security.grid(row=len(labels) + 1, column=1, sticky='w', pady=(4, 8))
        ttk.Checkbutton(security, text='Use TLS', variable=tls).pack(side='left')
        ttk.Checkbutton(security, text='Allow unverified certificate',
                        variable=verify_tls, onvalue=False, offvalue=True).pack(side='left', padx=8)
        ttk.Label(form, text='Only allow unverified certificates for a server you trust.').grid(
            row=len(labels) + 1, column=0, sticky='w', pady=(4, 8))
        ttk.Label(form, text='Search these categories').grid(
            row=len(labels) + 2, column=0, sticky='nw', padx=(0, 10), pady=4)
        categories = tk.Listbox(form, selectmode='multiple', exportselection=False,
                                height=min(len(CATEGORIES), 9), width=40)
        categories.grid(row=len(labels) + 2, column=1, sticky='ew', pady=4)
        for category in CATEGORIES:
            categories.insert('end', category)
        assigned = set(existing.get('categories') or (CATEGORIES[0],))
        for index, category in enumerate(CATEGORIES):
            if category in assigned:
                categories.selection_set(index)
        buttons = ttk.Frame(form)
        buttons.grid(row=len(labels) + 3, column=0, columnspan=2, sticky='e', pady=(10, 0))

        def save():
            server = values['server'].get().strip()
            nick = values['nick'].get().strip()
            channels = list(dict.fromkeys(
                value.strip() for value in values['channel'].get().split(',')
                if value.strip()))
            password_env = values['password_env'].get().strip()
            try:
                port = int(values['port'].get().strip())
            except ValueError:
                messagebox.showerror('IRC source', 'Port must be a number from 1 to 65535.', parent=dialog)
                return
            selected_categories = [CATEGORIES[index] for index in categories.curselection()]
            valid_server = bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*[A-Za-z0-9]', server))
            if (not valid_server or '..' in server or not 1 <= port <= 65535
                    or not re.fullmatch(r'[\w\-\[\]{}^|`]{1,30}', nick)
                    or not channels or any(not re.fullmatch(
                        r'#[\w\-\[\]{}^|`]+', channel) for channel in channels)
                    or (password_env and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', password_env))
                    or not selected_categories):
                messagebox.showerror(
                    'IRC source',
                    'Enter a valid server, nickname, at least one channel, port, optional environment variable, and at least one category.',
                    parent=dialog)
                return
            name = values['name'].get().strip()[:80]
            selected_sources = []
            first_id = None

            def stored_port(source):
                try:
                    return int(source.get('port') or 6697)
                except (TypeError, ValueError):
                    return None

            for channel in channels:
                duplicate = next((source for index, source in enumerate(self.irc_sources)
                                  if index != edit_index and
                                  str(source.get('server') or '').casefold() == server.casefold() and
                                  stored_port(source) == port and
                                  str(source.get('channel') or '').casefold() == channel.casefold()),
                                 None)
                if duplicate:
                    continue
                source_id = (existing.get('id') if edit_index is not None and not first_id
                             else uuid.uuid4().hex)
                if first_id is None:
                    first_id = source_id
                selected_sources.append({
                    'id': source_id,
                    'name': name or server,
                    'server': server, 'port': port, 'tls': tls.get(),
                    'verify_tls': verify_tls.get(), 'nick': nick,
                    'channel': channel, 'password_env': password_env,
                    'timeout': int(existing.get('timeout', 180)),
                    'categories': selected_categories, 'enabled': True,
                })
            if not selected_sources:
                messagebox.showinfo('IRC source',
                                    'All selected channels are already configured for this server and port.',
                                    parent=dialog)
                return
            if edit_index is None:
                self.irc_sources.extend(selected_sources)
            else:
                self.irc_sources[edit_index:edit_index + 1] = selected_sources
            dialog.destroy()
            self._save_irc_sources(first_id)
            self._done(f'Saved {len(selected_sources)} searchable IRC channel(s).')

        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=(6, 0))
        ttk.Button(buttons, text='Save source', command=save).pack(side='right')
        dialog.grab_set()
        dialog.wait_window()

    def _connect_and_list_irc_channels(self, editor, values, tls, verify_tls):
        server = values['server'].get().strip()
        nick = values['nick'].get().strip()
        password_env = values['password_env'].get().strip()
        try:
            port = int(values['port'].get().strip())
        except ValueError:
            messagebox.showerror('IRC connection', 'Port must be a number from 1 to 65535.', parent=editor)
            return
        if not server or not nick:
            messagebox.showerror('IRC connection',
                                 'Enter the server and nickname before connecting.', parent=editor)
            return
        config = {'server': server, 'port': port, 'tls': tls.get(),
                  'verify_tls': verify_tls.get(), 'nick': nick,
                  'password_env': password_env, 'timeout': 25}
        if server.casefold().endswith('undernet.org'):
            # UnderNet warns that a network-wide LIST can disconnect clients.
            # For the linked guide, request only its two relevant channels.
            config['channel_mask'] = '#Bookz,#ebooks'

        def channels_loaded(channels):
            self._done(f'Connected to {server}; found {len(channels)} public channel(s).')
            self._choose_irc_channels(editor, values['channel'], channels)

        self._worker(
            f'Connecting to {server} and listing IRC channels…',
            lambda: discover_irc_channels(config), channels_loaded,
            on_error=lambda error: messagebox.showerror(
                'IRC connection',
                f'{error}\n\nIf the server uses an untrusted TLS certificate, enable '
                '“Allow unverified certificate” only if you trust that server. '
                'You can also use its plain IRC port with TLS turned off.', parent=editor))

    @staticmethod
    def _choose_irc_channels(parent, channel_variable, channels):
        if not channels:
            messagebox.showinfo(
                'IRC channels',
                'The connection succeeded, but the server did not return any public channels. '
                'You can enter a channel name manually.', parent=parent)
            return
        picker = tk.Toplevel(parent)
        picker.title('Choose searchable IRC channels')
        picker.geometry('620x480')
        picker.transient(parent)
        ttk.Label(picker, text='Check each channel to add it as a searchable source.').pack(
            anchor='w', padx=10, pady=(10, 5))
        outer = ttk.Frame(picker)
        outer.pack(fill='both', expand=True, padx=10, pady=5)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        choices_frame = ttk.Frame(canvas)
        choices_frame.bind('<Configure>', lambda _event: canvas.configure(
            scrollregion=canvas.bbox('all')))
        window_id = canvas.create_window((0, 0), window=choices_frame, anchor='nw')
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(
            window_id, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        existing = {value.strip().casefold()
                    for value in channel_variable.get().split(',') if value.strip()}
        variables = []
        for entry in channels:
            channel = str(entry.get('channel') or '').strip()
            if not channel:
                continue
            variable = tk.BooleanVar(value=channel.casefold() in existing)
            topic = str(entry.get('topic') or '').replace('\n', ' ').strip()
            label = f'{channel}  ({entry.get("users", 0)} users)'
            if topic:
                label += ' — ' + (topic[:90] + '…' if len(topic) > 90 else topic)
            ttk.Checkbutton(choices_frame, text=label, variable=variable).pack(
                anchor='w', fill='x', pady=1)
            variables.append((channel, variable))
        buttons = ttk.Frame(picker)
        buttons.pack(fill='x', padx=10, pady=(5, 10))
        ttk.Button(buttons, text='Select all', command=lambda: [
            variable.set(True) for _channel, variable in variables]).pack(side='left')
        ttk.Button(buttons, text='Clear', command=lambda: [
            variable.set(False) for _channel, variable in variables]).pack(side='left', padx=5)

        def apply_selection():
            selected = [channel for channel, variable in variables if variable.get()]
            channel_variable.set(', '.join(selected))
            picker.destroy()

        ttk.Button(buttons, text='Use checked channels', command=apply_selection).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=picker.destroy).pack(side='right', padx=6)
        picker.grab_set()

    def remove_irc_source(self):
        index = self._selected_irc_source_index()
        if index is None:
            return
        source = self.irc_sources.pop(index)
        self._save_irc_sources()

    def move_irc_source(self, direction):
        index = self._selected_irc_source_index()
        target = index + direction if index is not None else None
        if target is None or target < 0 or target >= len(self.irc_sources):
            return
        self.irc_sources[index], self.irc_sources[target] = self.irc_sources[target], self.irc_sources[index]
        self._save_irc_sources(self.irc_sources[target].get('id'))

    def _selected_settings_category(self):
        if not self.settings_category:
            return None
        selected = self.settings_category.curselection()
        return self.settings_category.get(selected[0]) if selected else None

    def _select_priority_category(self, _event=None):
        self._render_download_priority()

    def _render_download_priority(self, selected_source=None):
        if not self.priority_view:
            return
        category = self._selected_settings_category()
        for row in self.priority_view.get_children():
            self.priority_view.delete(row)
        if not category:
            self.priority_status.set('Choose a media category.')
            return
        options = self._watch_sources()
        ordered = get_download_priority(self.db, category, options)
        if not ordered:
            self.priority_status.set(f'No connected download sources for {category}.')
            return
        for rank, source in enumerate(ordered, start=1):
            self.priority_view.insert('', 'end', iid=str(rank - 1), values=(rank, source))
        if selected_source in ordered:
            self.priority_view.selection_set(str(ordered.index(selected_source)))
        else:
            self.priority_view.selection_set('0')
        self.priority_status.set(f'Priority order saved for {category}.')

    def _move_priority(self, direction):
        category = self._selected_settings_category()
        if not category or not self.priority_view:
            return
        selected = self.priority_view.selection()
        if not selected:
            return
        ordered = [self.priority_view.item(row, 'values')[1]
                   for row in self.priority_view.get_children()]
        index = int(selected[0])
        target = index + direction
        if target < 0 or target >= len(ordered):
            return
        ordered[index], ordered[target] = ordered[target], ordered[index]
        save_download_priority(self.db, category, ordered, self._watch_sources())
        self._render_download_priority(ordered[target])

    def _reset_priority(self):
        category = self._selected_settings_category()
        if not category:
            return
        defaults = connected_download_sources(category, self._watch_sources())
        if not defaults:
            self._render_download_priority()
            return
        save_download_priority(self.db, category, defaults, self._watch_sources())
        self._render_download_priority(defaults[0])

    def _build_watched(self, parent):
        profile = ttk.Frame(parent)
        profile.pack(fill='x', pady=(0, 8))
        ttk.Label(profile, text='Active profile').pack(side='left')
        self.active_profile_box = ttk.Combobox(
            profile, textvariable=self.active_profile_name, state='readonly', width=28)
        self.active_profile_box.pack(side='left', padx=6)
        self.active_profile_box.bind('<<ComboboxSelected>>',
                                     self._on_active_profile_selected)
        ttk.Label(profile, text='New watches and their future downloads use this profile.').pack(
            side='left', padx=6)

        destination = ttk.LabelFrame(parent, text='Saved library folder')
        destination.pack(fill='x', pady=(0, 8))
        destination_entry = ttk.Entry(destination, textvariable=self.library_folder, width=96)
        destination_entry.pack(side='left', fill='x', expand=True, padx=6, pady=6)
        destination_entry.bind('<FocusOut>', lambda _event: set_setting(self.db, 'library_folder', self.library_folder.get().strip()))
        ttk.Button(destination, text='Choose folder', command=self.choose_library_folder).pack(side='left', padx=(0, 6))

        add_row = ttk.Frame(parent)
        add_row.pack(fill='x', pady=(0, 8))
        ttk.Button(add_row, text='Add to watched', command=self.open_watch_add_dialog).pack(side='left')
        ttk.Label(add_row, text='Choose a media category, then search its metadata or enter a title and source.').pack(side='left', padx=8)

        options = ttk.LabelFrame(parent, text='Connected sites to search for watched items')
        options.pack(fill='x', pady=(0, 8))
        line = ttk.Frame(options)
        line.pack(fill='x', padx=6, pady=(5, 2))
        ttk.Checkbutton(line, text='LibGen', variable=self.watch_libgen,
                        command=self._watch_sources).pack(side='left')
        ttk.Label(line, text='Base URL').pack(side='left', padx=(4, 0))
        libgen_base_entry = ttk.Entry(line, textvariable=self.watch_libgen_base, width=27)
        libgen_base_entry.pack(side='left', padx=5)
        libgen_base_entry.bind('<FocusOut>', lambda _event: self._watch_sources())
        ttk.Checkbutton(line, text="Anna's Archive", variable=self.watch_annas,
                        command=self._watch_sources).pack(side='left', padx=(8, 0))
        ttk.Label(line, text='Base URL').pack(side='left', padx=(4, 0))
        annas_base_entry = ttk.Entry(line, textvariable=self.watch_annas_base, width=27)
        annas_base_entry.pack(side='left', padx=5)
        annas_base_entry.bind('<FocusOut>', lambda _event: self._watch_sources())

        line = ttk.Frame(options)
        line.pack(fill='x', padx=6, pady=2)
        ttk.Label(line, text='OPDS feeds (; separated)').pack(side='left')
        opds_entry = ttk.Entry(line, textvariable=self.watch_opds_feeds, width=46)
        opds_entry.pack(side='left', padx=5, fill='x', expand=True)
        opds_entry.bind('<FocusOut>', lambda _event: self._watch_sources())
        ttk.Label(line, text='JSON catalogs').pack(side='left', padx=(8, 0))
        catalogs_entry = ttk.Entry(line, textvariable=self.watch_catalogs, width=34)
        catalogs_entry.pack(side='left', padx=5)
        catalogs_entry.bind('<FocusOut>', lambda _event: self._watch_sources())
        ttk.Button(line, text='Choose', command=self.choose_watch_catalogs).pack(side='left')

        line = ttk.Frame(options)
        line.pack(fill='x', padx=6, pady=(2, 5))
        ttk.Label(line, text='Legacy IRC configs').pack(side='left')
        irc_configs_entry = ttk.Entry(line, textvariable=self.watch_irc_configs, width=80)
        irc_configs_entry.pack(side='left', padx=5, fill='x', expand=True)
        irc_configs_entry.bind('<FocusOut>', lambda _event: self._watch_sources())
        ttk.Button(line, text='Import into Settings', command=self.choose_irc_configs).pack(side='left')
        line = ttk.Frame(options)
        line.pack(fill='x', padx=6, pady=(2, 5))
        ttk.Label(line, text='Public-domain comic mirror base').pack(side='left')
        mirror_entry = ttk.Entry(line, textvariable=self.watch_comic_mirror_base, width=72)
        mirror_entry.pack(side='left', padx=6, fill='x', expand=True)
        mirror_entry.bind('<FocusOut>', lambda _event: self._watch_sources())
        ttk.Label(options, text='Comic watches search this separate mirror for CBZ/PDF pages marked Public Domain or CC0. GetComics results open in a browser.').pack(anchor='w', padx=8, pady=(0, 5))

        columns = ('kind', 'title', 'author', 'release', 'status', 'checked', 'file')
        watched_frame = ttk.Frame(parent)
        watched_frame.pack(fill='both', expand=True)
        self.watched_view = ttk.Treeview(watched_frame, columns=columns, show='headings', selectmode='browse')
        for col, width in (('kind', 85), ('title', 190), ('author', 130), ('release', 95),
                           ('status', 170), ('checked', 130), ('file', 180)):
            self.watched_view.heading(col, text={'release': 'Release date', 'checked': 'Last checked'}.get(col, col.title()))
            self.watched_view.column(col, width=width, anchor='w')
        watched_y = ttk.Scrollbar(watched_frame, orient='vertical', command=self.watched_view.yview)
        watched_x = ttk.Scrollbar(parent, orient='horizontal', command=self.watched_view.xview)
        self.watched_view.configure(yscrollcommand=watched_y.set, xscrollcommand=watched_x.set)
        self.watched_view.pack(side='left', fill='both', expand=True)
        watched_y.pack(side='right', fill='y')
        watched_x.pack(fill='x')

        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Refresh', command=self.refresh_watches).pack(side='left')
        ttk.Button(buttons, text='Check watched now', command=lambda: self._run_watch_scan(force=True)).pack(side='left', padx=6)
        ttk.Button(buttons, text='Discover related', command=self.discover_watched_relationships).pack(side='left', padx=6)
        ttk.Button(buttons, text='Set/clear release date', command=self.edit_watch_release_date).pack(side='left', padx=6)
        ttk.Button(buttons, text='Remove selected', command=self.delete_watched).pack(side='left')
        ttk.Button(buttons, text='Export watch list (CSV)…', command=self.export_watch_list).pack(side='left', padx=6)

        graph_tools = ttk.Frame(parent)
        graph_tools.pack(fill='x', pady=(5, 0))
        ttk.Button(graph_tools, text='View continuity and releases',
                   command=self.view_media_continuity).pack(side='left')
        ttk.Button(graph_tools, text='Needs Review',
                   command=self.open_graph_review).pack(side='left', padx=6)
        ttk.Button(graph_tools, text='Watch policies',
                   command=self.open_watch_policy_editor).pack(side='left')
        ttk.Button(graph_tools, text='Book batches',
                   command=self.open_book_batches).pack(side='left', padx=6)

    def open_watch_add_dialog(self):
        if self._watch_add_dialog and self._watch_add_dialog.winfo_exists():
            self._watch_add_dialog.lift()
            return
        dialog = tk.Toplevel(self.root)
        self._watch_add_dialog = dialog
        dialog.title('Add to watched')
        dialog.geometry('700x490')
        dialog.minsize(560, 420)
        dialog.transient(self.root)

        category = tk.StringVar(value=CATEGORIES[0])
        mode = tk.StringVar(value='Book')
        values = {key: tk.StringVar() for key in
                  ('title', 'author', 'series', 'url', 'feed', 'release', 'list_name')}
        ttk.Label(dialog, text='Choose what to watch',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w', padx=12, pady=(12, 5))
        selectors = ttk.Frame(dialog)
        selectors.pack(fill='x', padx=12)
        ttk.Label(selectors, text='Category').pack(side='left')
        category_box = ttk.Combobox(selectors, textvariable=category, state='readonly',
                                    values=CATEGORIES, width=22)
        category_box.pack(side='left', padx=(6, 16))
        ttk.Label(selectors, text='Track').pack(side='left')
        mode_box = ttk.Combobox(selectors, textvariable=mode, state='readonly', width=30)
        mode_box.pack(side='left', padx=6)

        guidance = tk.StringVar(value='')
        ttk.Label(dialog, textvariable=guidance, wraplength=660,
                  justify='left').pack(anchor='w', padx=12, pady=(8, 6))
        fields = ttk.Frame(dialog)
        fields.pack(fill='both', expand=True, padx=12, pady=4)

        modes_by_category = {
            'Books': ('Book', 'Series', 'Author bibliography', 'Book list'),
            'Comics': ('Comic', 'Webcomic', 'Webtoon'),
            'Manga': ('Manga',),
            'Anime': ('Anime',),
            'Videos': ('Video', 'Web video series', 'Video podcast',
                       'Documentary series', 'Performance series'),
            'Games': ('Game',),
            'Software': ('Software',),
            'Light novels': ('Light novel',),
            'Web serials': ('Web serial',),
            'Fanfiction': ('Fanfiction',),
        }

        def add_field(container, label, key, width=70):
            row = ttk.Frame(container)
            row.pack(fill='x', pady=4)
            ttk.Label(row, text=label, width=19, anchor='w').pack(side='left')
            entry = ttk.Entry(row, textvariable=values[key], width=width)
            entry.pack(side='left', fill='x', expand=True)
            return entry

        def render_fields(*_args):
            available = modes_by_category.get(category.get(), ('Title',))
            mode_box.configure(values=available)
            if mode.get() not in available:
                mode.set(available[0])
            for child in fields.winfo_children():
                child.destroy()
            selected = mode.get()
            if category.get() == 'Books' and selected == 'Book list':
                guidance.set('Paste a public Goodreads or Hardcover book list URL. Its members are added to Watched and checked daily for additions.')
                add_field(fields, 'Book list URL', 'url')
                add_field(fields, 'List name (optional)', 'list_name')
                if not self.hardcover_api_token:
                    ttk.Label(fields, text='Hardcover lists need an API token in Settings → Metadata APIs.',
                              foreground='#805000').pack(anchor='w', padx=(145, 0), pady=(0, 4))
                return
            if category.get() == 'Books':
                if selected == 'Book':
                    guidance.set('Searches Goodreads, Open Library, Google Books, and other book catalogs by title, author, and series. Choosing a match adds it to Watched and searches connected book sources.')
                    add_field(fields, 'Title', 'title')
                    add_field(fields, 'Author (optional)', 'author')
                    add_field(fields, 'Series (optional)', 'series')
                elif selected == 'Series':
                    guidance.set('Choose a catalog series to add its full bibliography and check for new books each day.')
                    add_field(fields, 'Series name', 'series')
                    add_field(fields, 'Author (optional)', 'author')
                else:
                    guidance.set('Choose an author catalog profile to add the full bibliography and check for new books each day.')
                    add_field(fields, 'Author name', 'author')
                return
            guidance.set('Add a title and, when available, a public source page or chapter/feed URL. The watch will use the connected sources for this category.')
            add_field(fields, 'Title', 'title')
            add_field(fields, 'Author / creator', 'author')
            add_field(fields, 'Source / metadata URL', 'url')
            add_field(fields, 'Feed / creator page', 'feed')
            add_field(fields, 'Release date (optional)', 'release')

        category_box.bind('<<ComboboxSelected>>', render_fields)
        render_fields()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=12, pady=(6, 12))
        ttk.Button(buttons, text='Add to watched',
                   command=lambda: self._submit_watch_dialog(
                       category.get(), mode.get(), values, dialog)).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=self._close_watch_add_dialog).pack(
            side='right', padx=6)

        lookup_button = {'widget': None}
        def update_action_buttons(*_args):
            if lookup_button['widget'] and lookup_button['widget'].winfo_exists():
                lookup_button['widget'].destroy()
            lookup_button['widget'] = None
            if category.get() == 'Books' and mode.get() != 'Book list':
                lookup_button['widget'] = ttk.Button(
                    buttons, text='Find metadata',
                    command=lambda: self._find_book_watch_metadata(
                        mode.get(), values, dialog))
                lookup_button['widget'].pack(side='left')

        update_action_buttons()
        category_box.bind('<<ComboboxSelected>>',
                          lambda _event: (render_fields(), update_action_buttons()))
        mode_box.bind('<<ComboboxSelected>>',
                      lambda _event: (render_fields(), update_action_buttons()))
        dialog.protocol('WM_DELETE_WINDOW', self._close_watch_add_dialog)

    def _close_watch_add_dialog(self):
        dialog = self._watch_add_dialog
        self._watch_add_dialog = None
        if dialog and dialog.winfo_exists():
            dialog.destroy()

    def _find_book_watch_metadata(self, mode, values, dialog):
        if mode == 'Author bibliography':
            query, expected_kind = values['author'].get().strip(), 'author'
        elif mode == 'Series':
            query = ' '.join(part for part in
                             (values['series'].get().strip(), values['author'].get().strip()) if part)
            expected_kind = 'series'
        elif mode == 'Book':
            query = ' '.join(part for part in
                             (values['title'].get().strip(), values['author'].get().strip(),
                              values['series'].get().strip()) if part)
            expected_kind = 'book'
        else:
            return
        if not query:
            messagebox.showinfo('Book metadata', 'Enter a title, author, or series name first.')
            return

        def show_matches(results):
            if mode == 'Book':
                for item in results:
                    if item.get('kind') != 'book':
                        continue
                    if (not item.get('author') or
                            str(item.get('author')).casefold() == 'unknown'):
                        item['author'] = values['author'].get().strip() or 'Unknown'
                    if not item.get('series') and values['series'].get().strip():
                        item['series'] = values['series'].get().strip()
            self.show_goodreads_matches(results, expected_kind=expected_kind)

        self._worker(
            f'Searching book metadata for {query}…',
            lambda: search_book_metadata(query, limit=80),
            show_matches)

    def _submit_watch_dialog(self, category, mode, values, dialog):
        if category == 'Books' and mode == 'Book list':
            url = values['url'].get().strip()
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or '').casefold()
            if host == 'hardcover.app' or host.endswith('.hardcover.app'):
                provider = 'Hardcover'
                if not self.hardcover_api_token:
                    messagebox.showerror('Hardcover list',
                                         'Add a Hardcover API token in Settings → Metadata APIs first.')
                    return
            elif host == 'goodreads.com' or host.endswith('.goodreads.com'):
                provider = 'Goodreads'
            else:
                messagebox.showerror('Book list',
                                     'Enter a Goodreads or Hardcover list URL.')
                return
            if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
                messagebox.showerror('Book list', 'Enter a public list URL without embedded credentials.')
                return
            name = values['list_name'].get().strip()
            if not name:
                name = urllib.parse.unquote(parsed.path.rstrip('/').split('/')[-1])
                name = re.sub(r'[_-]+', ' ', name).strip() or 'Book list'
            metadata = {'sources': self._watch_sources(), 'catalog_provider': provider,
                        'entity_type': 'collection', 'known_books': []}
            profile_id = self.active_profile_id.get().strip()
            if profile_id:
                metadata['library_profile_ids'] = [profile_id]
            data_root = self.data_root

            def action():
                db = connect(data_root)
                try:
                    watch_id = add_watch(db, 'book_list', name, 'Various authors', url,
                                         metadata=metadata)
                    if profile_id:
                        assign_watch_profile(db, watch_id, profile_id)
                    return watch_id
                finally:
                    db.close()

            def added(watch_id):
                self._close_watch_add_dialog()
                self.refresh_watches()
                self.refresh_queue()
                self._done(f'Watching {name}; importing its books and checking for additions')
                self._run_added_watch_scan()

            self._worker(f'Adding {provider} list {name}…', action, added)
            return

        self.watch_goodreads_profile = {}
        self.watch_comic_profile = {}
        self.watch_lightnovel_profile = {}
        if category == 'Books':
            if mode in {'Book', 'Series', 'Author bibliography'}:
                self._find_book_watch_metadata(mode, values, dialog)
                return
        self.watch_url.set(values['url'].get().strip())
        self.watch_title.set(values['title'].get().strip())
        self.watch_author.set(values['author'].get().strip())
        self.watch_release_date.set(values['release'].get().strip())
        self.watch_feed_url.set(values['feed'].get().strip())
        self.watch_announcement_feeds.set('')
        watch_type = mode
        if mode == 'Anime':
            watch_type = 'Anime'
        self.watch_kind.set(watch_type)
        self._close_watch_add_dialog()
        self.add_watched(auto_scan=True)

    def _busy(self, message):
        self.status.set(message)

    def _done(self, message):
        self.status.set(message)

    def _worker(self, message, action, callback=None, on_error=None):
        self._busy(message)
        def run():
            try:
                result = action()
            except Exception as exc:
                self.logger.error('Background operation failed: %s: %s\n%s',
                                  message, exc, traceback.format_exc())
                def failed(error=exc):
                    self._done('Error — details saved in logs')
                    if on_error:
                        try:
                            on_error(error)
                        except Exception as callback_error:
                            self.logger.exception('Error handler failed after %s', message)
                            messagebox.showerror('Media Intake',
                                                 f'{callback_error}\n\nDetails saved to {self.log_file}')
                    else:
                        messagebox.showerror('Media Intake',
                                             f'{error}\n\nDetails saved to {self.log_file}')
                self.root.after(0, failed)
                return
            if callback:
                def completed():
                    try:
                        callback(result)
                    except Exception as callback_error:
                        self.logger.error('Completion handler failed after %s: %s\n%s',
                                          message, callback_error, traceback.format_exc())
                        self._done('Error — details saved in logs')
                        messagebox.showerror('Media Intake',
                                             f'{callback_error}\n\nDetails saved to {self.log_file}')
                self.root.after(0, completed)
            else:
                self.root.after(0, lambda: self._done('Ready'))
        threading.Thread(target=run, daemon=True).start()

    def open_logs(self):
        try:
            if hasattr(os, 'startfile'):
                os.startfile(str(self.log_file))
            else:
                webbrowser.open(self.log_file.resolve().as_uri())
        except Exception as exc:
            self.logger.exception('Could not open diagnostics log')
            messagebox.showerror('Media Intake', f'Could not open the log: {exc}\n{self.log_file}')

    def _update_search_category_info(self):
        category = self.search_category.get()
        if category == 'Auto-detect':
            self.search_category_info.set(
                'Searches the metadata and approved download sources across all supported media categories. '
                'Choose a result to watch it using that category’s sources. Internet Archive, Meawfy, '
                'and SoftArchive require YES entries in the local rights-evidence.json file before searches run. '
                'Anime Tosho, Tokyo Insider, AnimeOut, NoobSubs, and Chauthanh are Anime-only sources and '
                'each require a YES entry there too. Newly added file sites also require their own YES rows; '
                'blocked or browser-driven sites appear as links for manual review.')
            return
        metadata = ', '.join(METADATA_CONNECTORS.get(category, ())) or 'none connected'
        downloads = ', '.join(connected_download_sources(category, self._watch_sources())) or 'none connected'
        notes = ['Internet Archive, Meawfy, and SoftArchive searches and downloads require YES entries '
                 'in the local rights-evidence.json file.']
        notes.append('MediaFireTrend, XDCC.eu, WarezOmen and the game archive sites each require their '
                     'own YES entry in the same file. Browser-only results open for review.')
        if category == 'Anime':
            notes.append('Anime Tosho, Tokyo Insider, AnimeOut, NoobSubs, and Chauthanh also require '
                         'their own YES entries in the local rights-evidence.json file.')
        if category == 'Comics':
            notes.append('GetComics and mirror file searches obey the same local rights file. '
                         'Issues appear first; alternate sources and covers are grouped. GCD and Wikidata need no key. '
                         'Comic Vine, Metron and Taddy are searched when their credentials are configured.')
        elif category == 'Fanfiction':
            notes.append('FanFicFare can track supported story URLs; IRC also searches connected file indexes.')
        elif category in {'Videos', 'Games'}:
            notes.append('IRC searches connected file indexes for matching video or game formats.')
        elif category == 'Software':
            notes.append('FOSShub contributes metadata. SourceForge and GitHub return packages only for projects with an identified open-source license; their downloads require YES entries in the local rights-evidence.json file.')
        elif category == 'Manga':
            notes.append('Taddy is searched when its API credentials are configured.')
        if category == 'Videos' and not self.tmdb_api_key:
            notes.append('TVMaze and Wikidata work without a key; add TMDB_API_KEY in Metadata APIs for movie and TV expansion.')
        if category == 'Games' and not (self.igdb_client_id and self.igdb_client_secret):
            notes.append('Steam and Wikidata work without a key; add IGDB Twitch credentials in Metadata APIs for game relationships.')
        notes.append(self._connector_status_note(category))
        self.search_category_info.set(
            f'Metadata repositories: {metadata}. Download sources: {downloads}. ' + ' '.join(notes))

    def _connector_status_note(self, category):
        """Which torrent, Usenet and library sites this category will search, and why others will not."""
        try:
            configs = load_all_configs(self.db)
            rights = load_rights_evidence(self.rights_evidence_path)
        except Exception:
            return ''
        ready, waiting = [], []
        for spec in connectors_for(category):
            config = configs.get(spec.key) or {}
            if not config.get('enabled'):
                continue
            if spec.file_capable and not source_download_rights(rights, spec.key):
                waiting.append(f'{spec.label} (needs YES in rights file)')
            elif missing_settings(spec, config) and config.get('access') != 'jackett':
                waiting.append(f'{spec.label} (needs setup)')
            else:
                ready.append(spec.label)
        parts = []
        if ready:
            parts.append('Also searching: ' + ', '.join(ready) + '.')
        if waiting:
            parts.append('Not searched yet: ' + ', '.join(waiting) + ' — see Settings → Sources.')
        return ' '.join(parts)

    def search(self):
        query = self.query.get().strip()
        if not query:
            messagebox.showinfo('Media Intake', 'Enter a title, author, or search term.')
            return
        self._set_more_pages({})
        source = self.source.get()
        if source == 'Snahp forum threads':
            self.open_forum_helper()
            return
        if source == 'Jackett (torrents)':
            self._search_jackett(query)
            return
        if spec_for_choice(source):
            self._search_connector(spec_for_choice(source), query)
            return
        if source == 'All connected in category':
            category = self.search_category.get()
            options = self._watch_sources()
            if category != 'Auto-detect':
                # Torrent, Usenet and library sites are category-targeted; Auto-detect leaves them out
                # rather than asking each site once per category.
                options = connector_options(self, options)
            self._more_search = {'query': query, 'category': category, 'options': options,
                                 'priority': None if category == 'Auto-detect' else
                                 get_download_priority(self.db, category, options)}
            priority = ({name: get_download_priority(self.db, name, options)
                         for name in CATEGORIES} if category == 'Auto-detect'
                        else get_download_priority(self.db, category, options))
            credentials = {'comicvine_api_key': self.comicvine_api_key,
                           'metron_api_token': self.metron_api_token,
                           'taddy_user_id': self.taddy_user_id,
                           'taddy_api_key': self.taddy_api_key,
                           'tmdb_api_key': self.tmdb_api_key,
                           'igdb_client_id': self.igdb_client_id,
                           'igdb_client_secret': self.igdb_client_secret}
            self._worker(
                f'Searching {category} across its connected sources…',
                lambda: resolve_media_query(query, category, options, credentials, priority),
                self.show_category_results)
            return
        source = {
            'Open Library (metadata)': 'Open Library',
            'Google Books (metadata)': 'Google Books',
            'FOSShub (metadata)': 'FOSShub',
            'SourceForge (FOSS download)': 'SourceForge',
            'GitHub (FOSS download)': 'GitHub (FOSS)',
            'Internet Archive (download)': 'Internet Archive',
            'Meawfy (download)': 'Meawfy',
            'SoftArchive (download)': 'SoftArchive',
            'Anime Tosho (download)': 'Anime Tosho',
            'Tokyo Insider (download)': 'Tokyo Insider',
            'AnimeOut (download)': 'AnimeOut',
            'NoobSubs (download)': 'NoobSubs',
            'Chauthanh (download)': 'Chauthanh',
            'LibGen (download)': 'LibGen',
            "Anna's Archive (download)": "Anna's Archive",
            'NovelArchive (metadata)': 'NovelArchive',
            'LNORI (metadata)': 'LNORI',
            'RanobeDB (metadata)': 'RanobeDB',
            'MangaUpdates (metadata)': 'MangaUpdates',
            'GetComics (download)': 'GetComics',
            'Public-domain comic mirror (download)': 'Public-domain comic mirror',
            'Google CSE (browser search)': 'Google CSE',
            'MediaFireTrend (download)': 'MediaFireTrend',
            'XDCC.eu (download)': 'XDCC.eu',
            'WarezOmen (download)': 'WarezOmen',
            'MyAbandonware (download)': 'MyAbandonware',
            'OldGamesDownload (download)': 'OldGamesDownload',
            'AbandonwareGames (download)': 'AbandonwareGames',
            'GOG Revived (download)': 'GOG Revived',
            'AnkerGames (download)': 'AnkerGames',
        }.get(source, source)
        base = self.site_base.get().strip() or None
        def action():
            if source == 'Google CSE':
                category = self.search_category.get()
                return search_google_cse_browser(
                    query, None if category == 'Auto-detect' else category)
            registered = {provider: (source_key, categories)
                          for source_key, provider, categories
                          in GENERAL_DOWNLOAD_ADAPTERS}
            if source in registered:
                source_key, categories = registered[source]
                category = self.search_category.get()
                if category == 'Auto-detect':
                    category = 'Games' if categories == frozenset({'Games'}) else 'Books'
                if category not in categories:
                    raise ValueError(f'{source} is not connected to the {category} category')
                approval = source_download_rights(
                    load_rights_evidence(self.rights_evidence_path), source_key)
                if not approval:
                    raise ValueError(
                        f'{source} is NO in the local rights-evidence.json file. '
                        'Set approved_for_download to true before searching it.')
                allowed = formats_for_category(category)
                found = search_registered_download_site(
                    query, source_key, limit=30, category=category,
                    allowed_formats=allowed)
                results = []
                for hit in found:
                    if hit.get('discovery_only') and not hit.get('detail_url'):
                        results.append(hit)
                        continue
                    items = ([hit] if source_key == 'xdcc.eu' else
                             resolve_site_downloads(hit, source_key,
                                                    allowed_formats=allowed))
                    for item in items:
                        item.update(category=category, downloadable=True,
                                    rights_evidence_path=str(self.rights_evidence_path))
                    results.extend(items)
                return results
            if source == 'Open Library':
                return [{**item, 'category': self.search_category.get(),
                         'url': item.get('source_url') or '', 'format': 'catalog',
                         'source': 'Open Library', 'source_role': 'metadata',
                         'discovery_only': True,
                         'display_source': 'Metadata · Open Library'}
                        for item in search_openlibrary_metadata(query, limit=24)]
            if source == 'Google Books':
                return [{**item, 'category': self.search_category.get(),
                         'url': item.get('source_url') or '', 'format': 'catalog',
                         'source': 'Google Books', 'source_role': 'metadata',
                         'discovery_only': True,
                        'display_source': 'Metadata · Google Books'}
                        for item in search_google_books_metadata(query, limit=24)]
            if source == 'FOSShub':
                category = self.search_category.get()
                if category not in {'Auto-detect', 'Software'}:
                    raise ValueError('FOSShub is connected to the Software category')
                return [{**item, 'category': 'Software',
                         'display_source': 'Metadata · FOSShub'}
                        for item in search_fosshub_foss(query, limit=30)]
            if source in {'SourceForge', 'GitHub (FOSS)'}:
                category = self.search_category.get()
                if category not in {'Auto-detect', 'Software'}:
                    raise ValueError(f'{source} is connected to the Software category')
                source_key = 'sourceforge.net' if source == 'SourceForge' else 'github_foss'
                approval = source_download_rights(
                    load_rights_evidence(self.rights_evidence_path), source_key)
                if not approval:
                    raise ValueError(
                        f'{source} is NO in the local rights-evidence.json file. '
                        'Set approved_for_download to true before searching it.')
                searcher = search_sourceforge_foss if source == 'SourceForge' else search_github_foss
                results = searcher(query, limit=30)
                for item in results:
                    item['category'] = 'Software'
                    item['downloadable'] = True
                    item['rights_evidence_path'] = str(self.rights_evidence_path)
                return results
            if source == 'Internet Archive':
                category = self.search_category.get()
                if category == 'Auto-detect':
                    category = 'Books'
                return search_internet_archive(query, category=category, limit=30)
            if source in {'Meawfy', 'SoftArchive'}:
                category = self.search_category.get()
                if category == 'Auto-detect':
                    category = 'Books'
                resolver = (resolve_meawfy_downloads if source == 'Meawfy'
                            else resolve_softarchive_downloads)
                searcher = search_meawfy if source == 'Meawfy' else search_softarchive
                resolved = []
                for result in searcher(query):
                    resolved.extend(resolver(result, allowed_formats=formats_for_category(category)))
                return resolved
            anime_sources = {provider: (source_key, searcher, resolver)
                             for source_key, provider, searcher, resolver
                             in ANIME_DOWNLOAD_ADAPTERS}
            if source in anime_sources:
                category = self.search_category.get()
                if category == 'Auto-detect':
                    category = 'Anime'
                if category != 'Anime':
                    raise ValueError(f'{source} is only connected to the Anime category')
                source_key, searcher, resolver = anime_sources[source]
                approval = source_download_rights(
                    load_rights_evidence(self.rights_evidence_path), source_key)
                if not approval:
                    raise ValueError(
                        f'{source} is NO in the local rights-evidence.json file. '
                        'Set approved_for_download to true before searching it.')
                resolved = []
                for result in searcher(query):
                    for item in resolver(result, allowed_formats=formats_for_category('Anime')):
                        item['category'] = 'Anime'
                        item['downloadable'] = True
                        item['rights_evidence_path'] = str(self.rights_evidence_path)
                        resolved.append(item)
                return resolved
            if source == 'FMHY directory':
                return [{'title': f"Search {site['name']}", 'author': site['section'],
                         'format': 'link', 'source': site['search_url'], 'url': site['search_url'],
                         'fmhy': True} for site in candidates(query, limit=30)]
            if source == 'NovelArchive':
                return search_novelarchive(query)
            if source == 'LNORI':
                return search_lnori(query)
            if source == 'RanobeDB':
                return search_ranobedb(query)
            if source == 'MangaUpdates':
                return search_mangaupdates(query)
            if source == 'GetComics':
                return search_getcomics(query)
            if source == 'Public-domain comic mirror':
                if not base:
                    raise ValueError('Enter your separate public-domain comic mirror base URL above')
                return search_public_domain_comic_mirror(query, base)
            site = 'libgen' if source == 'LibGen' else 'annas'
            found = search_site(site, query, base=base)
            resolved = []
            for result in found[:20]:
                if not result.get('detail_url'): continue
                try:
                    resolved.extend(resolve_detail(site, result['detail_url'], result.get('title', query), result.get('author', 'Unknown'), result.get('format')))
                except Exception:
                    self.logger.exception('Could not resolve a %s result detail page', site)
                    continue
            return resolved
        self._worker(f'Searching {source}…', action, self.show_results)

    def show_category_results(self, response):
        results = response.get('results') or []
        errors = response.get('errors') or []
        self.show_results(results)
        self._set_more_pages(response.get('connector_pages') or {})
        if response.get('connector_skipped') is not None or response.get('notices'):
            self.logger.info('Source connectors skipped: %s; notices: %s',
                             response.get('connector_skipped'), response.get('notices'))
        searched = len(response.get('searched') or [])
        correction = response.get('query_correction') or {}
        if response.get('comic_summary'):
            counts = response['comic_summary']
            self._done(f"{counts['issue']} issues, {counts['series']} series, {counts['collection']} collected editions; "
                       f"{len(errors)} source errors" + ('; results capped, refine your search' if counts.get('truncated') else ''))
            return
        corrected = (f"Corrected {correction['from']!r} to {correction['to']!r}. "
                     if correction.get('from') and correction.get('to') else '')
        if response.get('series_name'):
            bundle = next((row.get('series_bundle') for row in results
                           if isinstance(row.get('series_bundle'), dict)), {})
            members = bundle.get('books') or []
            announced_count = sum(1 for member in members if member.get('publication_status') ==
                                  'announced; release date unconfirmed')
            published_count = max(0, len(members) - announced_count)
            if announced_count:
                counts = (f'{published_count} released continuity works and '
                          f'{announced_count} announced work' +
                          ('s' if announced_count != 1 else ''))
            else:
                counts = f'{len(members)} works'
            summary = (f"{corrected}{counts} in {response['series_name']}, ordered by series "
                       'position. Select any row to watch the full series.')
            plan = bundle.get('series_plan') or {}
            if plan.get('planned_mainline_novel_count'):
                next_number = plan.get('next_mainline_number')
                summary += (f" The {plan['planned_mainline_novel_count']}-novel plan continues with "
                            f"Book {next_number}, whose title and release date are unannounced.")
            if announced_count:
                summary += ' Announced works wait in Watched without searching until a date is confirmed.'
            review_count = int(response.get('series_review_count') or 0)
            if review_count:
                summary += (f' {review_count} uncertain catalog member(s) excluded from the '
                            'series queue; they will be saved for review when the series is watched.')
            if errors:
                summary += f' {len(errors)} connector error(s) across {searched} searches; details are in logs.'
            self._done(summary)
        elif errors:
            first = next((error for error in errors if any(error.startswith(spec.label + ':')
                                                            for spec in SOURCE_CONNECTORS.values())), '')
            self._done(f'{corrected}{len(results)} result(s); {len(errors)} connector error(s) across {searched} '
                       f'searches' + (f'. {first}' if first else ' (details in logs)'))
            for error in errors:
                self.logger.warning('Search source error: %s', error)
        else:
            self._done(f'{corrected}{len(results)} result(s) across {searched} connected searches')

    def add_selected_search_to_watched(self):
        selected = self.results_view.selection()
        if not selected:
            return
        if len(selected) > 1:
            self._preview_book_series_batch([dict(self.results[int(index)]) for index in selected])
            return
        item = dict(self.results[int(selected[0])])
        category = self._search_result_category(item)
        if (item.get('series_bundle') or item.get('metadata_variants') or
                item.get('discovery_only') or item.get('format') == 'catalog'):
            self._show_import_preview(item, category)
            return
        self._add_search_item_to_watched(item, category)

    def _preview_book_series_batch(self, items):
        series = [dict(item.get('series_bundle') or item) for item in items]
        if any(self._search_result_category(item) != 'Books' or record.get('kind') != 'series'
               for item, record in zip(items, series)):
            messagebox.showinfo('Add selected series', 'For a batch, select book series results with Ctrl-click or Shift-click. Other media can be added individually.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Watch selected book series')
        dialog.geometry('650x400')
        ttk.Label(dialog, text=f'Watch {len(series)} book series', font=('TkDefaultFont', 12, 'bold')).pack(anchor='w', padx=12, pady=12)
        text = tk.Text(dialog, wrap='word')
        text.pack(fill='both', expand=True, padx=12)
        text.insert('1.0', '\n'.join(f"{record['title']} — {len(record.get('books') or [])} listed books" for record in series)
                    + '\n\nExisting series and books are reused. Upcoming books wait for their release dates. Downloads use your saved sources and library folder.')
        text.configure(state='disabled')
        sources = self._watch_sources()
        profile_id = self.active_profile_id.get().strip()
        def start():
            dialog.destroy()
            from .book_batch import create_book_batch
            self._start_book_batch(create_book_batch(self.db, series, sources, profile_id=profile_id))
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=12)
        ttk.Button(bar, text=f'Watch {len(series)} series', command=start).pack(side='right')
        ttk.Button(bar, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)

    def _start_book_batch(self, batch_id, resumed=False):
        if batch_id in self._running_book_batches:
            return
        self._running_book_batches.add(batch_id)
        data_root = self.data_root
        def action():
            from .book_batch import run_book_batch
            db = connect(data_root)
            try:
                return run_book_batch(db, batch_id, progress=lambda index, total, result:
                    self.root.after(0, lambda value=f"Batch {batch_id}: {index}/{total} {result['title']} ({result['status']})":
                                    self.status.set(value)))
            finally:
                db.close()
        def finished(result):
            self._running_book_batches.discard(batch_id)
            self.refresh_watches()
            self.refresh_queue()
            message = (f"Batch {batch_id} {result['state']}: {result['added']} series added, "
                       f"{result['existing']} existing, {result['failed']} failed; {result['books_added']} new books")
            if result['remaining']:
                message += f"; {result['remaining']} series not yet processed"
            self._done(message)
            if result['failed']:
                messagebox.showwarning('Some series need attention', '\n'.join(
                    f"{row['title']}: {row['error']}" for row in result['results'] if row['status'] == 'failed')
                    + '\n\nOpen Book batches to retry them.')
            if result['added'] or result['existing']:
                self._run_added_watch_scan()
        def failed(error):
            self._running_book_batches.discard(batch_id)
            messagebox.showerror('Media Intake', f'Book batch {batch_id} stopped: {error}\n\nDetails saved to {self.log_file}')
        self._worker(f"{'Resuming' if resumed else 'Adding'} book batch {batch_id}…", action, finished, failed)

    def _resume_book_batches(self):
        from .book_batch import resumable_book_batches
        try:
            batch_ids = resumable_book_batches(self.db)
        except Exception:
            self.logger.exception('Could not read unfinished book batches')
            return
        for batch_id in batch_ids:
            self._start_book_batch(batch_id, resumed=True)

    def open_book_batches(self):
        from .book_batch import (book_batch_summary, cancel_book_batch, list_book_batches,
                                 pause_book_batch, retry_book_batch)
        existing = getattr(self, '_book_batches_dialog', None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        dialog = tk.Toplevel(self.root)
        self._book_batches_dialog = dialog
        dialog.title('Book batches')
        dialog.geometry('900x560')
        dialog.transient(self.root)
        batch_columns = ('state', 'created', 'series', 'added', 'existing', 'failed', 'review', 'remaining',
                         'downloaded', 'upcoming', 'book_failed', 'book_pending')
        headings = ('State', 'Created (UTC)', 'Series', 'Added', 'Existing', 'Failed', 'Needs choice',
                    'Not started', 'Books downloaded', 'Upcoming', 'Book errors', 'Waiting')
        batches = ttk.Treeview(dialog, columns=batch_columns, show='headings', height=7, selectmode='browse')
        for column, heading in zip(batch_columns, headings):
            batches.heading(column, text=heading)
            batches.column(column, width=135 if column == 'created' else 72, anchor='w')
        batches.pack(fill='x', padx=12, pady=(12, 6))
        series_columns = ('status', 'downloaded', 'upcoming', 'failed', 'pending', 'detail')
        series = ttk.Treeview(dialog, columns=series_columns, show='tree headings', selectmode='none')
        series.heading('#0', text='Series')
        series.column('#0', width=260)
        for column, heading, width in (('status', 'Status', 80), ('downloaded', 'Downloaded', 80),
                                       ('upcoming', 'Upcoming', 70), ('failed', 'Errors', 60),
                                       ('pending', 'Waiting', 60), ('detail', 'Detail', 300)):
            series.heading(column, text=heading)
            series.column(column, width=width, anchor='w')
        series.pack(fill='both', expand=True, padx=12)
        def selected_batch():
            selection = batches.selection()
            return int(selection[0]) if selection else None
        def show_series(*_):
            series.delete(*series.get_children())
            batch_id = selected_batch()
            if batch_id is None:
                return
            for row in book_batch_summary(self.db, batch_id)['results']:
                progress = row.get('progress') or {}
                detail = row.get('error') or (f"{progress.get('books', 0)} books, {row.get('books_added', 0)} new"
                                              if progress else '')
                series.insert('', 'end', text=row['title'] or 'Untitled', values=(
                    row['status'], progress.get('downloaded', ''), progress.get('upcoming', ''),
                    progress.get('failed', ''), progress.get('pending', ''), detail))
        def refresh():
            if not dialog.winfo_exists():
                return
            keep = selected_batch()
            batches.delete(*batches.get_children())
            for summary in list_book_batches(self.db, limit=50):
                state = summary['state']
                if summary['batch_id'] in self._running_book_batches and state == 'running':
                    state = 'running here'
                books = summary['books']
                batches.insert('', 'end', iid=str(summary['batch_id']), values=(
                    state, summary['created_at'], summary['total'], summary['added'], summary['existing'],
                    summary['failed'], summary['review'], summary['remaining'] + summary['cancelled'],
                    books['downloaded'],
                    books['upcoming'], books['failed'], books['pending']))
            children = batches.get_children()
            if keep is not None and str(keep) in children:
                batches.selection_set(str(keep))
            elif children:
                batches.selection_set(children[0])
            show_series()
            refresh_job[0] = dialog.after(3000, refresh)
        refresh_job = [None]
        batches.bind('<<TreeviewSelect>>', show_series)
        def control(operation):
            batch_id = selected_batch()
            if batch_id is None:
                return
            if operation == 'pause':
                pause_book_batch(self.db, batch_id)
            elif operation == 'cancel':
                if not messagebox.askyesno('Cancel book batch', 'Stop this batch? Series already added stay in Watched.', parent=dialog):
                    return
                cancel_book_batch(self.db, batch_id)
            elif operation == 'retry':
                if not retry_book_batch(self.db, batch_id, include_cancelled=True):
                    messagebox.showinfo('Retry book batch', 'This batch has no failed or cancelled series.', parent=dialog)
                    return
                self._start_book_batch(batch_id)
            elif operation == 'resume':
                if book_batch_summary(self.db, batch_id)['state'] not in ('paused', 'pending', 'running'):
                    return
                self._start_book_batch(batch_id, resumed=True)
            refresh_now()
        def refresh_now():
            if refresh_job[0]:
                dialog.after_cancel(refresh_job[0])
            refresh()
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=12)
        ttk.Button(bar, text='Pause', command=lambda: control('pause')).pack(side='left')
        ttk.Button(bar, text='Resume', command=lambda: control('resume')).pack(side='left', padx=6)
        ttk.Button(bar, text='Cancel batch', command=lambda: control('cancel')).pack(side='left')
        ttk.Button(bar, text='Retry failed/cancelled', command=lambda: control('retry')).pack(side='left', padx=6)
        ttk.Button(bar, text='Review matches',
                   command=lambda: selected_batch() is not None and self.open_batch_review(selected_batch())).pack(side='left')
        ttk.Button(bar, text='Add series from a list…', command=self.open_series_list_import).pack(side='left', padx=6)
        ttk.Button(bar, text='Close', command=dialog.destroy).pack(side='right')
        refresh()

    def _books_search_function(self):
        """A thread-safe title search returning Books category rows, as the Search tab uses."""
        options = self._watch_sources()
        priority = get_download_priority(self.db, 'Books', options)
        credentials = {'comicvine_api_key': self.comicvine_api_key, 'metron_api_token': self.metron_api_token,
                       'taddy_user_id': self.taddy_user_id, 'taddy_api_key': self.taddy_api_key,
                       'tmdb_api_key': self.tmdb_api_key, 'igdb_client_id': self.igdb_client_id,
                       'igdb_client_secret': self.igdb_client_secret}
        return lambda title: (resolve_media_query(title, 'Books', options, credentials, priority) or {}).get('results') or []

    def open_series_list_import(self):
        from .book_batch import create_title_batch, parse_title_list
        dialog = tk.Toplevel(self.root)
        dialog.title('Add book series from a list')
        dialog.geometry('560x460')
        dialog.transient(self.root)
        ttk.Label(dialog, wraplength=520, justify='left', text=(
            'Paste up to 100 book series, one per line. Bullets and numbering are ignored. '
            'Clear matches are added automatically; anything uncertain waits for you to choose.')).pack(
            anchor='w', padx=12, pady=(12, 6))
        text = tk.Text(dialog, wrap='word', height=16)
        text.pack(fill='both', expand=True, padx=12)
        status = tk.StringVar()
        ttk.Label(dialog, textvariable=status).pack(anchor='w', padx=12, pady=(6, 0))
        data_root, sources = self.data_root, self._watch_sources()
        profile_id = self.active_profile_id.get().strip()
        search = self._books_search_function()
        def find():
            try:
                titles = parse_title_list(text.get('1.0', 'end'))
            except ValueError as exc:
                status.set(str(exc))
                return
            if not titles:
                status.set('Paste at least one series title.')
                return
            find_button.configure(state='disabled')
            def action():
                db = connect(data_root)
                try:
                    return create_title_batch(db, titles, search, sources, profile_id, progress=lambda index, total, title:
                        self.root.after(0, lambda: status.set(f'Searched {index}/{total}: {title}')))
                finally:
                    db.close()
            def found(batch_id):
                if dialog.winfo_exists():
                    dialog.destroy()
                self.open_batch_review(batch_id)
            def failed(error):
                if dialog.winfo_exists():
                    find_button.configure(state='normal')
                    status.set(f'Search failed: {error}')
            self._worker(f'Matching {len(titles)} series title(s)…', action, found, failed)
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=12)
        find_button = ttk.Button(bar, text='Find matches', command=find)
        find_button.pack(side='right')
        ttk.Button(bar, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)

    def open_batch_review(self, batch_id):
        from .book_batch import (book_batch_summary, choose_candidate, reopen_item, review_items,
                                 skip_item, start_draft)
        dialog = tk.Toplevel(self.root)
        dialog.title(f'Review series matches (batch {batch_id})')
        dialog.geometry('900x520')
        dialog.transient(self.root)
        heading = tk.StringVar()
        ttk.Label(dialog, textvariable=heading, wraplength=860, justify='left').pack(anchor='w', padx=12, pady=(12, 6))
        panes = ttk.Frame(dialog)
        panes.pack(fill='both', expand=True, padx=12)
        titles = ttk.Treeview(panes, columns=('state', 'note'), show='tree headings', selectmode='browse')
        titles.heading('#0', text='Your title')
        titles.column('#0', width=230)
        titles.heading('state', text='Status')
        titles.column('state', width=90)
        titles.heading('note', text='Note')
        titles.column('note', width=160)
        titles.pack(side='left', fill='both', expand=True)
        candidates = ttk.Treeview(panes, columns=('books', 'author', 'sources', 'match'),
                                  show='tree headings', selectmode='browse')
        candidates.heading('#0', text='Catalog series')
        candidates.column('#0', width=230)
        for column, label, width in (('books', 'Books', 50), ('author', 'Author', 120),
                                     ('sources', 'Sources', 110), ('match', 'Match', 55)):
            candidates.heading(column, text=label)
            candidates.column(column, width=width, anchor='w')
        candidates.pack(side='left', fill='both', expand=True, padx=(8, 0))
        state = {'items': {}}
        labels = {'review': 'Choose', 'not_found': 'Not found', 'skipped': 'Skipped'}
        def refresh(keep=None):
            summary = book_batch_summary(self.db, batch_id)
            ready = sum(row['status'] == 'pending' for row in summary['results'])
            heading.set(f"{summary['total']} title(s): {ready} ready to add, {summary['added'] + summary['existing']} "
                        f"already added, {summary['review']} need a choice, {summary['not_found']} not found, "
                        f"{summary['skipped']} skipped.")
            add_button.configure(text=f'Add {ready} ready series' if ready else 'Nothing ready to add',
                                 state='normal' if ready else 'disabled')
            titles.delete(*titles.get_children())
            state['items'] = {}
            for item in review_items(self.db, batch_id):
                iid = str(item['item_id'])
                state['items'][iid] = item
                titles.insert('', 'end', iid=iid, text=item['requested_title'],
                              values=(labels.get(item['state'], item['state']), item['error'] or
                                      f"{len(item['candidates'])} possible match(es)"))
            children = titles.get_children()
            if keep in children:
                titles.selection_set(keep)
            elif children:
                titles.selection_set(children[0])
            show_candidates()
        def show_candidates(*_):
            candidates.delete(*candidates.get_children())
            selected = titles.selection()
            item = state['items'].get(selected[0]) if selected else None
            for index, candidate in enumerate((item or {}).get('candidates') or []):
                candidates.insert('', 'end', iid=str(index), text=candidate['title'], values=(
                    candidate['books'], candidate['author'], ', '.join(candidate['providers']),
                    f"{round(candidate['score'] * 100)}%"))
            if candidates.get_children():
                candidates.selection_set('0')
        titles.bind('<<TreeviewSelect>>', show_candidates)
        def current_item():
            selected = titles.selection()
            return state['items'].get(selected[0]) if selected else None
        def use_match():
            item, chosen = current_item(), candidates.selection()
            if not item or not chosen:
                return
            choose_candidate(self.db, item['item_id'], int(chosen[0]))
            refresh()
        def skip():
            item = current_item()
            if item:
                skip_item(self.db, item['item_id'])
                refresh(str(item['item_id']))
        def reopen():
            item = current_item()
            if item:
                reopen_item(self.db, item['item_id'])
                refresh(str(item['item_id']))
        def add_ready():
            if start_draft(self.db, batch_id):
                self._start_book_batch(batch_id)
            dialog.destroy()
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=12)
        ttk.Button(bar, text='Use selected match', command=use_match).pack(side='left')
        ttk.Button(bar, text='Skip title', command=skip).pack(side='left', padx=6)
        ttk.Button(bar, text='Reconsider', command=reopen).pack(side='left')
        add_button = ttk.Button(bar, text='Add ready series', command=add_ready)
        add_button.pack(side='right')
        ttk.Button(bar, text='Close (keeps choices)', command=dialog.destroy).pack(side='right', padx=6)
        refresh()

    def _show_import_preview(self, item, category):
        bundle = item.get('series_bundle') or {}
        members = bundle.get('books') or []
        future_members = [book for book in members if (
            book.get('publication_status') == 'announced; release date unconfirmed' or
            (book.get('release_date') and book['release_date'] > dt.date.today().isoformat()))]
        variants = [row for row in (item.get('metadata_variants') or [])
                    if isinstance(row, dict)]
        providers = list(dict.fromkeys(
            row.get('provider') for row in variants if row.get('provider')))
        identifiers = [external_id for row in variants
                       for external_id in (row.get('external_ids') or
                                            (row.get('catalog_metadata') or {}).get('external_ids') or [])
                       if isinstance(external_id, dict) and external_id.get('external_id')]
        authors = {str(row.get('author')).casefold() for row in variants
                   if row.get('author') and str(row.get('author')).casefold() != 'unknown'}
        title = str(item.get('title') or bundle.get('title') or 'Untitled')
        subtype = (item.get('video_subtype') or item.get('game_subtype') or item.get('kind') or '')
        collapsed = max(0, len(variants) - 1)
        ambiguities = []
        if len(authors) > 1:
            ambiguities.append('catalogs disagree on the author')
        if variants and not identifiers:
            ambiguities.append('no stable catalog ID was found')
        if len(providers) > 1:
            ambiguities.append(f'{len(providers)} metadata providers returned a match')
        rows = [f'Name: {title}', f'Media category: {category}',
                f'Type: {str(subtype).replace("_", " ").title() or "not classified"}',
                f'Release date: {item.get("release_date") or "not listed"}',
                f'Metadata sources: {", ".join(providers) or item.get("provider") or "none"}',
                f'Equivalent catalog results collapsed: {collapsed}',
                f'Direct installments found: {len(members)}',
                f'Future / undated announced installments: {len(future_members)}',
                f'Series entries held for review: {len(bundle.get("review_members") or [])}',
                f'Download candidates found: {len(item.get("download_variants") or [])}',
                f'Identity notes: {"; ".join(ambiguities) if ambiguities else "no provider conflict detected"}']
        if category == 'Comics':
            rows.insert(3, f"Issue number: {item.get('issue_number') or 'not an individual issue'}; "
                        f"series run: {item.get('series_start_year') or 'year unknown'}")
            rows.insert(4, f"Cover editions retained: {len(item.get('edition_variants') or [])}")
        dialog = tk.Toplevel(self.root)
        dialog.title('Import preview')
        dialog.geometry('660x420')
        dialog.transient(self.root)
        ttk.Label(dialog, text='Review what will be added to Watched',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w', padx=12, pady=(12, 5))
        guidance = ('This adds only the selected issue to Watched. Alternate metadata and cover editions stay attached. '
                    'Downloads use your existing source settings; collected editions remain separate.'
                    if category == 'Comics' and item.get('comic_entity') == 'issue' else
                    'Series members will be added once each; future entries stay scheduled until their release date. Related works and uncertain matches remain in the continuity review flow.')
        ttk.Label(dialog, text=guidance,
                  wraplength=625, justify='left').pack(anchor='w', padx=12, pady=(0, 8))
        summary = tk.Text(dialog, height=12, wrap='word')
        summary.pack(fill='both', expand=True, padx=12, pady=5)
        summary.insert('1.0', '\n'.join(rows))
        summary.configure(state='disabled')
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=12, pady=(5, 12))
        ttk.Button(buttons, text='Add to Watched',
                   command=lambda: (dialog.destroy(), self._add_search_item_to_watched(item, category))).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)

    def _add_search_item_to_watched(self, item, category):
        series_bundle = item.get('series_bundle')
        if category == 'Books' and isinstance(series_bundle, dict):
            self._associate_book_metadata(series_bundle)
            return
        metadata_variants = item.get('metadata_variants') or []
        if (not metadata_variants and not item.get('discovery_only')
                and item.get('format') not in {'catalog', 'link'}
                and category not in {'Books', 'Light novels', 'Web serials', 'Comics', 'Manga'}):
            messagebox.showinfo('Add to Watched', 'This result is a downloadable file. Use “Queue selected result” to add it to the download queue.')
            return
        if category in {'Books', 'Light novels', 'Web serials'}:
            if metadata_variants:
                self._associate_book_metadata(metadata_variants[0])
            elif item.get('download_variants'):
                self.watch_goodreads_profile = {}
                self.watch_comic_profile = {}
                self.watch_lightnovel_profile = {}
                self.watch_url.set('')
                self.watch_title.set(item.get('title') or '')
                self.watch_author.set('' if item.get('author') == 'Unknown' else item.get('author', ''))
                watch_kind = {'Light novels': 'Light novel', 'Web serials': 'Webfiction'}
                self.watch_kind.set(watch_kind.get(category, 'Book'))
                self.add_watched(auto_scan=True)
            else:
                self._associate_book_metadata(item)
            return
        if category in {'Comics', 'Manga'}:
            if category == 'Comics' and (item.get('comic_entity') == 'issue' or item.get('kind') == 'comic_issue'):
                from .comic_metadata import add_issue_watch
                data_root, sources = self.data_root, self._watch_sources()
                profile_id = self.active_profile_id.get().strip()
                def action():
                    db = connect(data_root)
                    try:
                        return add_issue_watch(db, item, sources, profile_id)
                    finally:
                        db.close()
                def added(_watch_id):
                    self.refresh_watches()
                    self._done('Watching ' + item['title'])
                    self._run_added_watch_scan()
                self._worker('Adding comic issue to Watched', action, added)
                return
            if metadata_variants:
                item = {**metadata_variants[0], **item} if category == 'Comics' else dict(metadata_variants[0])
            if (category == 'Comics' and item.get('metadata_provider', item.get('provider')) in
                    {'GCD', 'Metron', 'Wikidata'} and not item.get('comicvine_id')
                    and not any(item.get(field) for field in ('feed_url', 'sss_url', 'taddy_id'))):
                messagebox.showinfo('Comic catalog metadata',
                    'This catalog currently supports metadata lookup and individual issue watches. '
                    'Select a numbered issue to watch it, or use a supported content/feed URL for the series. '
                    'Automatic series release tracking for this catalog is planned.')
                return
            item.update(item.get('metadata') or {})
            if item.get('provider') == 'MangaUpdates':
                item['kind'] = 'manga'
                item['source_url'] = item.get('source_url') or item.get('url')
            item.setdefault('kind', 'comic' if category == 'Comics' else 'manga')
            self.associate_comic_metadata(item)
            return
        if category == 'Fanfiction':
            if metadata_variants:
                item = dict(metadata_variants[0])
            self.watch_goodreads_profile = {}
            self.watch_url.set(item.get('source_url') or item.get('url') or '')
            self.watch_title.set(item.get('title') or 'Fanfiction story')
            self.watch_author.set(item.get('author') or '')
            self.watch_kind.set('Fanfiction')
            self.add_watched(auto_scan=True)
            return
        if category == 'Software':
            self.watch_goodreads_profile = {}
            self.watch_comic_profile = {}
            self.watch_lightnovel_profile = {}
            self.watch_url.set(item.get('source_url') or item.get('url') or '')
            self.watch_title.set(item.get('title') or '')
            self.watch_author.set('')
            self.watch_kind.set('Software')
            self.add_watched(auto_scan=True)
            return
        if category in {'Anime', 'Videos', 'Games'}:
            self.associate_media_catalog(item, category)
            return
        messagebox.showinfo(
            'Add to Watched',
            f'{category} metadata is available to open. Scheduled {category.casefold()} watching is part of the media roadmap.')

    def open_forum_helper(self):
        window = tk.Toplevel(self.root)
        window.title('Snahp forum browser helper')
        window.geometry('500x220')
        window.resizable(False, False)
        window.transient(self.root)
        ttk.Label(
            window,
            text=('Open the embedded browser, sign in manually, then search the forum. Click “Import thread titles” '
                  'on the page; Media Intake will parse only same-forum topic titles and links. The browser profile '
                  'stays in this app’s data folder. You can also load a saved HTML page manually.'),
            wraplength=465, justify='left').pack(fill='x', padx=14, pady=(14, 10))
        controls = ttk.Frame(window)
        controls.pack(fill='x', padx=14, pady=(0, 12))
        ttk.Button(controls, text='Open embedded sign-in browser',
                   command=self.start_forum_download_watch).pack(side='left')
        ttk.Button(controls, text='Load saved HTML', command=self.load_saved_forum_results).pack(side='left', padx=8)
        ttk.Button(controls, text='Close', command=window.destroy).pack(side='right')

    def start_forum_download_watch(self):
        query = self.query.get().strip()
        if not query:
            messagebox.showinfo('Snahp forum', 'Enter the search term before starting the helper.')
            return
        downloads = Path.home() / 'Downloads'
        if not downloads.is_dir():
            chosen = filedialog.askdirectory(title='Choose the folder where you save forum pages')
            if not chosen:
                return
            downloads = Path(chosen)
        self._forum_query = query
        self._forum_download_baseline = {}
        self._forum_download_stability = {}
        capture_folder = self.data_root / 'browser-captures'
        capture_folder.mkdir(parents=True, exist_ok=True)
        self._forum_download_folders = [capture_folder, downloads]
        for folder in self._forum_download_folders:
            for path in folder.glob('*'):
                if path.is_file() and path.suffix.casefold() in ('.html', '.htm'):
                    try:
                        stat = path.stat()
                        self._forum_download_baseline[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size)
                    except OSError:
                        continue
        self._forum_download_watch = True
        opened = self.open_embedded_browser('https://fora.snahp.eu/ucp.php?mode=login')
        if opened:
            self._done('Embedded browser ready; sign in and click Import thread titles on the search page')
            self.root.after(1800, self._poll_forum_downloads)
        else:
            self._forum_download_watch = False
            messagebox.showerror('Snahp forum', 'Could not start the embedded browser.')

    def open_embedded_browser(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.hostname != 'fora.snahp.eu':
            messagebox.showerror('Embedded browser', 'This sign-in helper is restricted to the Snahp forum host.')
            return False
        if self._embedded_browser_process and self._embedded_browser_process.poll() is None:
            self._done('Embedded browser is already open; use its current tab')
            return True
        profile = self.data_root / 'browser-profile'
        captures = self.data_root / 'browser-captures'
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--embedded-browser', url, str(profile), str(captures)]
        else:
            script = Path(__file__).resolve().parents[1] / 'run_gui.py'
            command = [sys.executable, str(script), '--embedded-browser', url, str(profile), str(captures)]
        try:
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            self._embedded_browser_process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
            return True
        except OSError:
            self.logger.exception('Could not launch embedded browser')
            return False

    def _poll_forum_downloads(self):
        if not self._forum_download_watch:
            return
        ready = []
        try:
            for folder in self._forum_download_folders:
                for path in folder.glob('*'):
                    if not path.is_file() or path.suffix.casefold() not in ('.html', '.htm'):
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    signature = (stat.st_mtime_ns, stat.st_size)
                    key = str(path.resolve())
                    if key in self._forum_download_baseline and self._forum_download_baseline[key] == signature:
                        continue
                    prior, count = self._forum_download_stability.get(key, (None, 0))
                    count = count + 1 if prior == signature else 1
                    self._forum_download_stability[key] = (signature, count)
                    if count >= 2 and stat.st_size <= 5_000_000:
                        ready.append((path, signature))
        except OSError as exc:
            self.logger.exception('Could not check Downloads for saved Snahp pages')
            self._done(f'Snahp import watch error: {exc}')
        if ready and not self._forum_import_in_progress:
            self._forum_import_in_progress = True
            query = self._forum_query
            self._busy('Importing saved Snahp results…')

            def run():
                imported = []
                for path, signature in ready:
                    try:
                        results = search_forum_threads(query, html_path=str(path))
                        imported.append((str(path), signature, results, None))
                    except Exception as exc:
                        self.logger.error('Could not import saved forum page %s: %s\n%s',
                                          path, exc, traceback.format_exc())
                        imported.append((str(path), signature, [], str(exc)))
                    if path.parent.resolve() == (self.data_root / 'browser-captures').resolve():
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            self.logger.exception('Could not remove temporary embedded-browser capture %s', path)
                self.root.after(0, lambda: self._finish_forum_auto_import(query, imported))

            threading.Thread(target=run, daemon=True).start()
        elif self._forum_download_watch:
            self.root.after(1800, self._poll_forum_downloads)

    def _finish_forum_auto_import(self, query, imported):
        self._forum_import_in_progress = False
        self._forum_download_watch = False
        combined = []
        errors = []
        for path, signature, results, error in imported:
            self._forum_download_baseline[path] = signature
            if error:
                errors.append(error)
            else:
                combined.extend(results)
        if query != self._forum_last_query or not all(row.get('forum_thread') for row in self.results):
            existing = []
        else:
            existing = list(self.results)
        merged = {row.get('url'): row for row in existing if row.get('url')}
        merged.update({row.get('url'): row for row in combined if row.get('url')})
        self._forum_last_query = query
        self.source.set('Snahp forum threads')
        self.show_results(list(merged.values()))
        if errors:
            messagebox.showwarning('Snahp import', '\n'.join(errors[-2:]) + f'\n\nDetails saved to {self.log_file}')
        elif not combined:
            self._done('Saved Snahp page imported; no thread links were found')
        else:
            self._done(f'Imported {len(combined)} Snahp thread result(s)')

    def load_saved_forum_results(self):
        path = filedialog.askopenfilename(
            initialdir=str(self.data_root), title='Choose saved forum search HTML',
            filetypes=[('HTML pages', '*.html *.htm'), ('All files', '*.*')])
        if not path:
            return
        query = self.query.get().strip()
        if not query:
            messagebox.showinfo('Forum search results', 'Enter the search term used for this saved forum page.')
            return
        self.source.set('Snahp forum threads')
        self._worker('Reading saved forum results…',
                     lambda: search_forum_threads(query, html_path=path), self.show_results)

    EXTERNAL_DOWNLOAD_KEYS = ('downloader', 'gopeed_url', 'xdm_path', 'command', 'directory',
                              'jackett_url', 'jackett_indexer')
    EXTERNAL_DOWNLOAD_SECRETS = ('gopeed_token', 'jackett_api_key')

    def _load_external_download_settings(self):
        settings = {'downloader': 'gopeed', 'gopeed_url': 'http://127.0.0.1:9999',
                    'jackett_url': 'http://127.0.0.1:9117', 'jackett_indexer': 'all'}
        for key in self.EXTERNAL_DOWNLOAD_KEYS:
            value = get_setting(self.db, f'external_dl_{key}')
            if value is not None:
                settings[key] = value
        for key in self.EXTERNAL_DOWNLOAD_SECRETS:
            try:
                settings[key] = get_local_secret(self.db, key) or ''
            except Exception:
                settings[key] = ''
                self.logger.warning('Saved %s could not be unlocked; re-enter it in Settings.', key)
        return settings

    def _build_external_download_settings(self, parent):
        from .external_downloaders import DOWNLOADER_LABELS
        from .core import download_settings
        from . import transfer
        engine = ttk.LabelFrame(parent, text='Download engine', padding=8)
        engine.pack(fill='x', pady=(0, 10))
        current = download_settings(self.db)
        engine_vars = {key: tk.StringVar(value=str(current[key])) for key in current}
        for row, (label, key, low, high) in enumerate((
                ('Connections per file (1–16)', 'connections', 1, 16),
                ('Speed limit, KB/s (0 = unlimited)', 'speed_limit_kbps', 0, 10_000_000),
                ('Retries per connection', 'retries', 0, 10))):
            ttk.Label(engine, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=4)
            ttk.Spinbox(engine, from_=low, to=high, textvariable=engine_vars[key], width=12).grid(
                row=row, column=1, sticky='w', pady=4)
        for row, (label, key, low, high) in enumerate((
                ('Start automatic downloads only from hour (0–23)', 'window_start_hour', 0, 23),
                ('…until hour (same as start = any time)', 'window_end_hour', 0, 23)), start=3):
            ttk.Label(engine, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=4)
            ttk.Spinbox(engine, from_=low, to=high, textvariable=engine_vars[key], width=12).grid(
                row=row, column=1, sticky='w', pady=4)
        for row, (label, key, low, high) in enumerate((
                ('Seed torrents until ratio, % (0 = no ratio target)', 'seed_ratio_pct', 0, 10_000),
                ('…or for at most, minutes (0 = no time limit)', 'seed_minutes', 0, 525_600),
                ('Upload limit, KB/s (0 = unlimited)', 'upload_limit_kbps', 0, 10_000_000)), start=8):
            ttk.Label(engine, text=label).grid(row=row, column=0, sticky='w', padx=(0, 8), pady=4)
            ttk.Spinbox(engine, from_=low, to=high, textvariable=engine_vars[key], width=12).grid(
                row=row, column=1, sticky='w', pady=4)
        engine_status = tk.StringVar(value='Large files download over several connections, resume after '
                                     'pauses, errors and restarts, and stay within the speed limit.')
        def save_engine():
            for key, variable in engine_vars.items():
                if key == 'autostart':
                    continue
                try:
                    value = max(0, int(variable.get()))
                except ValueError:
                    engine_status.set(f'{key.replace("_", " ")} must be a whole number')
                    return
                set_setting(self.db, f'download_{key}', value)
            saved = download_settings(self.db)
            transfer.LIMITER.set_rate(saved['speed_limit_kbps'] * 1024)
            from .torrents import set_upload_limit
            set_upload_limit(saved['upload_limit_kbps'] * 1024)
            engine_status.set('Saved; the speed limit applies to running downloads now')
        ttk.Button(engine, text='Save download engine settings', command=save_engine).grid(
            row=11, column=0, sticky='w', pady=(6, 0))
        autostart = tk.BooleanVar(value=bool(current['autostart']))
        def save_autostart():
            set_setting(self.db, 'download_autostart', 1 if autostart.get() else 0)
        ttk.Checkbutton(engine, text='Start queued downloads automatically', variable=autostart,
                        command=save_autostart).grid(row=6, column=0, sticky='w', pady=(4, 0))
        ttk.Label(engine, text='Downloads at once').grid(row=7, column=0, sticky='w', pady=4)
        ttk.Spinbox(engine, from_=1, to=8, textvariable=engine_vars['parallel'], width=12).grid(
            row=7, column=1, sticky='w', pady=4)
        ttk.Checkbutton(engine, text='Watch the clipboard for download links (magnets, .torrent, file hosts, '
                        'shortlinks, direct files)', variable=self.clipboard_capture,
                        command=lambda: set_setting(self.db, 'clipboard_capture',
                                                    '1' if self.clipboard_capture.get() else '0')).grid(
            row=5, column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Label(engine, textvariable=engine_status, wraplength=700, justify='left').grid(
            row=12, column=0, columnspan=2, sticky='w', pady=(4, 0))
        ttk.Label(parent, text='Jackett and optional external download managers',
                  font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        ttk.Label(parent, wraplength=900, justify='left', text=(
            'Direct files use the built-in engine above. Jackett searches the indexers you configured in your own '
            'Jackett instance (choose "Jackett (torrents)" on the Search tab). An external manager is optional: '
            'torrent and magnet results can go to Gopeed, and any file can go to Xtreme Download Manager or a '
            'custom command. Hand-offs use the same rights-evidence approval as built-in downloads; Jackett results '
            'need jackett.approved_for_download = true.'
        )).pack(anchor='w', pady=(4, 10))
        settings = self.external_dl_settings
        variables = {key: tk.StringVar(value=str(settings.get(key) or ''))
                     for key in self.EXTERNAL_DOWNLOAD_KEYS + self.EXTERNAL_DOWNLOAD_SECRETS}
        labels = {value: key for key, value in DOWNLOADER_LABELS.items()}
        downloader_label = tk.StringVar(value=DOWNLOADER_LABELS.get(settings.get('downloader'), 'Gopeed'))
        sections = (
            ('Jackett', (('Jackett address', 'jackett_url', False), ('API key', 'jackett_api_key', True),
                         ('Indexer ("all" or one indexer ID)', 'jackett_indexer', False))),
            ('Download manager', (('Gopeed API address', 'gopeed_url', False),
                                  ('Gopeed API token', 'gopeed_token', True),
                                  ('xdm-app.exe path', 'xdm_path', False),
                                  ('Custom command ({url}, {dir}, {name})', 'command', False),
                                  ('Download folder (blank: library folder)', 'directory', False))),
        )
        status = tk.StringVar()
        for title, fields in sections:
            frame = ttk.LabelFrame(parent, text=title, padding=8)
            frame.pack(fill='x', pady=(0, 8))
            row = 0
            if title == 'Download manager':
                ttk.Label(frame, text='Send downloads to').grid(row=0, column=0, sticky='w', pady=5)
                ttk.Combobox(frame, textvariable=downloader_label, state='readonly', width=30,
                             values=list(DOWNLOADER_LABELS.values())).grid(row=0, column=1, sticky='w', pady=5)
                row = 1
            for offset, (label, key, secret) in enumerate(fields, row):
                ttk.Label(frame, text=label).grid(row=offset, column=0, sticky='w', padx=(0, 8), pady=5)
                ttk.Entry(frame, textvariable=variables[key], width=60,
                          show='*' if secret else '').grid(row=offset, column=1, sticky='ew', pady=5)
            frame.columnconfigure(1, weight=1)

        def collect():
            values = {key: variable.get().strip() for key, variable in variables.items()}
            values['downloader'] = labels.get(downloader_label.get(), 'gopeed')
            return values

        def save():
            values = collect()
            try:
                for key in self.EXTERNAL_DOWNLOAD_SECRETS:
                    set_local_secret(self.db, key, values[key])
            except Exception as exc:
                status.set(f'Could not securely save credentials: {exc}')
                return
            for key in self.EXTERNAL_DOWNLOAD_KEYS:
                set_setting(self.db, f'external_dl_{key}', values[key])
            self.external_dl_settings = values
            status.set('Saved')

        def test_jackett():
            from .jackett import test_connection
            values = collect()
            self._worker('Testing Jackett…', lambda: test_connection(values['jackett_url'], values['jackett_api_key']),
                         lambda modes: status.set('Jackett connected; search modes: ' + (', '.join(modes) or 'none reported')),
                         lambda error: status.set(f'Jackett: {error}'))

        def test_gopeed():
            from .external_downloaders import gopeed_info
            values = collect()
            self._worker('Testing Gopeed…', lambda: gopeed_info(values['gopeed_url'], values['gopeed_token']),
                         lambda info: status.set('Gopeed connected' + (
                             f" (version {info.get('version')})" if isinstance(info, dict) and info.get('version') else '')),
                         lambda error: status.set(f'Gopeed: {error}'))

        def choose_xdm():
            chosen = filedialog.askopenfilename(title='Choose xdm-app.exe',
                                                filetypes=[('Programs', '*.exe'), ('All files', '*.*')])
            if chosen:
                variables['xdm_path'].set(chosen)

        buttons = ttk.Frame(parent)
        buttons.pack(anchor='w', pady=(4, 4))
        ttk.Button(buttons, text='Save', command=save).pack(side='left')
        ttk.Button(buttons, text='Test Jackett', command=test_jackett).pack(side='left', padx=6)
        ttk.Button(buttons, text='Test Gopeed', command=test_gopeed).pack(side='left')
        ttk.Button(buttons, text='Find xdm-app.exe…', command=choose_xdm).pack(side='left', padx=6)
        ttk.Label(parent, textvariable=status, wraplength=900, justify='left').pack(anchor='w', pady=(4, 0))

    def _handoff_settings(self):
        settings = dict(self.external_dl_settings)
        if not settings.get('directory'):
            settings['directory'] = self.library_folder.get().strip()
        return settings

    def _search_connector(self, spec, query):
        """Search one source connector directly (Search one source), with paging."""
        from .connectors import search_one
        category = self.search_category.get()
        if category == 'Auto-detect' or category not in spec.categories:
            category = 'Videos' if 'Videos' in spec.categories else sorted(spec.categories)[0]
        if spec.file_capable:
            approval = source_download_rights(load_rights_evidence(self.rights_evidence_path), spec.key)
            if not approval:
                messagebox.showinfo('Media Intake', (
                    f'{spec.label} is NO in the local rights-evidence.json file, so it is not searched or '
                    'downloaded. Use Settings → Sources → Open rights file and set its approved_for_download to '
                    'true if you are entitled to download from it.'))
                return
        options = connector_options(self, {})
        configs = options['source_configs']
        rights_path = str(self.rights_evidence_path)

        def action(page=1):
            found = search_one(spec.key, query, category, page, configs)
            for row in found.rows:
                row.update(connector_row=True, category=category)
                if spec.file_capable:
                    row['rights_evidence_path'] = rights_path
            return found

        def shown(found, append=False):
            rows = found.rows
            for row in rows:
                row['format_label'] = row.get('quality')
            self.show_results((self.results if append else []) + rows)
            self._more_search = {'single': spec.key, 'query': query, 'category': category, 'action': action}
            self._set_more_pages({spec.key: found.page + 1} if found.has_more and spec.pages else {})
            note = f' {found.notice}' if found.notice else ''
            self._done(f'{len(self.results)} result(s) from {spec.label} ({category}).{note}')
        self._more_search_shown = shown
        self._worker(f'Searching {spec.label}…', action, shown,
                     lambda error: (self._done(str(error)), messagebox.showerror('Media Intake', str(error))))

    def _set_more_pages(self, pages):
        self._more_pages = dict(pages or {})
        button = getattr(self, 'load_more_button', None)
        if button is not None:
            button.configure(state='normal' if self._more_pages else 'disabled')

    def load_more_results(self):
        state = getattr(self, '_more_search', None)
        pages = dict(getattr(self, '_more_pages', {}) or {})
        if not state or not pages:
            return
        if state.get('single'):
            page = pages[state['single']]
            self._worker('Loading more results…', lambda: state['action'](page),
                         lambda found: self._more_search_shown(found, append=True))
            return
        from .discovery import search_more_connector_results
        existing = list(self.results)

        def loaded(response):
            self.show_results(response['results'])
            self._set_more_pages(response.get('connector_pages') or {})
            errors = response.get('errors') or []
            self._done(f"Loaded {response.get('added', 0)} more release(s)"
                       + (f'; {errors[0]}' if errors else ''))
        self._worker('Loading more results…', lambda: search_more_connector_results(
            state['query'], state['category'], state['options'], pages, existing, state.get('priority') or ()),
            loaded)

    def _search_jackett(self, query):
        from .jackett import search_jackett
        settings = dict(self.external_dl_settings)
        category = self.search_category.get()
        category = None if category == 'Auto-detect' else category
        self._worker('Searching Jackett…', lambda: search_jackett(
            query, settings.get('jackett_url'), settings.get('jackett_api_key'), category=category,
            indexer=settings.get('jackett_indexer') or 'all'), self.show_results)

    def send_selected_results_to_downloader(self):
        selected = self.results_view.selection()
        if not selected:
            return
        items = [dict(self.results[int(index)]) for index in selected]
        sendable = [item for item in items if item.get('external_download') or (
            item.get('url') and not item.get('discovery_only') and item.get('source_role') != 'metadata'
            and urllib.parse.urlsplit(str(item.get('url'))).scheme in ('http', 'https'))]
        if not sendable:
            messagebox.showinfo('Send to download manager',
                                'Select Jackett results or direct-file results. Catalog and discovery results have no file link.')
            return
        self._send_to_downloader(sendable)

    def send_queue_item_to_downloader(self):
        selected = self.queue_view.selection()
        if not selected:
            return
        item_id = int(selected[0])
        row = next((row for row in self.queue_rows if int(row['id']) == item_id), None)
        try:
            metadata = json.loads((row or {}).get('metadata') or '{}')
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get('watch_search') or not metadata.get('url'):
            messagebox.showinfo('Send to download manager', 'This queue entry has no file link yet.')
            return
        self._send_to_downloader([metadata], queue_id=item_id)

    def _send_to_downloader(self, items, queue_id=None):
        from .external_downloaders import hand_off
        settings, rights_path, data_root = self._handoff_settings(), self.rights_evidence_path, self.data_root
        def action():
            outcomes = []
            for item in items:
                try:
                    outcomes.append((item, hand_off(item, settings, rights_path=rights_path), None))
                except Exception as exc:
                    outcomes.append((item, None, str(exc)))
            if queue_id is not None and outcomes[0][1]:
                db = connect(data_root)
                try:
                    db.execute("""UPDATE items SET state='handed_off', error=NULL, progress_message=?,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?""", (outcomes[0][1], queue_id))
                    db.commit()
                finally:
                    db.close()
            return outcomes
        def finished(outcomes):
            sent = [status for _item, status, error in outcomes if status]
            failed = [f"{item.get('title') or 'Untitled'}: {error}" for item, _status, error in outcomes if error]
            if queue_id is not None:
                self.refresh_queue()
            self._done(sent[0] if len(sent) == 1 else f'{len(sent)} sent to download manager')
            if failed:
                messagebox.showwarning('Send to download manager', '\n'.join(failed))
        self._worker('Sending to download manager…', action, finished)

    def resolve_shortlink_url(self):
        """Resolve the URL field: headless first, then the userscript browser helper."""
        from .shortlinks import ShortlinkNeedsBrowser, is_shortlink, resolve_shortlink
        url = self.url.get().strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            messagebox.showinfo('Resolve shortlink', 'Paste a shortlink into the URL field first.')
            return
        if not is_shortlink(url):
            if not messagebox.askyesno('Resolve shortlink', 'This address is not a known shortener. '
                                       'Open it in the shortlink helper anyway?'):
                return
            self._open_shortlink_browser(url)
            return
        def action():
            try:
                return resolve_shortlink(url), None
            except ShortlinkNeedsBrowser as exc:
                return None, str(exc)
        def finished(result):
            target, reason = result
            if target:
                self._apply_resolved_link(target)
            else:
                self._done(f'{reason}; opening the shortlink helper')
                self._open_shortlink_browser(url)
        self._worker('Resolving shortlink…', action, finished)

    def _apply_resolved_link(self, target):
        """Turn a resolved destination into a direct file in the Add a direct file fields."""
        from .core import EXTENSIONS
        from .file_hosts import resolve_hosted_file
        def action():
            try:
                hosted = resolve_hosted_file(target, EXTENSIONS) if target.startswith('https://') else None
            except Exception as exc:
                return target, None, str(exc)
            return target, (hosted or [None])[0], None
        def finished(result):
            original, file, warning = result
            self.url.set(file.url if file else original)
            if file:
                self.format.set(file.format)
                if not self.title.get().strip() and file.filename:
                    self.title.set(Path(file.filename).stem)
            else:
                name = Path(urllib.parse.unquote(urllib.parse.urlsplit(original).path))
                if name.suffix.casefold() in EXTENSIONS:
                    self.format.set(name.suffix.casefold())
                    if not self.title.get().strip():
                        self.title.set(name.stem)
            message = f'Shortlink resolved to {urllib.parse.urlsplit(self.url.get()).hostname}'
            if warning:
                message += f' ({warning})'
            self._done(message + '; review the fields, then Add to queue or Send to download manager')
        self._worker('Reading resolved link…', action, finished)

    def _schedule_podcast_checks(self):
        """Check due podcasts in the background every few minutes; record finished episodes."""
        try:
            self.root.after(300_000, self._schedule_podcast_checks)
        except tk.TclError:
            return
        if self._podcast_check_running:
            return
        self._podcast_check_running = True
        data_root = self.data_root

        def run():
            from . import podcasts
            outcome = {}
            try:
                db = connect(data_root)
                try:
                    outcome = podcasts.check_due(db)
                    podcasts.sync_downloads(db)
                finally:
                    db.close()
            except Exception:
                logging.getLogger('media_intake').exception('Podcast check failed')
            def done():
                self._podcast_check_running = False
                new = sum(value for value in outcome.values() if isinstance(value, int))
                if hasattr(self, 'podcasts_panel'):
                    self.podcasts_panel.refresh()
                if new:
                    self.refresh_queue()
                    self._done(f'{new} new podcast episode(s) queued')
            try:
                self.root.after(0, done)
            except (RuntimeError, tk.TclError):
                pass
        threading.Thread(target=run, daemon=True).start()

    def _autostart_downloads(self):
        """Run queued downloads automatically, up to the parallel limit (Settings → Downloads)."""
        from . import transfer
        from .core import download_settings, in_download_window
        settings = download_settings(self.db)
        if not settings['autostart'] or self._download_batch_active or not in_download_window(settings):
            return
        running = {int(row['id']) for row in self.queue_rows
                   if transfer.is_active(('item', int(row['id'])))} | self._autostart_ids
        slots = settings['parallel'] - len(running)
        if slots <= 0:
            return
        destination = self.library_folder.get().strip() or None
        for row in list_items(self.db, state='queued'):
            if slots <= 0:
                break
            item_id = int(row['id'])
            try:
                metadata = json.loads(row.get('metadata') or '{}')
            except (TypeError, ValueError):
                metadata = {}
            if item_id in running or metadata.get('watch_search'):
                continue
            slots -= 1
            self._autostart_ids.add(item_id)
            def run(item_id=item_id):
                try:
                    self._download_one(self.data_root, destination, item_id)
                finally:
                    try:
                        self.root.after(0, lambda: self._autostart_finished(item_id))
                    except (RuntimeError, tk.TclError):
                        pass
            threading.Thread(target=run, daemon=True).start()

    def _autostart_finished(self, item_id):
        self._autostart_ids.discard(item_id)
        self.refresh_queue()
        row = self.db.execute('SELECT state FROM items WHERE id=?', (item_id,)).fetchone()
        helper = self._shortlink_browser_process
        if row and row['state'] == 'needs_shortlink' and not (helper and helper.poll() is None):
            self._open_shortlink_for_item(item_id)

    def _poll_clipboard(self):
        try:
            if self.clipboard_capture.get():
                try:
                    text = self.root.clipboard_get()
                except tk.TclError:
                    text = ''
                if self._clipboard_last is None:
                    self._clipboard_last = text  # ignore whatever was copied before the app started
                elif text != self._clipboard_last:
                    self._clipboard_last = text
                    self._offer_clipboard_links(text)
            else:
                self._clipboard_last = None
            self.root.after(1500, self._poll_clipboard)
        except tk.TclError:
            return

    def _offer_clipboard_links(self, text):
        from .link_capture import KIND_LABELS, classify_links
        links = classify_links(text)
        if links:
            queued = {row[0] for row in self.db.execute(
                f"SELECT url FROM items WHERE url IN ({','.join('?' * len(links))})", [l['url'] for l in links])}
            links = [link for link in links if link['url'] not in queued]
        if not links:
            return
        if self._clipboard_offer is not None and self._clipboard_offer.winfo_exists():
            self._clipboard_offer.destroy()
        offer = tk.Toplevel(self.root)
        self._clipboard_offer = offer
        offer.title('Download links copied')
        offer.transient(self.root)
        offer.resizable(False, False)
        ttk.Label(offer, text=f'Copied {len(links)} download link(s)', font=('TkDefaultFont', 10, 'bold')).pack(
            anchor='w', padx=12, pady=(10, 4))
        for link in links[:6]:
            shown = link['url'] if len(link['url']) <= 70 else link['url'][:67] + '…'
            ttk.Label(offer, text=f"{KIND_LABELS[link['kind']]}: {shown}").pack(anchor='w', padx=12)
        if len(links) > 6:
            ttk.Label(offer, text=f'…and {len(links) - 6} more').pack(anchor='w', padx=12)
        def queue(start):
            offer.destroy()
            self._queue_captured_links(links, start)
        bar = ttk.Frame(offer)
        bar.pack(fill='x', padx=12, pady=10)
        ttk.Button(bar, text='Queue and download', command=lambda: queue(True)).pack(side='left')
        ttk.Button(bar, text='Queue', command=lambda: queue(False)).pack(side='left', padx=6)
        ttk.Button(bar, text='Ignore', command=offer.destroy).pack(side='left')
        offer.after(60_000, lambda: offer.winfo_exists() and offer.destroy())

    def _queue_captured_links(self, links, start=False):
        from .core import EXTENSIONS
        from .link_capture import queue_item
        guess = self.format.get().strip().casefold()
        guess = guess if guess in EXTENSIONS else '.zip'
        def action():
            db = connect(self.data_root)
            try:
                return [enqueue(db, queue_item(link, guess))['id'] for link in links]
            finally:
                db.close()
        def queued(ids):
            self.refresh_queue()
            self._done(f'Queued {len(ids)} copied link(s)')
            if start:
                self.download_queued([int(item_id) for item_id in ids])
        self._worker('Queueing copied links…', action, queued)

    def _open_shortlink_for_item(self, item_id):
        row = self.db.execute('SELECT metadata FROM items WHERE id=?', (item_id,)).fetchone()
        if not row:
            return
        url = json.loads(row['metadata']).get('url')
        self._shortlink_target_item = item_id
        self._open_shortlink_browser(url)

    def _select_queue_item(self, item_id):
        if self.queue_view.exists(str(item_id)):
            self.queue_view.selection_set(str(item_id))
            return
        for row in self.queue_view.get_children():
            if str(self.queue_view.set(row, 'id')) == str(item_id):
                self.queue_view.selection_set(row)
                return

    def _selected_queue_id(self):
        selected = self.queue_view.selection()
        if not selected:
            return None
        value = self.queue_view.set(selected[0], 'id') or selected[0]
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def pause_selected_download(self):
        from . import transfer
        item_id = self._selected_queue_id()
        if item_id is None:
            return
        if transfer.request_pause(('item', item_id)):
            self._done('Pausing… progress is kept')
        else:
            self._done('That download is not running')

    @staticmethod
    def _select_row_under_pointer(tree, event):
        row = tree.identify_row(event.y)
        if row:
            tree.selection_set(row)
            tree.focus(row)
        return row

    def _library_context_menu(self, event):
        from .kindle import get_kindle_email_targets
        if not self._select_row_under_pointer(self.library_view, event):
            return
        menu = tk.Menu(self.root, tearoff=False)
        kindles = tk.Menu(menu, tearoff=False)
        targets = get_kindle_email_targets(self.db)
        for target in targets:
            label = f"{target['name']} ({target['email']})" if target.get('name') else target['email']
            kindles.add_command(label=label, command=lambda chosen=target: self.email_library_book([chosen]))
        if len(targets) > 1:
            kindles.add_separator()
            kindles.add_command(label='All Kindles', command=lambda: self.email_library_book(targets))
        if targets:
            menu.add_cascade(label='Email to Kindle', menu=kindles)
        else:
            menu.add_command(label='Email to Kindle… (add an address in Settings > Kindle)',
                             command=lambda: messagebox.showinfo(
                                 'Email to Kindle', 'Add your Kindle email address and SMTP settings in Settings > Kindle.'))
        menu.add_command(label='Send to connected Kindle / configured Kindles', command=self.send_selected_to_kindle)
        menu.add_separator()
        menu.add_command(label='Open file', command=self.open_selected_library_file)
        menu.add_command(label='Open containing folder', command=self.open_selected_library_folder)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def email_library_book(self, targets):
        from .kindle import email_to_kindle
        selected = self.library_view.selection()
        if not selected:
            return
        item_id, data_root = int(selected[0]), self.data_root
        names = ', '.join(target.get('name') or target['email'] for target in targets)
        def action():
            db = connect(data_root)
            try:
                return email_to_kindle(db, item_id, data_root, targets)
            finally:
                db.close()
        def completed(row):
            self.refresh_queue()
            delivery = self._row_kindle_delivery(row)
            if delivery.get('status') == 'sent_unconfirmed':
                self._done(f"Emailed {self._row_title(row)} to {names}; it appears in the Kindle library shortly")
            else:
                self._done(f"Kindle email failed: {delivery.get('error') or 'see Library status'}")
        self._worker(f'Emailing book to {names}…', action, completed)

    def _queue_context_menu(self, event):
        if not self._select_row_under_pointer(self.queue_view, event):
            return
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label='Search all IRC servers for this book', command=self.search_irc_for_queue_item)
        menu.add_command(label='Open IRC channel to search myself…', command=self.open_irc_console_for_queue_item)
        menu.add_separator()
        menu.add_command(label='Download / resume', command=self.download_selected)
        menu.add_command(label='Pause', command=self.pause_selected_download)
        menu.add_command(label='Cancel', command=self.cancel_selected_download)
        menu.add_command(label='Send to download manager', command=self.send_queue_item_to_downloader)
        menu.add_command(label='Stop seeding', command=self.stop_selected_seeding)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _queue_item_query(self):
        item_id = self._selected_queue_id()
        row = next((row for row in self.queue_rows if int(row['id']) == item_id), None) if item_id else None
        if not row:
            return None, None, None
        try:
            metadata = json.loads(row.get('metadata') or '{}')
        except (TypeError, ValueError):
            metadata = {}
        author = str(metadata.get('author') or '')
        query = ' '.join(part for part in (str(metadata.get('title') or ''),
                                           '' if author.casefold() in ('', 'unknown') else author) if part)
        return item_id, query.strip()[:150], metadata.get('watch_id')

    def _irc_options(self):
        return {'irc_sources': [source for source in self.irc_sources if source.get('enabled', True)]}

    def search_irc_for_queue_item(self):
        from .irc import search_all_sources
        item_id, query, watch_id = self._queue_item_query()
        if not query:
            return
        options = self._irc_options()
        if not options['irc_sources']:
            messagebox.showinfo('IRC search', 'Add an IRC server in Settings > IRC sources first.')
            return
        self._worker(f'Searching {len(options["irc_sources"])} IRC server(s) for {query}…',
                     lambda: search_all_sources(query, options),
                     lambda outcome: self.show_irc_results(outcome[0], f'IRC results for {query}',
                                                          watch_id=watch_id, errors=outcome[1]))

    def show_irc_results(self, entries, title, watch_id=None, errors=()):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry('980x520')
        dialog.transient(self.root)
        note = f'{len(entries)} result(s). Select one or more lines to add to the downloads.'
        if errors:
            note += ' Unreachable: ' + '; '.join(str(error)[:120] for error in errors)
        ttk.Label(dialog, text=note, wraplength=940, justify='left').pack(anchor='w', padx=12, pady=(12, 4))
        filter_text = tk.StringVar()
        filter_row = ttk.Frame(dialog)
        filter_row.pack(fill='x', padx=12)
        ttk.Label(filter_row, text='Filter').pack(side='left')
        ttk.Entry(filter_row, textvariable=filter_text, width=40).pack(side='left', padx=6)
        columns = ('author', 'format', 'size', 'bot', 'source')
        view = ttk.Treeview(dialog, columns=columns, show='tree headings', selectmode='extended')
        view.heading('#0', text='Title')
        view.column('#0', width=330)
        for column, label, width in (('author', 'Author', 180), ('format', 'Format', 60), ('size', 'Size', 80),
                                     ('bot', 'Bot', 110), ('source', 'Server', 170)):
            view.heading(column, text=label)
            view.column(column, width=width, anchor='w')
        view.pack(fill='both', expand=True, padx=12, pady=6)
        def render(*_):
            needle = filter_text.get().casefold().strip()
            view.delete(*view.get_children())
            for index, entry in enumerate(entries):
                line = entry.get('filename') or entry.get('title') or ''
                if needle and needle not in line.casefold() and needle not in str(entry.get('bot', '')).casefold():
                    continue
                view.insert('', 'end', iid=str(index), text=entry.get('title') or line, values=(
                    entry.get('author') or '', entry.get('format') or '', entry.get('size_text') or '',
                    entry.get('bot') or (entry.get('irc') or {}).get('bot') or '',
                    entry.get('irc_source_name') or (entry.get('irc') or {}).get('server') or ''))
        filter_text.trace_add('write', render)
        render()
        def add(start):
            chosen = [entries[int(index)] for index in view.selection()]
            if not chosen:
                return
            data_root = self.data_root
            def action():
                from .irc import enqueue_irc
                db = connect(data_root)
                try:
                    return [enqueue_irc(db, dict(entry['irc']), watch_id=watch_id)['id'] for entry in chosen]
                finally:
                    db.close()
            def added(ids):
                self.refresh_queue()
                self._done(f'Added {len(ids)} IRC file(s) to the downloads')
                if start:
                    self.download_queued([int(item_id) for item_id in ids])
            self._worker('Adding IRC results to the downloads…', action, added)
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=12)
        ttk.Button(bar, text='Add selected and download', command=lambda: add(True)).pack(side='left')
        ttk.Button(bar, text='Add selected to the queue', command=lambda: add(False)).pack(side='left', padx=6)
        ttk.Button(bar, text='Close', command=dialog.destroy).pack(side='right')
        view.bind('<Double-1>', lambda _event: add(True))
        return dialog

    def open_irc_console_for_queue_item(self):
        item_id, query, watch_id = self._queue_item_query()
        self.open_irc_console(initial_command=f'@search {query}' if query else '', watch_id=watch_id)

    def open_irc_console(self, initial_command='', watch_id=None):
        from .irc import irc_source_priority_label
        from .irc_session import IrcSession
        sources = [source for source in self.irc_sources if source.get('enabled', True)]
        if not sources:
            messagebox.showinfo('IRC', 'Add an IRC server in Settings > IRC sources first.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('IRC channel')
        dialog.geometry('860x560')
        labels = [irc_source_priority_label(source.get('name'), source) for source in sources]
        chosen = tk.StringVar(value=labels[0])
        top = ttk.Frame(dialog)
        top.pack(fill='x', padx=12, pady=(12, 6))
        ttk.Label(top, text='Server / channel').pack(side='left')
        ttk.Combobox(top, textvariable=chosen, values=labels, state='readonly', width=55).pack(side='left', padx=6)
        status = tk.StringVar(value='Not connected')
        log = tk.Text(dialog, wrap='word', state='disabled', height=20)
        log.pack(fill='both', expand=True, padx=12)
        ttk.Label(dialog, textvariable=status).pack(anchor='w', padx=12, pady=(4, 0))
        entry_row = ttk.Frame(dialog)
        entry_row.pack(fill='x', padx=12, pady=8)
        command = tk.StringVar(value=initial_command)
        command_entry = ttk.Entry(entry_row, textvariable=command)
        command_entry.pack(side='left', fill='x', expand=True)
        state = {'session': None}
        library = Path(self.library_folder.get().strip() or self.data_root / 'downloads')

        def write(text):
            log.configure(state='normal')
            log.insert('end', text + '\n')
            log.see('end')
            log.configure(state='disabled')

        def handle(kind, payload):
            if not dialog.winfo_exists():
                return
            if kind == 'line':
                write(payload)
            elif kind in ('status', 'error'):
                status.set(payload)
                write(('! ' if kind == 'error' else '* ') + payload)
            elif kind == 'joined':
                status.set(f"In {payload['channel']} as {payload['nick']}. Type a command (for example @search title author) and press Enter.")
                write(f"* Joined {payload['channel']} as {payload['nick']}")
            elif kind == 'results':
                write(f"* {len(payload['entries'])} result(s) in {payload['filename']} from {payload['sender']}")
                self.show_irc_results(payload['entries'], f"{payload['filename']} ({chosen.get()})", watch_id=watch_id)
            elif kind == 'offer':
                size = payload['size']
                if messagebox.askyesno('IRC file offer', f"{payload['sender']} is sending {payload['filename']} "
                                       f"({size / 1048576:.1f} MB). Save it to the library folder?", parent=dialog):
                    state['session'].accept_offer(payload['offer_id'], library)
                    write(f"* Receiving {payload['filename']}…")
                else:
                    state['session'].decline_offer(payload['offer_id'])
            elif kind == 'progress':
                _offer_id, done, total = payload
                status.set(f'Receiving file: {done / 1048576:.1f} of {total / 1048576:.1f} MB')
            elif kind == 'saved':
                write(f"* Saved {payload['path']}")
                status.set('File saved to the library folder')
                self._record_console_download(payload)
            elif kind == 'closed':
                status.set('Disconnected')
                connect_button.configure(text='Connect')

        def connect_session():
            if state['session'] is not None:
                state['session'].close()
                state['session'] = None
                return
            source = dict(sources[labels.index(chosen.get())])
            config = {key: value for key, value in source.items() if key not in ('id', 'name', 'categories', 'enabled')}
            session = IrcSession(config, lambda kind, payload: self.root.after(0, lambda: handle(kind, payload)))
            state['session'] = session
            connect_button.configure(text='Disconnect')
            session.start()

        def send(*_):
            text = command.get().strip()
            if text and state['session'] is not None:
                state['session'].send(text)
                command.set('')

        connect_button = ttk.Button(top, text='Connect', command=connect_session)
        connect_button.pack(side='left')
        ttk.Button(entry_row, text='Send', command=send).pack(side='left', padx=6)
        command_entry.bind('<Return>', send)
        def on_close():
            if state['session'] is not None:
                state['session'].close()
            dialog.destroy()
        dialog.protocol('WM_DELETE_WINDOW', on_close)
        connect_session()
        return dialog

    def _record_console_download(self, payload):
        """Add a file received in the IRC console to the queue/library as downloaded."""
        from .core import EXTENSIONS
        path, offer = Path(payload['path']), payload['offer']
        ext = path.suffix.casefold()
        if ext not in EXTENSIONS:
            return
        name = path.stem
        author, sep, title = name.partition(' - ')
        if not sep:
            author, title = 'Unknown', name
        url = 'irc+xdcc://console/' + urllib.parse.quote(f"{offer['sender']}/{path.name}")
        try:
            row = enqueue(self.db, {'title': title, 'author': author, 'url': url, 'format': ext,
                                    'source': f"IRC {offer['sender']}", 'source_key': 'irc'})
            self.db.execute("UPDATE items SET state='downloaded', file_path=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (str(path.resolve()), row['id']))
            self.db.commit()
        except Exception:
            self.logger.exception('Could not record the IRC console download')
        self.refresh_queue()

    def open_source_health(self):
        from .discovery import SOURCE_KEY_LABELS
        from .source_health import clear, summaries
        from .transfer import format_bytes
        existing = getattr(self, '_source_health_dialog', None)
        if existing and existing.winfo_exists():
            existing.lift()
            return
        dialog = tk.Toplevel(self.root)
        self._source_health_dialog = dialog
        dialog.title('Download source health')
        dialog.geometry('980x420')
        dialog.transient(self.root)
        ttk.Label(dialog, wraplength=940, justify='left', text=(
            'Recent downloads and searches for each source. "Failing" means the last three attempts failed; '
            'automatic downloads then try equally good matches from other sources first.')).pack(
            anchor='w', padx=12, pady=(12, 6))
        columns = ('status', 'recent', 'downloads', 'searches', 'speed', 'last_ok', 'last_error')
        view = ttk.Treeview(dialog, columns=columns, show='tree headings', selectmode='browse')
        view.heading('#0', text='Source')
        view.column('#0', width=170)
        for column, label, width in (('status', 'Status', 75), ('recent', 'Recent OK', 70),
                                     ('downloads', 'Downloads OK/failed', 120), ('searches', 'Searches OK/failed', 115),
                                     ('speed', 'Typical speed', 90), ('last_ok', 'Last worked (UTC)', 130),
                                     ('last_error', 'Latest error', 260)):
            view.heading(column, text=label)
            view.column(column, width=width, anchor='w')
        view.tag_configure('failing', foreground='#b91c1c')
        view.tag_configure('degraded', foreground='#b45309')
        view.pack(fill='both', expand=True, padx=12)
        def refresh():
            view.delete(*view.get_children())
            for item in summaries(self.db):
                key = item['source_key']
                error = item['last_error'] or ''
                if item['last_error_at'] and error:
                    error = f"{item['last_error_at']}: {error}"
                view.insert('', 'end', iid=key, text=SOURCE_KEY_LABELS.get(key, key), tags=(item['status'],), values=(
                    item['status'], f"{item['recent_successes']}/{item['recent_attempts']}",
                    f"{item['downloads_ok']}/{item['downloads_failed']}",
                    f"{item['searches_ok']}/{item['searches_failed']}",
                    f"{format_bytes(item['median_speed'])}/s" if item['median_speed'] else '',
                    item['last_ok_at'] or 'never', error[:200]))
        def clear_selected():
            selected = view.selection()
            if selected and messagebox.askyesno('Source health', f'Clear the history for {selected[0]}?', parent=dialog):
                clear(self.db, selected[0])
                refresh()
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=12)
        ttk.Button(bar, text='Refresh', command=refresh).pack(side='left')
        ttk.Button(bar, text='Clear history for selected', command=clear_selected).pack(side='left', padx=6)
        ttk.Button(bar, text='Close', command=dialog.destroy).pack(side='right')
        refresh()

    def stop_selected_seeding(self):
        from .torrents import SEEDER
        item_id = self._selected_queue_id()
        if item_id is None:
            return
        if SEEDER.stop(item_id):
            self.refresh_queue()
            self._done('Seeding stopped; the downloaded files are kept')
        else:
            self._done('That item is not seeding')

    def pause_all_downloads(self):
        from . import transfer
        paused = sum(transfer.request_pause(('item', int(row['id'])))
                     for row in self.queue_rows if row.get('state') == 'downloading')
        self._done(f'Pausing {paused} download(s)' if paused else 'No downloads are running')

    def cancel_selected_download(self):
        from . import transfer
        item_id = self._selected_queue_id()
        if item_id is None:
            return
        if transfer.request_cancel(('item', item_id)):
            self._done('Cancelling…')
            return
        row = self.db.execute('SELECT state FROM items WHERE id=?', (item_id,)).fetchone()
        if row and row['state'] in ('paused', 'failed', 'queued', 'needs_shortlink'):
            folder = Path(self.library_folder.get().strip() or self.data_root / 'downloads')
            from .streams import discard_stream_partial
            from .torrents import discard_torrent_partial
            for candidate in {folder, self.data_root / 'downloads'}:
                transfer.discard_partial(candidate / f'.partial-{item_id}.part')
                discard_torrent_partial(candidate, item_id)
                discard_stream_partial(candidate / f'.partial-{item_id}.hls')
            self.db.execute("""UPDATE items SET state='cancelled', error=NULL, progress_bytes=NULL,
                progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (item_id,))
            self.db.commit()
            self.refresh_queue()
            self._done('Cancelled; partial data removed')

    def _recover_interrupted_downloads(self):
        """Transfers cut off by closing the app resume from their saved checkpoints."""
        from .core import recover_interrupted_downloads
        recover_interrupted_downloads(self.db)
        from . import transfer
        from .core import download_settings
        settings = download_settings(self.db)
        transfer.LIMITER.set_rate(settings['speed_limit_kbps'] * 1024)
        from . import torrents
        torrents.set_upload_limit(settings['upload_limit_kbps'] * 1024)
        if torrents.available():
            data_root = self.data_root
            try:
                torrents.resume_seeding(self.db, lambda: connect(data_root))
            except Exception:
                self.logger.exception('Could not resume torrent seeding')

    def _open_shortlink_browser(self, url):
        from .shortlink_browser import read_captures
        if self._shortlink_browser_process and self._shortlink_browser_process.poll() is None:
            self._done('Shortlink helper is already open')
            return
        captures = self.data_root / 'browser-captures'
        if self._shortlink_captures_seen is None:
            self._shortlink_captures_seen = set()
            read_captures(captures, self._shortlink_captures_seen)  # ignore earlier sessions
        profile = self.data_root / 'browser-profile'
        if getattr(sys, 'frozen', False):
            command = [sys.executable, '--shortlink-browser', url, str(profile), str(captures)]
        else:
            script = Path(__file__).resolve().parents[1] / 'run_gui.py'
            command = [sys.executable, str(script), '--shortlink-browser', url, str(profile), str(captures)]
        try:
            flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            self._shortlink_browser_process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
        except OSError:
            self.logger.exception('Could not launch shortlink helper')
            messagebox.showerror('Resolve shortlink', 'Could not start the shortlink helper.')
            return
        self._done('Shortlink helper open: complete any CAPTCHA; the final file link comes back here')
        self.root.after(1000, self._poll_shortlink_captures)

    def _poll_shortlink_captures(self):
        from .shortlink_browser import read_captures
        records = read_captures(self.data_root / 'browser-captures', self._shortlink_captures_seen)
        if records and self._shortlink_target_item is not None:
            from .core import resolve_shortlink_item
            item_id, self._shortlink_target_item = self._shortlink_target_item, None
            resolve_shortlink_item(self.db, item_id, records[-1]['final_url'])
            self.refresh_queue()
            self._select_queue_item(item_id)
            self.download_selected()
        elif records:
            self._apply_resolved_link(records[-1]['final_url'])
        process = self._shortlink_browser_process
        if process and process.poll() is None:
            self.root.after(1000, self._poll_shortlink_captures)

    def show_results(self, results):
        self.results = results
        for row in self.results_view.get_children(): self.results_view.delete(row)
        for index, item in enumerate(results):
            self.results_view.insert('', 'end', iid=str(index), values=(
                item.get('category') or self.search_category.get(), item.get('title', ''),
                self._result_creator_label(item), item.get('series', ''),
                self._series_order_label(item), format_summary(item),
                source_summary(item)))
        self._done(f'{len(results)} result(s)')

    @staticmethod
    def _result_creator_label(item):
        """Author, or for films, shows and games (which have none) the type and year.

        Same-titled works ("John Wick" the film, the franchise and the comic)
        otherwise look like duplicate rows.
        """
        author = str(item.get('author') or '')
        if author and author.casefold() != 'unknown':
            return author
        kind = str(item.get('video_subtype') or item.get('game_subtype') or item.get('entity_type') or
                   (item.get('comic_entity') and 'comic_' + item['comic_entity']) or '')
        label = {'movie': 'Film', 'tv_series': 'TV series', 'web_series': 'Web series', 'miniseries': 'Miniseries',
                 'franchise': 'Franchise', 'anime': 'Anime', 'base_game': 'Game', 'short_film': 'Short film',
                 'animated_film': 'Animated film', 'anime_ova': 'OVA', 'anime_oad': 'OAD',
                 'comic_series': 'Comic series', 'comic_collection': 'Collection', 'comic_issue': 'Issue'}.get(kind, '')
        # Comic runs share names across publishers and relaunches (Bone 1991, 1995,
        # 1997), so their start year and length tell them apart.
        year = (str(item.get('series_start_year') or '') if kind.startswith('comic_') else '') or \
            str(item.get('release_date') or '')[:4] or str(item.get('release_year') or '')
        issues = item.get('issue_count') if kind == 'comic_series' else None
        parts = (label, year, f'{issues} issues' if issues else '', str(item.get('publisher') or '') if
                 kind.startswith('comic_') else '')
        return ' · '.join(part for part in parts if part) or author

    @staticmethod
    def _series_order_label(item):
        position = item.get('series_index')
        if position is None:
            return item.get('series_position') or ''
        try:
            number = float(position)
            label = f'{number:g}'
        except (TypeError, ValueError):
            label = str(position)
        if 'publication date' in str(item.get('series_position_source') or '').casefold():
            label = '~' + label
        return label

    def choose_release(self):
        selected = self.results_view.selection()
        if not selected:
            return
        item = self.results[int(selected[0])]
        if not (item.get('download_variants') or item.get('connector_row')) or item.get('discovery_only'):
            messagebox.showinfo('Choose release', 'This result has no downloadable releases. Try another result, '
                                'or check Settings → Sources for sources that need approval or an account.')
            return
        ReleasePicker(self, item)

    def queue_result(self):
        selected = self.results_view.selection()
        if not selected: return
        item = self.results[int(selected[0])]
        if wants_release_picker(item):
            ReleasePicker(self, item)
            return
        if item.get('connector_row') and not item.get('discovery_only'):
            queue_rows(self, [dict(self.results[int(index)]) for index in selected
                              if self.results[int(index)].get('connector_row')])
            return
        if item.get('external_download'):
            from .torrents import available
            if not available():
                self.send_selected_results_to_downloader()
                return
            items = [dict(self.results[int(index)]) for index in selected
                     if self.results[int(index)].get('external_download')]
            def queue_torrents():
                db = connect(self.data_root)
                try:
                    return [enqueue(db, dict(row, rights_evidence_path=str(self.rights_evidence_path)))
                            for row in items]
                finally:
                    db.close()
            self._worker('Queueing torrents…', queue_torrents,
                         lambda rows: (self.refresh_queue(), self._done(f'Queued {len(rows)} torrent(s)')))
            return
        category = self._search_result_category(item)
        if item.get('series_bundle'):
            self._add_search_item_to_watched(item, category)
            return
        if item.get('forum_thread'):
            messagebox.showinfo('Forum thread result', 'Forum results are thread references and cannot be added to the download queue.')
            return
        if item.get('fmhy'):
            messagebox.showinfo('FMHY directory result', 'This is a discovery link. Open it in a browser and add a resolved direct file URL.')
            return
        if (item.get('discovery_only') or item.get('source_role') == 'metadata' or
                item.get('format') == 'catalog' or item.get('metadata_variants')):
            self._add_search_item_to_watched(item, category)
            return
        def action():
            db = connect(self.data_root)
            try:
                if item.get('irc'):
                    config = dict(item['irc'])
                    for key in ('source_key', 'rights_evidence_path', 'rights_work_key'):
                        if item.get(key):
                            config[key] = item[key]
                    return enqueue_irc(db, config)
                return enqueue(db, item)
            finally: db.close()
        self._worker('Adding to queue…', action, lambda _result: self._done('Queued'))

    def _search_result_category(self, item):
        category = str(item.get('category') or '').strip()
        if category in CATEGORIES:
            return category
        selected = self.search_category.get()
        if selected in CATEGORIES:
            return selected
        kind = str(item.get('kind') or item.get('entity_type') or '').casefold()
        mapping = {
            'book': 'Books', 'series': 'Books', 'author': 'Books',
            'lightnovel': 'Light novels', 'light novel': 'Light novels',
            'webfiction': 'Web serials', 'web serial': 'Web serials',
            'comic': 'Comics', 'comic_issue': 'Comics', 'webcomic': 'Comics',
            'manga': 'Manga', 'webtoon': 'Manga', 'anime': 'Anime',
            'video': 'Videos', 'video_episode': 'Videos', 'game': 'Games',
            'software': 'Software', 'fanfiction': 'Fanfiction',
        }
        return mapping.get(kind, 'Books' if str(item.get('provider') or '').casefold()
                           in {'goodreads', 'open library', 'google books'} else 'Books')

    def activate_result(self):
        selected = self.results_view.selection()
        if selected and wants_release_picker(self.results[int(selected[0])]):
            ReleasePicker(self, self.results[int(selected[0])])
        elif selected and self.results[int(selected[0])].get('series_bundle'):
            self.add_selected_search_to_watched()
        elif selected and (self.results[int(selected[0])].get('forum_thread')
                         or self.results[int(selected[0])].get('discovery_only')
                         or self.results[int(selected[0])].get('fmhy')):
            self.open_selected_link()
        else:
            self.queue_result()

    def open_selected_link(self):
        selected = self.results_view.selection()
        if not selected:
            return
        item = self.results[int(selected[0])]
        url = item.get('url') or item.get('source')
        if not url or not (item.get('forum_thread') or item.get('discovery_only') or item.get('fmhy')):
            messagebox.showinfo('Open result', 'Select a catalog or discovery result first.')
            return
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            messagebox.showerror('Open result', 'The selected result does not contain a safe HTTPS page link.')
            return
        webbrowser.open(url)

    def open_selected_thread(self):
        selected = self.results_view.selection()
        if not selected:
            return
        item = self.results[int(selected[0])]
        if not item.get('forum_thread'):
            messagebox.showinfo('Open forum thread', 'Select a forum thread result first.')
            return
        webbrowser.open(item['url'])

    def add_direct(self):
        item = {'title': self.title.get().strip(), 'author': self.author.get().strip() or 'Unknown',
                'url': self.url.get().strip(), 'format': self.format.get().strip(), 'source': self.url.get().strip()}
        def action():
            db = connect(self.data_root)
            try: return enqueue(db, item)
            finally: db.close()
        self._worker('Adding to queue…', action, lambda _result: self._done('Queued'))

    def choose_library_folder(self):
        current = self.library_folder.get().strip() or str(self.data_root / 'downloads')
        chosen = filedialog.askdirectory(initialdir=current, title='Choose saved library folder')
        if not chosen:
            return
        self.library_folder.set(chosen)
        set_setting(self.db, 'library_folder', chosen)

    def choose_watch_catalogs(self):
        paths = filedialog.askopenfilenames(
            initialdir=str(self.data_root), title='Choose JSON catalogs',
            filetypes=[('JSON catalogs', '*.json'), ('All files', '*.*')])
        if paths:
            self.watch_catalogs.set(';'.join(paths))
            self._watch_sources()

    def choose_irc_configs(self):
        chosen = filedialog.askopenfilenames(
            initialdir=str(self.data_root), title='Import IRC sources from JSON configurations',
            filetypes=[('JSON configuration', '*.json'), ('All files', '*.*')])
        if chosen:
            self.watch_irc_configs.set(';'.join(chosen))
            self.irc_sources = self._load_irc_sources()
            self._saved_watch_sources = None
            self._watch_sources()
            self._render_irc_sources()
            self._render_download_priority()

    def _watch_sources(self):
        opds_feeds = [value.strip() for value in self.watch_opds_feeds.get().split(';') if value.strip()]
        catalogs = [value.strip() for value in self.watch_catalogs.get().split(';') if value.strip()]
        irc_configs = [value.strip() for value in self.watch_irc_configs.get().split(';') if value.strip()]
        comic_rss_configured = False
        webfiction_rss_configured = False
        taddy_configured = bool(self.taddy_user_id and self.taddy_api_key)
        for row in self.db.execute('SELECT kind,source_url,metadata FROM watched_items'):
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
        sources = {
            'rights_evidence_path': str(self.rights_evidence_path),
            'libgen': self.watch_libgen.get(),
            'libgen_base': self.watch_libgen_base.get().strip(),
            'annas': self.watch_annas.get(),
            'annas_base': self.watch_annas_base.get().strip(),
            'opds_feeds': opds_feeds,
            'catalogs': catalogs,
            'irc_configs': irc_configs,
            'irc_sources': list(self.irc_sources),
            'comic_mirror_base': self.watch_comic_mirror_base.get().strip(),
            'taddy_configured': taddy_configured,
            'comic_rss_configured': comic_rss_configured,
            'webfiction_rss_configured': webfiction_rss_configured,
        }
        if sources != self._saved_watch_sources:
            for key, value in sources.items():
                if isinstance(value, bool):
                    stored = '1' if value else '0'
                elif key == 'irc_sources':
                    stored = json.dumps(value, ensure_ascii=False)
                elif isinstance(value, list):
                    stored = ';'.join(value)
                else:
                    stored = value
                set_setting(self.db, 'watch_' + key, stored)
            self._saved_watch_sources = {key: list(value) if isinstance(value, list) else value
                                         for key, value in sources.items()}
        return sources

    def lookup_goodreads(self):
        query = self.watch_title.get().strip()
        if not query:
            query = simpledialog.askstring('Look up Goodreads', 'Search for a book, series, or author:') or ''
        query = query.strip()
        if not query:
            return
        self._goodreads_pending_search_query = query
        self._goodreads_pending_search_url = 'https://www.goodreads.com/search?' + urllib.parse.urlencode({'q': query})
        self._worker(f'Searching Goodreads for {query}…',
                     lambda: search_book_metadata(query), self.show_goodreads_matches,
                     on_error=lambda error: self.goodreads_search_unavailable(query, error))

    def _schedule_title_suggestions(self, event=None):
        if event and event.keysym in {'Return', 'Escape', 'Up', 'Down', 'Left', 'Right', 'Tab'}:
            return
        if self._title_suggestion_after:
            try:
                self.root.after_cancel(self._title_suggestion_after)
            except tk.TclError:
                pass
            self._title_suggestion_after = None
        query = self.watch_title.get().strip()
        if len(query) < 3:
            self._close_title_suggestion_popup()
            return
        self._title_suggestion_query = query
        self._title_suggestion_after = self.root.after(
            900, lambda current=query: self._load_title_suggestions(current))

    def _load_title_suggestions(self, query):
        self._title_suggestion_after = None
        if query != self.watch_title.get().strip():
            return

        def run():
            try:
                cache_key = query.casefold()
                results = self._book_metadata_cache.get(cache_key)
                if results is None:
                    # Predictive suggestions should stay responsive even when a
                    # third-party catalog is offline. The explicit metadata
                    # lookup button searches Goodreads and all configured book
                    # catalogs; typeahead uses Open Library's author/book index.
                    results = search_openlibrary_metadata(query, limit=24)
                    if len(self._book_metadata_cache) >= 64:
                        self._book_metadata_cache.pop(next(iter(self._book_metadata_cache)))
                    self._book_metadata_cache[cache_key] = results
                error = None
            except Exception as exc:
                results, error = [], exc
                self.logger.warning('Title suggestions failed for %r: %s', query, exc)
            self.root.after(0, lambda: self._show_title_suggestions(query, results, error))

        threading.Thread(target=run, daemon=True).start()

    def _show_title_suggestions(self, query, results, error=None):
        if query != self.watch_title.get().strip():
            return
        if error or not results:
            self._close_title_suggestion_popup()
            return
        self._title_suggestion_results = results
        if not self._title_suggestion_popup or not self._title_suggestion_popup.winfo_exists():
            popup = tk.Toplevel(self.root)
            popup.wm_overrideredirect(True)
            popup.attributes('-topmost', True)
            listing = tk.Listbox(popup, height=min(7, len(results)), width=76,
                                 activestyle='dotbox', exportselection=False)
            listing.pack(fill='both', expand=True)
            listing.bind('<ButtonRelease-1>', self._choose_title_suggestion)
            listing.bind('<Double-1>', self._choose_title_suggestion)
            listing.bind('<Return>', self._choose_title_suggestion)
            listing.bind('<Escape>', lambda _event: self._close_title_suggestion_popup())
            self._title_suggestion_popup = popup
            self._title_suggestion_list = listing
        listing = self._title_suggestion_list
        listing.delete(0, tk.END)
        for item in results:
            listing.insert(tk.END, f"{item.get('kind', 'book').title()}  ·  {item.get('title', '')}  ·  {item.get('provider', 'Open Library')}")
        listing.configure(height=min(7, len(results)))
        self.watch_title_entry.update_idletasks()
        x = self.watch_title_entry.winfo_rootx()
        y = self.watch_title_entry.winfo_rooty() + self.watch_title_entry.winfo_height()
        self._title_suggestion_popup.geometry(f'+{x}+{y}')
        self._title_suggestion_popup.deiconify()

    def _focus_title_suggestions(self, _event=None):
        listing = self._title_suggestion_list
        if listing and listing.winfo_exists() and self._title_suggestion_results:
            listing.focus_set()
            listing.selection_clear(0, tk.END)
            listing.selection_set(0)
            listing.activate(0)
            return 'break'

    def _choose_title_suggestion(self, _event=None):
        listing = self._title_suggestion_list
        if not listing or not listing.winfo_exists():
            return 'break'
        selected = listing.curselection()
        if selected and selected[0] < len(self._title_suggestion_results):
            item = self._title_suggestion_results[selected[0]]
            self._close_title_suggestion_popup()
            self._associate_book_metadata(item)
        return 'break'

    def _close_title_suggestion_popup(self):
        popup = self._title_suggestion_popup
        self._title_suggestion_popup = None
        self._title_suggestion_list = None
        self._title_suggestion_results = []
        if popup and popup.winfo_exists():
            popup.destroy()

    def goodreads_search_unavailable(self, query, error):
        self._done('Goodreads search blocked; save its results page to import')
        opened = webbrowser.open(self._goodreads_pending_search_url, new=1)
        message = (f'{error}\n\nThe search phrase “{query}” is ready. Save the opened Goodreads search page '
                   'as HTML, then click Import Goodreads HTML to load its book, series, or author matches.')
        if not opened:
            message += '\n\nThe Goodreads page could not be opened automatically.'
        messagebox.showwarning('Goodreads search unavailable', message)

    def _lookup_comic_catalog(self, provider):
        query = self.watch_title.get().strip()
        if not query:
            query = simpledialog.askstring(f'Look up {provider}', 'Search for a manga or comic:') or ''
        query = query.strip()
        if not query:
            return
        search = search_anilist if provider == 'AniList' else search_mangadex
        self._worker(f'Searching {provider} for {query}…',
                     lambda: search(query),
                     lambda results: self.show_comic_matches(provider, results))

    def lookup_anilist(self):
        self._lookup_comic_catalog('AniList')

    def lookup_mangadex(self):
        self._lookup_comic_catalog('MangaDex')

    def lookup_taddy(self):
        query = self.watch_title.get().strip()
        if not query:
            query = simpledialog.askstring('Look up Taddy', 'Search for a comic, webtoon, or manga:') or ''
        query = query.strip()
        if not query:
            return
        user_id = self.taddy_user_id or os.environ.get('TADDY_USER_ID', '').strip()
        api_key = self.taddy_api_key or os.environ.get('TADDY_API_KEY', '').strip()
        if not user_id:
            user_id = simpledialog.askstring('Taddy credentials', 'Taddy numeric user ID:') or ''
        if not user_id:
            return
        if not api_key:
            api_key = simpledialog.askstring('Taddy credentials', 'Taddy API key (used for this lookup only):', show='*') or ''
        if not api_key:
            return
        self.taddy_user_id = user_id
        self.taddy_api_key = api_key
        self._worker(f'Searching Taddy for {query}…',
                     lambda: search_taddy(query, user_id, api_key),
                     lambda results: self.show_comic_matches('Taddy', results))

    def _lookup_catalog(self, provider, searcher):
        query = self.watch_title.get().strip()
        if not query:
            query = simpledialog.askstring(f'Look up {provider}', f'Search {provider}:') or ''
        query = query.strip()
        if query:
            self._worker(f'Searching {provider} for {query}…', lambda: searcher(query),
                         lambda results: self.show_catalog_matches(provider, results))

    def lookup_novelarchive(self):
        self._lookup_catalog('NovelArchive', search_novelarchive)

    def lookup_lnori(self):
        self._lookup_catalog('LNORI', search_lnori)

    def lookup_ranobedb(self):
        self._lookup_catalog('RanobeDB', search_ranobedb)

    def lookup_mangaupdates(self):
        self._lookup_catalog('MangaUpdates', search_mangaupdates)

    def lookup_comicvine(self):
        query = self.watch_title.get().strip()
        if not query:
            query = simpledialog.askstring('Look up Comic Vine', 'Search a comic series:') or ''
        query = query.strip()
        if not query:
            return
        api_key = os.environ.get('COMICVINE_API_KEY', '').strip()
        if not api_key:
            api_key = simpledialog.askstring(
                'Comic Vine API key',
                'Enter your Comic Vine API key for this session. To keep scheduled tracking after restart, set COMICVINE_API_KEY in the app environment.',
                show='*') or ''
        if not api_key:
            return
        self.comicvine_api_key = api_key
        self._worker(f'Searching Comic Vine for {query}…',
                     lambda: search_comicvine(query, api_key),
                     lambda results: self.show_comic_matches('Comic Vine', results))

    def show_catalog_matches(self, provider, results):
        if not results:
            self._done(f'No {provider} matches found')
            messagebox.showinfo(f'{provider} lookup', f'No catalog matches found in {provider}.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f'Choose a {provider} entry')
        dialog.geometry('760x430')
        dialog.transient(self.root)
        ttk.Label(dialog, text=f'Click a {provider} result to add it to Watched and start its connector scan.').pack(anchor='w', padx=10, pady=(10, 5))
        matches = ttk.Treeview(dialog, columns=('title', 'author', 'details'), show='headings', selectmode='browse')
        for column, heading, width in (('title', 'Title', 310), ('author', 'Author', 180),
                                       ('details', 'Catalog details', 230)):
            matches.heading(column, text=heading)
            matches.column(column, width=width, anchor='w')
        matches.pack(fill='both', expand=True, padx=10, pady=5)
        for index, item in enumerate(results):
            detail = item.get('metadata') or {}
            if provider == 'MangaUpdates':
                licensing = detail.get('english_licensed')
                status = ('English licensed' if licensing is True else
                          'No English license listed' if licensing is False else 'English status unknown')
                details = status
            elif provider == 'RanobeDB':
                details = f"{detail.get('book_count') or '?'} volumes · {detail.get('status') or 'status unknown'}"
            elif provider == 'LNORI':
                details = f"{detail.get('publication_year') or ''} · {detail.get('volumes') or '?'} volumes"
            else:
                details = f"{detail.get('chapters') or '?'} chapters · {detail.get('release_status') or ''}"
            matches.insert('', 'end', iid=str(index), values=(item.get('title', ''), item.get('author', ''), details))

        def associate(_event=None):
            selected = matches.selection()
            if not selected:
                return
            item = results[int(selected[0])]
            dialog.destroy()
            if provider == 'RanobeDB':
                metadata = item.get('metadata') or {}
                self.watch_lightnovel_profile = {'ranobedb_id': metadata.get('catalog_id')}
                self.watch_comic_profile = {}
                self.watch_url.set(item.get('url') or '')
                self.watch_title.set(item.get('title') or '')
                self.watch_author.set(item.get('author') or '')
                self.watch_kind.set('Light novel')
                self._done('RanobeDB series selected; English release dates will be checked daily')
            elif provider == 'MangaUpdates':
                metadata = item.get('metadata') or {}
                item.update(metadata)
                item['provider'] = 'MangaUpdates'
                item['kind'] = 'manga'
                item['source_url'] = item.get('url')
                self.associate_comic_metadata(item)
            else:
                self.watch_lightnovel_profile = {}
                self.watch_url.set(item.get('url') or '')
                self.watch_title.set(item.get('title') or '')
                self.watch_author.set(item.get('author') or '')
                if provider == 'NovelArchive':
                    self.watch_kind.set('Web serial')
                    self._done('Metadata filled in; add an RSS/Atom/JSON Feed source to enable chapter tracking')
                else:
                    self.watch_kind.set('Light novel' if provider == 'LNORI' else 'Auto')
                    self._done(f'{provider} metadata filled in; select the matching RanobeDB series to track English volume release dates')
            if provider != 'MangaUpdates':
                self.add_watched(auto_scan=True)

        matches.bind('<ButtonRelease-1>', associate)
        matches.bind('<Double-1>', associate)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=(5, 10))
        ttk.Button(buttons, text='Use selected', command=associate).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)
        self._done(f'{len(results)} {provider} match(es)')

    def show_comic_matches(self, provider, results):
        if not results:
            self._done(f'No {provider} matches found')
            messagebox.showinfo(f'{provider} lookup', f'No manga matches found in {provider}.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f'Choose a {provider} manga')
        dialog.geometry('760x430')
        dialog.transient(self.root)
        ttk.Label(dialog, text=f'Click a {provider} result to add it to Watched and start its connector scan.').pack(anchor='w', padx=10, pady=(10, 5))
        matches = ttk.Treeview(dialog, columns=('title', 'author', 'kind', 'release', 'english'), show='headings', selectmode='browse')
        for column, heading, width in (('title', 'Title', 280), ('author', 'Creator', 170),
                                       ('kind', 'Type', 75), ('release', 'Start date', 90),
                                       ('english', 'English status', 125)):
            matches.heading(column, text=heading)
            matches.column(column, width=width, anchor='w')
        matches.pack(fill='both', expand=True, padx=10, pady=5)
        for index, item in enumerate(results):
            licensed = item.get('english_licensed')
            english = ('English licensed' if licensed is True else
                       'No English license listed' if licensed is False else '')
            matches.insert('', 'end', iid=str(index), values=(
                item.get('title', ''), item.get('author', ''), item.get('kind', '').title(),
                item.get('release_date') or '', english))

        def associate(_event=None):
            selected = matches.selection()
            if not selected:
                return
            item = results[int(selected[0])]
            dialog.destroy()
            self.associate_comic_metadata(item)

        matches.bind('<ButtonRelease-1>', associate)
        matches.bind('<Double-1>', associate)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=(5, 10))
        ttk.Button(buttons, text='Use selected', command=associate).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)
        self._done(f'{len(results)} {provider} match(es)')

    def associate_comic_metadata(self, item):
        self.watch_goodreads_profile = {}
        self.watch_comic_profile = {
            'provider': item.get('provider'),
            'anilist_id': item.get('anilist_id'),
            'mangadex_id': item.get('mangadex_id'),
            'taddy_id': item.get('taddy_id'),
            'comicvine_id': item.get('comicvine_id'),
            'english_licensed': item.get('english_licensed'),
            'english_publishers': item.get('english_publishers') or [],
            'sss_url': item.get('sss_url'),
            'external_ids': item.get('external_ids') or [],
            'comic_metadata_provenance': item.get('comic_metadata_provenance') or {},
            'comic_entity': item.get('comic_entity'),
            'catalog_metadata': {key: value for key, value in item.items()
                                 if key not in ('provider', 'kind', 'title', 'author',
                                                'release_date', 'source_url',
                                                'anilist_id', 'mangadex_id', 'taddy_id',
                                                'comicvine_id', 'english_licensed',
                                                'english_publishers', 'metadata', 'url',
                                                'sss_url', 'catalog_metadata')},
        }
        self.watch_url.set(item.get('source_url', ''))
        if item.get('sss_url'):
            self.watch_feed_url.set(item['sss_url'])
        self.watch_title.set(item.get('title', ''))
        self.watch_author.set('' if item.get('author') == 'Unknown' else item.get('author', ''))
        self.watch_release_date.set(item.get('release_date') or '')
        self.watch_kind.set(item.get('kind', 'manga').title())
        if item.get('comicvine_id'):
            self._done('Comic Vine volume selected; set COMICVINE_API_KEY for scheduled issue-date tracking')
        elif not item.get('mangadex_id') and not item.get('sss_url'):
            self._done(f"{item.get('provider', 'Catalog')} metadata filled in; add a chapter feed to enable automatic downloads")
        else:
            source_name = 'Taddy SSS' if item.get('sss_url') else 'MangaDex'
            self._done(f"{item.get('provider', 'Catalog')} metadata filled in; {source_name} chapter tracking is ready")
        self.add_watched(auto_scan=True)

    def show_goodreads_matches(self, results, expected_kind=None):
        if expected_kind:
            results = [item for item in results
                       if str(item.get('kind') or '').casefold() == expected_kind]
        if not results:
            self._done('No book metadata matches found')
            messagebox.showinfo(
                'Book lookup',
                f'No {expected_kind or "book"} matches found across the metadata catalogs.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Choose a book metadata match')
        dialog.geometry('1040x460')
        dialog.transient(self.root)
        ttk.Label(dialog, text='Click a result to add it to Watched and search the connected book sources.').pack(anchor='w', padx=10, pady=(10, 5))
        matches = ttk.Treeview(
            dialog, columns=('provider', 'kind', 'title', 'author', 'series'),
            show='headings', selectmode='browse')
        matches.heading('provider', text='Source')
        matches.heading('kind', text='Type')
        matches.heading('title', text='Title')
        matches.heading('author', text='Author')
        matches.heading('series', text='Series')
        matches.column('provider', width=105, anchor='w')
        matches.column('kind', width=80, anchor='w')
        matches.column('title', width=300, anchor='w')
        matches.column('author', width=230, anchor='w')
        matches.column('series', width=290, anchor='w')
        matches.pack(fill='both', expand=True, padx=10, pady=5)

        def normalized_title(value):
            return ' '.join(re.findall(r'[a-z0-9]+', str(value or '').casefold()))

        def display_series(item):
            if item.get('kind') == 'series':
                return str(item.get('title') or '').strip()
            catalog = item.get('catalog_metadata') or item.get('metadata') or {}
            values = item.get('series') or item.get('series_name')
            if not values and isinstance(catalog, dict):
                values = (catalog.get('series') or catalog.get('normalized_series') or
                          catalog.get('series_references') or catalog.get('series_name'))
            if isinstance(values, dict):
                values = [values]
            elif not isinstance(values, (list, tuple)):
                values = [values] if values else []
            names = []
            for entry in values:
                if isinstance(entry, dict):
                    name = str(entry.get('name') or entry.get('seriesTitle') or
                               entry.get('title') or '').strip()
                    position = entry.get('series_index')
                    if position is None:
                        position = entry.get('orderNumber')
                    if position is None:
                        position = entry.get('volumeSeriesOrder')
                else:
                    name, position = str(entry or '').strip(), None
                if name and name not in names:
                    if position is not None and not re.search(
                            r'#\s*' + re.escape(str(position)) + r'\s*$', name):
                        name += f' #{position}'
                    names.append(name)
            return '; '.join(names)

        # Some providers omit author or series fields. Fill a blank only when
        # another exact-title book result agrees on one value, so unrelated
        # editions or same-title books do not get mislabeled.
        title_metadata = {}
        for candidate in results:
            if candidate.get('kind') != 'book':
                continue
            key = normalized_title(candidate.get('title'))
            if not key:
                continue
            details = title_metadata.setdefault(key, {'authors': set(), 'series': set()})
            candidate_author = str(candidate.get('author') or '').strip()
            if candidate_author and candidate_author.casefold() != 'unknown':
                details['authors'].add(candidate_author)
            candidate_series = display_series(candidate)
            if candidate_series:
                details['series'].add(candidate_series)

        for index, item in enumerate(results):
            author = str(item.get('author') or '').strip()
            if not author or author.casefold() == 'unknown':
                if item.get('kind') == 'author':
                    author = str(item.get('title') or '')
                else:
                    authors = list(dict.fromkeys(
                        str(book.get('author') or '').strip()
                        for book in item.get('books') or []
                        if isinstance(book, dict) and book.get('author') and
                        str(book.get('author')).strip().casefold() != 'unknown'))
                    author = ', '.join(authors[:2])
                    if len(authors) > 2:
                        author += f' +{len(authors) - 2}'
                inferred = title_metadata.get(normalized_title(item.get('title')), {})
                if not author and len(inferred.get('authors', ())) == 1:
                    author = next(iter(inferred['authors']))
            series = display_series(item)
            if not series:
                inferred = title_metadata.get(normalized_title(item.get('title')), {})
                if len(inferred.get('series', ())) == 1:
                    series = next(iter(inferred['series']))
            matches.insert('', 'end', iid=str(index), values=(
                item.get('provider') or 'Goodreads', item['kind'].title(), item['title'],
                author or 'Unknown', series))

        def associate(_event=None):
            selected = matches.selection()
            if not selected:
                return
            result = results[int(selected[0])]
            dialog.destroy()
            self._close_watch_add_dialog()
            self.watch_goodreads_profile = {}
            self._goodreads_pending_search_query = ''
            self._goodreads_pending_search_url = result['source_url']
            self.watch_url.set(result['source_url'])
            self.watch_title.set(result['title'])
            self.watch_author.set('')
            self.watch_release_date.set('')
            self.watch_kind.set(result['kind'].title())

            def load_metadata():
                if result.get('provider', 'Goodreads').casefold() != 'goodreads':
                    return result
                parsed = fetch_goodreads(result['source_url'])
                if ((not parsed.get('author') or
                     str(parsed.get('author')).casefold() == 'unknown') and
                        result.get('author') and
                        str(result.get('author')).casefold() != 'unknown'):
                    parsed['author'] = result['author']
                if not parsed.get('series') and result.get('series'):
                    parsed['series'] = result['series']
                if result.get('kind') in ('series', 'author'):
                    combined = {book.get('source_url'): book
                                for book in parsed.get('books', []) if book.get('source_url')}
                    for book in result.get('books') or []:
                        if book.get('source_url'):
                            combined.setdefault(book['source_url'], book)
                    parsed['books'] = list(combined.values())
                for key in ('catalog_sources', 'catalog_ids'):
                    if result.get(key):
                        parsed[key] = result[key]
                parsed.setdefault('provider', 'Goodreads')
                return parsed
            self._worker('Loading Goodreads metadata…',
                         load_metadata,
                         self._associate_book_metadata,
                         on_error=lambda error: self._auto_add_goodreads_fallback(result, error))

        matches.bind('<ButtonRelease-1>', associate)
        matches.bind('<Double-1>', associate)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=(5, 10))
        ttk.Button(buttons, text='Use selected', command=associate).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)
        self._done(f'{len(results)} book metadata match(es)')

    def _associate_book_metadata(self, metadata):
        provider = str(metadata.get('provider') or '').casefold()
        source_url = metadata.get('source_url') or metadata.get('url') or ''
        if provider in {'goodreads', 'open library'} or not provider:
            self.associate_goodreads_metadata(metadata, auto_add=True)
            return

        self.watch_goodreads_profile = {}
        self.watch_comic_profile = {}
        self.watch_url.set(source_url)
        self.watch_title.set(metadata.get('title') or '')
        self.watch_author.set('' if metadata.get('author') == 'Unknown' else metadata.get('author', ''))
        self.watch_release_date.set(metadata.get('release_date') or '')
        self.watch_feed_url.set('')
        catalog = metadata.get('catalog_metadata') or metadata.get('metadata') or {}
        if provider == 'novelarchive':
            self.watch_lightnovel_profile = {}
            self.watch_kind.set('Web serial')
        elif provider in {'lnori', 'ranobedb'}:
            self.watch_lightnovel_profile = dict(catalog)
            if provider == 'ranobedb':
                self.watch_lightnovel_profile['ranobedb_id'] = catalog.get('catalog_id')
            self.watch_kind.set('Light novel')
        else:
            self.associate_goodreads_metadata(metadata, auto_add=True)
            return
        self.add_watched(auto_scan=True)

    def associate_goodreads_metadata(self, metadata, auto_add=False):
        self.watch_comic_profile = {}
        self.watch_lightnovel_profile = {}
        self.watch_goodreads_profile = dict(metadata)
        self.watch_url.set(metadata.get('source_url', ''))
        self.watch_title.set(metadata.get('title', ''))
        self.watch_author.set('' if metadata.get('author') == 'Unknown' else metadata.get('author', ''))
        self.watch_release_date.set(metadata.get('release_date') or '')
        self.watch_kind.set(metadata.get('kind', 'book').title())
        if auto_add:
            self.add_watched(auto_scan=True)
        else:
            self._done(f"{metadata.get('provider', 'Goodreads')} metadata filled in; review and add it to Watched")

    def associate_media_catalog(self, item, category=None):
        """Create a video/game watch from a normalized metadata search result."""
        category = category or item.get('category') or self.search_category.get()
        if category == 'Anime':
            kind, fallback_subtype = 'video', 'anime'
        elif category in {'Videos', 'Auto-detect'}:
            kind, fallback_subtype = 'video', 'movie'
        elif category == 'Games':
            kind, fallback_subtype = 'game', 'base_game'
        else:
            self._done(f'{category} watch selection is not supported here yet')
            return
        variants = [dict(row) for row in (item.get('metadata_variants') or [])
                    if isinstance(row, dict)]
        if not variants and item.get('source_role') != 'download':
            variants = [dict(item)]
        preferred = next((row for row in variants
                          if str(row.get('provider') or '').casefold() != 'wikidata'),
                         variants[0] if variants else dict(item))
        catalog = dict(preferred.get('catalog_metadata') or preferred.get('metadata') or {})
        metadata_type = str(preferred.get('entity_type') or catalog.get('entity_type') or
                            ('series' if preferred.get('video_subtype') in
                             {'tv_series', 'web_series', 'miniseries'} else 'work'))
        subtype = str(preferred.get('video_subtype') or preferred.get('game_subtype') or
                      catalog.get('video_subtype') or catalog.get('game_subtype') or fallback_subtype)
        external_ids = []
        for row in variants + [preferred, dict(item)]:
            for external_id in row.get('external_ids') or (row.get('catalog_metadata') or {}).get('external_ids') or []:
                if not isinstance(external_id, dict) or not external_id.get('provider') or not external_id.get('external_id'):
                    continue
                if not any(old['provider'] == external_id['provider'] and
                           str(old['external_id']) == str(external_id['external_id'])
                           for old in external_ids):
                    external_ids.append(dict(external_id))
            qid = (row.get('qid') or (row.get('catalog_metadata') or {}).get('wikidata_qid'))
            if qid and not any(old['provider'] == 'wikidata' and old['external_id'] == str(qid)
                               for old in external_ids):
                external_ids.append({'provider': 'wikidata', 'external_id': str(qid),
                                     'source_url': row.get('source_url')})
        provider = str(preferred.get('provider') or item.get('provider') or 'Catalog')
        metadata = {'sources': self._watch_sources(), 'entity_type': metadata_type,
                    'catalog_provider': provider, 'catalog_metadata': catalog,
                    'external_ids': external_ids,
                    'provider_records': [{
                        'provider': row.get('provider'), 'source_url': row.get('source_url'),
                        'external_ids': row.get('external_ids') or
                                        (row.get('catalog_metadata') or {}).get('external_ids') or [],
                        'release_date': row.get('release_date'),
                        'status': row.get('publication_status'),
                    } for row in variants]}
        profile_id = self.active_profile_id.get().strip()
        if profile_id:
            metadata['library_profile_ids'] = [profile_id]
        announcement_feeds = [value.strip() for value in
                              self.watch_announcement_feeds.get().split(';') if value.strip()]
        if announcement_feeds:
            metadata['announcement_feed_urls'] = announcement_feeds
        if kind == 'video':
            metadata['video_subtype'] = subtype
        else:
            metadata['game_subtype'] = subtype
        title = str(item.get('title') or preferred.get('title') or '').strip()
        if not title:
            messagebox.showerror('Add to Watched', 'The selected catalog result has no title.')
            return
        author = str(item.get('author') or preferred.get('author') or 'Unknown')
        source_url = preferred.get('source_url') or item.get('source_url') or item.get('url') or None
        release_date = preferred.get('release_date') or item.get('release_date')
        qid = next((value['external_id'] for value in external_ids
                    if value['provider'] == 'wikidata'), None)
        data_root = self.data_root

        def action():
            db = connect(data_root)
            try:
                watch_id = add_watch(db, kind, title, author, source_url, release_date, metadata)
                if profile_id:
                    assign_watch_profile(db, watch_id, profile_id)
                watch = db.execute('SELECT * FROM watched_items WHERE id=?',
                                   (watch_id,)).fetchone()
                graph_diff = None
                if qid and watch and watch['media_entity_id']:
                    imported = import_wikidata_entity(db, qid,
                                                      watch_entity_id=int(watch['media_entity_id']))
                    if int(imported.get('root_entity_id') or 0) == int(watch['media_entity_id']):
                        _apply_wikidata_watch_policy(db, dict(watch), imported)
                    graph_diff = capture_graph_snapshot(db, int(watch['media_entity_id']))
                return {'watch_id': watch_id, 'title': title, 'graph_diff': graph_diff}
            finally:
                db.close()

        def added(result):
            self.refresh_watches()
            self.refresh_queue()
            self._done(f"Watching {result['title']}; searching its connected {category} sources")
            self._run_watch_scan(force=True, automatic=True)

        self._worker(f'Adding {title} to Watched', action, added)

    def _auto_add_goodreads_fallback(self, result, error):
        self.logger.warning('Goodreads detail page unavailable for %s (%s): %s',
                            result.get('kind'), result.get('title'), error)
        fallback = dict(result)
        fallback.setdefault('provider', 'Goodreads')
        self.associate_goodreads_metadata(fallback, auto_add=True)
        self._done(f"Goodreads page unavailable; added {result.get('kind', 'entry')} and saved the error in logs")

    def goodreads_page_unavailable(self, result, error):
        self._goodreads_pending_search_query = ''
        self._goodreads_pending_search_url = result['source_url']
        self._done('Goodreads name and URL filled in; page import is available')
        opened = webbrowser.open(result['source_url'], new=1)
        message = (f'{error}\n\nI filled in {result["kind"]} “{result["title"]}” and its Goodreads URL. '
                   'You can still add it now; its first successful page check will establish a safe baseline. '
                   'To import the author’s book list now, save the opened page as HTML and click '
                   'Import Goodreads HTML.')
        if not opened:
            message += '\n\nThe Goodreads page could not be opened automatically.'
        messagebox.showwarning('Goodreads page unavailable', message)

    def load_goodreads_html(self):
        if self._goodreads_pending_search_query and self._goodreads_pending_search_url:
            url = self._goodreads_pending_search_url
        else:
            url = self.watch_url.get().strip()
        if not url:
            url = simpledialog.askstring('Import Goodreads HTML',
                                         'Goodreads search, profile, series, or book URL:') or ''
        if not url.strip():
            return
        path = filedialog.askopenfilename(
            initialdir=str(Path.home() / 'Downloads'), title='Choose saved Goodreads page HTML',
            filetypes=[('HTML pages', '*.html *.htm'), ('All files', '*.*')])
        if not path:
            return
        if urllib.parse.urlsplit(url.strip()).path.casefold() == '/search':
            query = self._goodreads_pending_search_query or self.watch_title.get().strip()
            self._worker('Reading saved Goodreads search…',
                         lambda: parse_goodreads_search_html(query, path, url.strip()),
                         self.show_goodreads_matches)
        else:
            self._worker('Reading saved Goodreads page…',
                         lambda: parse_goodreads_html(url.strip(), path), self.associate_goodreads_metadata)

    def add_watched(self, auto_scan=False):
        url = self.watch_url.get().strip()
        title = self.watch_title.get().strip()
        author = self.watch_author.get().strip() or 'Unknown'
        release = self.watch_release_date.get().strip() or None
        entered_kind = self.watch_kind.get().casefold()
        video_feed_subtypes = {
            'web video series': 'web_series',
            'video podcast': 'video_podcast',
            'documentary series': 'documentary_series',
            'performance series': 'performance_series',
        }
        anime_watch = entered_kind == 'anime'
        video_feed_watch = entered_kind in video_feed_subtypes
        kind = entered_kind
        if video_feed_watch or anime_watch:
            kind = 'video'
        kind = {'web serial': 'webfiction', 'light novel': 'lightnovel'}.get(kind, kind)
        kind = None if kind == 'auto' else kind
        if kind is None and mangadex_id_from_url(self.watch_url.get().strip()):
            kind = 'manga'
        sources = self._watch_sources()
        feed_url = self.watch_feed_url.get().strip()
        announcement_feeds = list(dict.fromkeys(
            value.strip() for value in self.watch_announcement_feeds.get().split(';')
            if value.strip()))
        selector = self.watch_selector.get().strip() or 'article,main'
        comic_profile = dict(self.watch_comic_profile)
        lightnovel_profile = dict(self.watch_lightnovel_profile)
        goodreads_profile = dict(self.watch_goodreads_profile)
        active_profile_id = self.active_profile_id.get().strip()

        def action():
            db = connect(self.data_root)
            try:
                def attach_profile(result):
                    if active_profile_id:
                        assign_watch_profile(db, int(result[0]), active_profile_id)
                    return result

                watch_title = title
                watch_url = url
                watch_author = author
                watch_kind = kind or 'book'
                goodreads_failed = False
                if watch_kind == 'author' and not watch_url:
                    if not watch_title:
                        raise ValueError('Enter an author name or choose an author from a metadata lookup.')
                    matches = search_book_metadata(watch_title, limit=60)
                    normalized = lambda value: ' '.join(re.findall(
                        r'[a-z0-9]+', str(value or '').casefold()))
                    exact = [dict(match) for match in matches
                             if match.get('kind') == 'author' and
                             normalized(match.get('title')) == normalized(watch_title) and
                             match.get('source_url')]
                    if not exact:
                        raise ValueError(
                            f'No exact author profile was found for “{watch_title}”. '
                            'Try the Goodreads or Open Library author profile URL.')
                    exact.sort(key=lambda match: (
                        0 if str(match.get('provider') or '').casefold() == 'goodreads' else
                        1 if str(match.get('provider') or '').casefold() == 'open library' else 2))
                    profile = exact[0]
                    profile_url = str(profile['source_url'])
                    if str(profile.get('provider') or '').casefold() == 'goodreads':
                        try:
                            return attach_profile(add_from_goodreads(
                                db, profile_url, 'author', watch_title, 'Unknown', release, sources))
                        except Exception as exc:
                            self.logger.warning(
                                'Goodreads author profile read failed for %s; saving its search result and retrying by watch scan: %s',
                                profile_url, exc)
                    profile['kind'] = 'author'
                    return attach_profile(add_from_goodreads_metadata(
                        db, profile, profile_url, 'author', watch_title, 'Unknown', release, sources))
                host = (urllib.parse.urlsplit(watch_url).hostname or '').casefold()
                is_goodreads_url = host == 'goodreads.com' or host.endswith('.goodreads.com')
                if watch_url and watch_kind in ('book', 'series', 'author'):
                    if goodreads_profile and goodreads_profile.get('source_url') == watch_url:
                        return attach_profile(add_from_goodreads_metadata(
                            db, goodreads_profile, watch_url, kind, watch_title or None,
                            watch_author, release, sources))
                    if is_goodreads_url:
                        try:
                            return attach_profile(add_from_goodreads(
                                db, watch_url, kind, watch_title or None, watch_author, release, sources))
                        except Exception as exc:
                            goodreads_failed = True
                            self.logger.warning('Goodreads metadata fetch failed for %s; retaining entered metadata: %s',
                                                watch_url, exc)
                            if not watch_title:
                                raise
                metadata = {'sources': sources}
                if active_profile_id:
                    metadata['library_profile_ids'] = [active_profile_id]
                if watch_kind in {'video', 'game'}:
                    metadata['entity_type'] = 'series' if anime_watch else 'work'
                    metadata['video_subtype' if watch_kind == 'video' else 'game_subtype'] = (
                        ('anime' if anime_watch else 'movie')
                        if watch_kind == 'video' else 'base_game')
                    if announcement_feeds:
                        metadata['announcement_feed_urls'] = announcement_feeds
                if watch_kind in ('series', 'author'):
                    metadata['known_books'] = []
                    if goodreads_failed:
                        metadata['goodreads_baseline_pending'] = True
                if watch_kind in ('comic', 'manga', 'webcomic', 'webtoon'):
                    comic_profile['mangadex_id'] = (comic_profile.get('mangadex_id') or
                                                     mangadex_id_from_url(watch_url))
                    comic_feed = comic_profile.get('sss_url') or feed_url
                    if comic_feed:
                        discovered = discover_feed(comic_feed, watch_kind, limit=200)
                        watch_title = watch_title or discovered.get('title') or ''
                        if watch_author == 'Unknown':
                            watch_author = discovered.get('author') or 'Unknown'
                        if discovered.get('sss_metadata') and not comic_profile.get('catalog_metadata'):
                            comic_profile['catalog_metadata'] = discovered['sss_metadata']
                    comic_profile.update({'feed_url': comic_feed, 'selector': selector,
                                          'language': comic_profile.get('language') or 'en',
                                          'known_chapters': []})
                    # A title-only comic watch searches the enabled comic
                    # sources (including GetComics) on each scheduled scan.
                    metadata['comic'] = comic_profile
                    metadata['external_ids'] = comic_profile.get('external_ids') or []
                if watch_kind == 'lightnovel':
                    if not lightnovel_profile.get('ranobedb_id'):
                        match = re.search(r'/series/(\d+)(?:/|$)', urllib.parse.urlsplit(watch_url).path)
                        if match:
                            lightnovel_profile['ranobedb_id'] = match.group(1)
                    if (watch_url and not lightnovel_profile.get('ranobedb_id')
                            and urllib.parse.urlsplit(watch_url).hostname not in {'lnori.com', 'novelarchive.cc'}):
                        raise ValueError('Look up a RanobeDB series for English release dates, or use an LNORI/NovelArchive series URL')
                    metadata['lightnovel'] = lightnovel_profile
                if watch_kind == 'webfiction':
                    serial_feed = feed_url or watch_url
                    if not serial_feed:
                        raise ValueError('Enter a public RSS/Atom/JSON Feed source or a NovelArchive series URL')
                    is_novelarchive = urllib.parse.urlsplit(serial_feed).hostname == 'novelarchive.cc'
                    if not is_novelarchive:
                        discovered = discover_feed(serial_feed, watch_kind, limit=200)
                        watch_title = watch_title or discovered.get('title') or ''
                        if watch_author == 'Unknown':
                            watch_author = discovered.get('author') or 'Unknown'
                    metadata['serial'] = {'feed_url': serial_feed, 'selector': selector,
                                          'known_chapters': [], 'known_files': []}
                    watch_url = watch_url or serial_feed
                if video_feed_watch:
                    video_feed = feed_url or watch_url
                    if not video_feed:
                        raise ValueError('Enter an RSS/Atom/JSON Feed/JSON-LD/iCalendar episode feed or YouTube channel RSS URL')
                    discovered = discover_feed(video_feed, 'webfiction', limit=500)
                    watch_title = watch_title or discovered.get('title') or entered_kind.title()
                    if watch_author == 'Unknown':
                        watch_author = discovered.get('author') or 'Unknown'
                    metadata.update({'entity_type': 'series',
                                     'video_subtype': video_feed_subtypes[entered_kind],
                                     'catalog_provider': 'Video feed',
                                     'video_feed_url': video_feed,
                                     'video_feed_provider_metadata': discovered.get('provider_metadata') or {},
                                     'video_feed_known_episodes': [],
                                     'video_feed_watched_episodes': [],
                                     'video_feed_seen_at': {}})
                    watch_url = watch_url or video_feed
                elif watch_kind == 'fanfiction':
                    if not watch_url:
                        raise ValueError('Enter a public story URL for this fanfiction watch')
                    from .fanfiction import story_url_key
                    story_url_key(watch_url)
                    metadata['fanfiction'] = {'story_url': watch_url}
                    if not watch_title:
                        last_segment = Path(urllib.parse.urlsplit(watch_url).path.rstrip('/')).name
                        watch_title = urllib.parse.unquote(last_segment).replace('-', ' ').replace('_', ' ').strip()
                    watch_title = watch_title or 'Watched fanfiction'
                watch_id = add_watch(db, watch_kind, watch_title, watch_author, watch_url or feed_url or None,
                                     release, metadata)
                if active_profile_id:
                    assign_watch_profile(db, watch_id, active_profile_id)
                return watch_id, None
            finally:
                db.close()

        def added(result):
            goodreads_metadata = (result[1] if isinstance(result, tuple) and len(result) > 1
                                  and isinstance(result[1], dict) else {})
            self.watch_url.set('')
            self.watch_title.set('')
            self.watch_author.set('')
            self.watch_release_date.set('')
            self.watch_feed_url.set('')
            self.watch_announcement_feeds.set('')
            self.watch_comic_profile = {}
            self.watch_lightnovel_profile = {}
            self.watch_goodreads_profile = {}
            self.refresh_watches()
            self.refresh_queue()
            if goodreads_metadata.get('kind') == 'series':
                self._done('Series added; listed books are queued and will be searched automatically')
            elif goodreads_metadata.get('kind') == 'author':
                self._done('Author added; listed books are queued and new books will be added automatically')
            elif kind in ('comic', 'manga', 'webcomic', 'webtoon'):
                self._done('Comic watch added; new chapters will be checked daily')
            elif kind == 'lightnovel':
                self._done('Light-novel series added; English volumes will be searched on their release dates')
            elif kind == 'software':
                self._done('Software watch added; approved FOSS repositories will be checked daily for new releases')
            elif kind in ('webfiction', 'fanfiction'):
                self._done('Fanfiction watch added; its EPUB will update with new chapters daily' if kind == 'fanfiction'
                           else 'Web serial watch added; new chapters will be checked daily and saved as EPUBs')
            elif video_feed_watch:
                self._done(f'{entered_kind.title()} added; its feed will be checked daily for new episodes')
            elif announcement_feeds:
                self._done('Release feeds connected; existing posts were baselined and new announcements will enter Needs Review')
            elif goodreads_metadata.get('release_date'):
                self._done('Watched; release date saved')
            else:
                self._done('Added to watched')
            if (auto_scan or kind in ('series', 'author') or video_feed_watch or
                    bool(announcement_feeds) or
                    goodreads_metadata.get('kind') in ('series', 'author')):
                self.root.after(100, self._run_added_watch_scan)

        if url and kind in ('book', 'series', 'author'):
            worker_message = 'Reading Goodreads…'
        elif kind == 'author' and not url:
            worker_message = 'Looking up author and bibliography…'
        else:
            worker_message = 'Adding watch…'
        self._worker(worker_message, action, added)

    def _run_added_watch_scan(self):
        if self.watch_scan_running:
            self.root.after(500, self._run_added_watch_scan)
            return
        self._run_watch_scan(force=True, automatic=True)

    def refresh_watches(self):
        if hasattr(self, 'webcomics_panel'):
            self.webcomics_panel.refresh()
        self.watch_rows = list_watches(self.db)
        for row in self.watched_view.get_children():
            self.watched_view.delete(row)
        state_names = {'upcoming': 'Unreleased', 'watching': 'Watching', 'searching': 'Searching',
                       'downloaded': 'Saved to library', 'failed': 'Retry scheduled', 'series': 'Watching series',
                       'author': 'Watching author'}
        for row in self.watch_rows:
            state = state_names.get(row.get('state'), row.get('state', ''))
            try:
                watch_metadata = json.loads(row.get('metadata') or '{}')
            except (TypeError, ValueError):
                watch_metadata = {}
            if (watch_metadata.get('publication_status') ==
                    'announced; release date unconfirmed' and not row.get('release_date')):
                state = 'Announced · date TBD'
            if row.get('kind') in ('comic', 'manga', 'webcomic', 'webtoon') and row.get('state') == 'watching':
                state = 'Watching ' + row['kind']
            if row.get('kind') == 'lightnovel':
                try:
                    profile = json.loads(row.get('metadata') or '{}').get('lightnovel') or {}
                    release_status = profile.get('english_release_status')
                    if release_status == 'not_listed':
                        state = 'No English release listed'
                    elif release_status == 'upcoming':
                        state = f"English release upcoming ({profile.get('english_upcoming_volumes', 0)})"
                    elif release_status == 'available':
                        state = f"English listed ({profile.get('english_available_volumes', 0)})"
                except (TypeError, ValueError):
                    pass
            kind_label = str(row.get('kind') or '').title()
            if row.get('kind') == 'video' and watch_metadata.get('video_subtype') == 'web_series':
                kind_label = 'Web video series'
            elif row.get('kind') == 'video_episode':
                kind_label = 'Video episode'
            if row.get('last_error') and row.get('state') not in ('downloaded', 'upcoming'):
                state += ': ' + row['last_error'][:42]
            self.watched_view.insert('', 'end', iid=str(row['id']), values=(
                kind_label, row.get('title', ''), row.get('author', ''), row.get('release_date') or '', state,
                row.get('last_checked_at') or '', row.get('file_path') or ''))

    def delete_watched(self):
        selected = self.watched_view.selection()
        if not selected:
            return
        remove_watch(self.db, int(selected[0]))
        self.refresh_watches()

    def _selected_watch_row(self):
        selected = self.watched_view.selection()
        if not selected:
            return None
        return next((item for item in self.watch_rows if str(item.get('id')) == selected[0]), None)

    def view_media_continuity(self):
        watch = self._selected_watch_row()
        if not watch:
            messagebox.showinfo('Continuity', 'Select a watched item first.')
            return
        entity_id = watch.get('media_entity_id')
        if not entity_id:
            messagebox.showinfo('Continuity', 'This watched item is not linked to a media graph entity yet.')
            return
        data_root = self.data_root

        def action():
            db = connect(data_root)
            try:
                return (get_media_graph(db, int(entity_id), max_depth=4),
                        list_media_changes(db, int(entity_id)))
            finally:
                db.close()

        self._worker(f'Loading continuity for {watch.get("title", "selection")}', action,
                     lambda result: self._show_media_continuity(watch, *result))

    def _show_media_continuity(self, watch, graph, changes):
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Continuity and releases — {watch.get('title', 'selection')}")
        dialog.geometry('980x570')
        dialog.transient(self.root)
        pages = ttk.Notebook(dialog)
        pages.pack(fill='both', expand=True, padx=8, pady=8)
        graph_page = ttk.Frame(pages, padding=6)
        change_page = ttk.Frame(pages, padding=6)
        pages.add(graph_page, text='Related media')
        pages.add(change_page, text='Observed changes')
        columns = ('record', 'relationship', 'position', 'evidence')
        tree = ttk.Treeview(graph_page, columns=columns, show='tree headings', height=18)
        tree.heading('#0', text='Media entity')
        for column, label, width in (('record', 'Type / release', 145),
                                     ('relationship', 'Relationship', 185),
                                     ('position', 'Order / date', 115),
                                     ('evidence', 'Evidence source', 390)):
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor='w')
        tree.pack(fill='both', expand=True)
        entity_by_id = {int(row['id']): row for row in graph.get('entities') or []}
        root = entity_by_id.get(int(graph.get('root_entity_id') or 0), {})
        tree.insert('', 'end', iid='root', text=root.get('title') or watch.get('title'),
                    values=(root.get('entity_type') or watch.get('kind'), 'Selected watch',
                            root.get('release_date') or '', root.get('canonical_url') or ''))
        for index, relationship in enumerate(graph.get('relationships') or []):
            source = entity_by_id.get(int(relationship['source_entity_id']), {})
            target = entity_by_id.get(int(relationship['target_entity_id']), {})
            evidence = relationship.get('evidence') or []
            evidence_text = '; '.join(dict.fromkeys(
                str(item.get('provider') or 'source') +
                (f" — {item.get('source_url')}" if item.get('source_url') else '')
                for item in evidence))
            order = (relationship.get('metadata') or {}).get('series_position') or ''
            tree.insert('', 'end', iid=f'rel-{index}',
                        text=f"{source.get('title', '?')} → {target.get('title', '?')}",
                        values=(relationship.get('status', 'candidate'),
                                relationship.get('relationship_type', '').replace('_', ' '),
                                order, evidence_text or f"confidence {relationship.get('confidence', 0):.2f}"))
        for index, release in enumerate(graph.get('releases') or []):
            entity = entity_by_id.get(int(release['entity_id']), {})
            detail = (release.get('metadata') or {}).get('title') or entity.get('title') or ''
            release_type = release.get('release_type') or ''
            if release.get('is_canonical'):
                release_type += ' · canonical'
            tree.insert('', 'end', iid=f'release-{index}',
                        text=detail,
                        values=(release.get('status') or 'release', release_type,
                                release.get('release_date') or 'date TBD',
                                release.get('provider') or ''))
        for index, claim in enumerate(graph.get('claims') or []):
            tree.insert('', 'end', iid=f'claim-{index}',
                        text=f"{claim.get('field_name')}: {claim.get('value')}",
                        values=('metadata claim', '', '',
                                f"{claim.get('provider')} ({claim.get('confidence', 0):.2f})"))
        if graph.get('truncated'):
            ttk.Label(graph_page, text='The graph view reached its display limit; more related entities may exist.').pack(anchor='w')

        change_columns = ('detected', 'change', 'before', 'after')
        change_tree = ttk.Treeview(change_page, columns=change_columns, show='headings', height=18)
        for column, label, width in (('detected', 'Detected', 150), ('change', 'Change', 220),
                                     ('before', 'Before', 240), ('after', 'After', 240)):
            change_tree.heading(column, text=label)
            change_tree.column(column, width=width, anchor='w')
        change_tree.pack(fill='both', expand=True)
        for index, change in enumerate(changes):
            def compact(value):
                text = json.dumps(value, ensure_ascii=False, sort_keys=True) if value is not None else ''
                return text[:300]
            change_tree.insert('', 'end', iid=f'change-{index}', values=(
                change.get('detected_at', ''), change.get('change_type', ''),
                compact(change.get('before')), compact(change.get('after'))))
        if not changes:
            ttk.Label(change_page, text='No changes have been recorded yet.').pack(anchor='w', pady=5)
        graph_actions = ttk.Frame(dialog)
        graph_actions.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Button(graph_actions, text='Refresh catalog relationships',
                   command=lambda: self._refresh_media_graph(watch, dialog)).pack(side='left')
        ttk.Button(graph_actions, text='Close', command=dialog.destroy).pack(side='right')

    def _refresh_media_graph(self, watch, dialog):
        entity_id = watch.get('media_entity_id')
        if not entity_id:
            return
        options = dict(self._watch_sources())
        options.update({
            'tmdb_api_key': self.tmdb_api_key,
            'igdb_client_id': self.igdb_client_id,
            'igdb_client_secret': self.igdb_client_secret,
        })
        data_root = self.data_root

        def action():
            db = connect(data_root)
            try:
                return refresh_media_graph(db, int(entity_id), source_options=options)
            finally:
                db.close()

        def refreshed(result):
            dialog.destroy()
            self._done(
                f"Refreshed {len(result.get('refreshed') or [])} catalog record(s) "
                f"across {result.get('entities_visited', 0)} graph entities")
            errors = result.get('errors') or []
            skipped = result.get('skipped') or []
            if errors or skipped or result.get('refresh_truncated'):
                messages = list(errors[:8])
                if skipped:
                    labels = ', '.join(
                        f"{item.get('provider')}:{item.get('external_id')}"
                        for item in skipped[:12])
                    messages.append('No graph refresh adapter is connected for: ' + labels)
                if result.get('refresh_truncated'):
                    messages.append(
                        'Refresh reached its 100-entity safety limit; some relationships were left unrefreshed.')
                messagebox.showwarning(
                    'Catalog refresh',
                    'Some catalog sources were unavailable or unsupported:\n\n' +
                    '\n'.join(messages))
            self.view_media_continuity()

        self._worker(f'Refreshing catalog relationships for {watch.get("title", "selection")}',
                     action, refreshed)

    def open_graph_review(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('Media graph — Needs Review')
        dialog.geometry('1000x600')
        dialog.transient(self.root)
        ttk.Label(dialog, text='Review uncertain identities, relationships, and catalog disagreements. Choose a canonical provider date, lifecycle status, or book series position; accepted exact announcement dates can schedule upcoming watches.',
                  wraplength=960, justify='left').pack(anchor='w', padx=10, pady=(10, 5))
        columns = ('issue', 'entity', 'title', 'status', 'created')
        tree = ttk.Treeview(dialog, columns=columns, show='headings', selectmode='browse', height=13)
        for column, label, width in (('issue', 'Issue', 155), ('entity', 'Entity', 200),
                                     ('title', 'Review item', 370), ('status', 'Status', 90),
                                     ('created', 'Created', 135)):
            tree.heading(column, text=label)
            tree.column(column, width=width, anchor='w')
        tree.pack(fill='both', expand=True, padx=10, pady=5)
        detail = tk.Text(dialog, height=6, wrap='word')
        detail.pack(fill='x', padx=10, pady=(2, 6))
        detail.configure(state='disabled')
        rows_by_id = {}

        def load_reviews():
            data_root = self.data_root

            def action():
                db = connect(data_root)
                try:
                    reviews = list_graph_reviews(db, status='open')
                    for item in reviews:
                        entity = (db.execute('SELECT title FROM media_entities WHERE id=?',
                                             (item.get('entity_id'),)).fetchone()
                                  if item.get('entity_id') else None)
                        item['entity_title'] = entity['title'] if entity else ''
                    return reviews
                finally:
                    db.close()

            def loaded(reviews):
                rows_by_id.clear()
                for item in tree.get_children():
                    tree.delete(item)
                for review in reviews:
                    iid = str(review['id'])
                    rows_by_id[iid] = review
                    tree.insert('', 'end', iid=iid, values=(
                        review.get('issue_type', '').replace('_', ' '),
                        review.get('entity_title') or review.get('entity_id') or '',
                        review.get('title', ''), review.get('status', ''),
                        review.get('created_at', '')))
                show_detail()
            self._worker('Loading review queue', action, loaded)

        def show_detail(_event=None):
            selection = tree.selection()
            review = rows_by_id.get(selection[0]) if selection else None
            detail.configure(state='normal')
            detail.delete('1.0', 'end')
            if review:
                detail.insert('1.0', json.dumps(review.get('detail') or {},
                                                ensure_ascii=False, indent=2, sort_keys=True))
            detail.configure(state='disabled')

        def resolve(status):
            selection = tree.selection()
            if not selection:
                messagebox.showinfo('Needs Review', 'Select a review item first.', parent=dialog)
                return
            review_id = int(selection[0])
            data_root = self.data_root

            def action():
                db = connect(data_root)
                try:
                    resolve_graph_review(db, review_id, status)
                    db.commit()
                    return True
                finally:
                    db.close()
            self._worker(f'Marking review {status}', action, lambda _result: load_reviews())

        def choose_release_date():
            selection = tree.selection()
            review = rows_by_id.get(selection[0]) if selection else None
            metadata_date_review = bool(
                review and review.get('issue_type') == 'conflicting_metadata' and
                (review.get('detail') or {}).get('field') == 'release_date')
            if not review or (review.get('issue_type') not in {
                    'conflicting_release_dates', 'ambiguous_release_dates',
                    'conflicting_episode_dates', 'ambiguous_episode_dates'} and
                    not metadata_date_review):
                messagebox.showinfo('Needs Review',
                                    'Select a release-date disagreement first.', parent=dialog)
                return
            if metadata_date_review:
                claims = [dict(claim, release_date=claim.get('value'),
                               region='Entity date', platform='', status='')
                          for claim in (review.get('detail') or {}).get('claims', [])
                          if claim.get('id') is not None and claim.get('value')]
            else:
                claims = [claim for claim in (review.get('detail') or {}).get('claims', [])
                          if claim.get('id') is not None and claim.get('release_date')]
            if not claims:
                messagebox.showinfo('Needs Review',
                                    'This review item has no dated observations to choose from.',
                                    parent=dialog)
                return
            picker = tk.Toplevel(dialog)
            picker.title('Choose canonical release date')
            picker.geometry('780x330')
            picker.transient(dialog)
            ttk.Label(picker, text='Choose the provider observation to use. Its source record stays in the graph; primary dates also schedule the watched item for this date.',
                      wraplength=740, justify='left').pack(anchor='w', padx=10, pady=(10, 6))
            fields = ('provider', 'date', 'region', 'platform', 'status')
            picker_tree = ttk.Treeview(picker, columns=fields, show='headings',
                                       selectmode='browse', height=8)
            for field, label, width in (('provider', 'Provider', 110), ('date', 'Release date', 120),
                                        ('region', 'Region', 125), ('platform', 'Platform', 175),
                                        ('status', 'Status', 160)):
                picker_tree.heading(field, text=label)
                picker_tree.column(field, width=width, anchor='w')
            picker_tree.pack(fill='both', expand=True, padx=10, pady=5)
            for index, claim in enumerate(claims):
                picker_tree.insert('', 'end', iid=str(index), values=(
                    claim.get('provider') or 'Unknown', claim.get('release_date') or '',
                    claim.get('region') or 'Unspecified', claim.get('platform') or 'All platforms',
                    claim.get('status') or ''))
            picker_tree.selection_set('0')

            def apply_choice():
                chosen = picker_tree.selection()
                if not chosen:
                    messagebox.showinfo('Choose release date', 'Select one provider observation.',
                                        parent=picker)
                    return
                release_id = int(claims[int(chosen[0])]['id'])
                review_id = int(review['id'])
                data_root = self.data_root

                def action():
                    db = connect(data_root)
                    try:
                        result = (set_reviewed_media_release_date(db, review_id, release_id)
                                  if metadata_date_review else
                                  set_reviewed_release_date(db, review_id, release_id))
                        db.commit()
                        return result
                    finally:
                        db.close()

                def applied(result):
                    picker.destroy()
                    self._done(f"Set canonical release date to {result['release_date']} ({result['provider']})")
                    load_reviews()

                self._worker('Applying reviewed release date', action, applied)

            buttons = ttk.Frame(picker)
            buttons.pack(fill='x', padx=10, pady=(4, 10))
            ttk.Button(buttons, text='Use selected date', command=apply_choice).pack(side='left')
            ttk.Button(buttons, text='Cancel', command=picker.destroy).pack(side='right')

        def choose_lifecycle_status():
            selection = tree.selection()
            review = rows_by_id.get(selection[0]) if selection else None
            if (not review or review.get('issue_type') != 'conflicting_metadata' or
                    (review.get('detail') or {}).get('field') != 'status'):
                messagebox.showinfo('Needs Review',
                                    'Select a lifecycle status disagreement first.', parent=dialog)
                return
            claims = [claim for claim in (review.get('detail') or {}).get('claims', [])
                      if claim.get('id') is not None and isinstance(claim.get('value'), str)
                      and claim.get('value').strip()]
            if not claims:
                messagebox.showinfo('Needs Review',
                                    'This review item has no selectable status observations.',
                                    parent=dialog)
                return
            picker = tk.Toplevel(dialog)
            picker.title('Choose canonical lifecycle status')
            picker.geometry('800x330')
            picker.transient(dialog)
            ttk.Label(picker, text='Choose the provider status to use. The other source observations remain in the graph, and this choice stays canonical across watch refreshes.',
                      wraplength=760, justify='left').pack(anchor='w', padx=10, pady=(10, 6))
            fields = ('provider', 'status', 'confidence', 'source')
            picker_tree = ttk.Treeview(picker, columns=fields, show='headings',
                                       selectmode='browse', height=8)
            for field, label, width in (('provider', 'Provider', 130), ('status', 'Status', 145),
                                        ('confidence', 'Confidence', 100), ('source', 'Evidence URL', 390)):
                picker_tree.heading(field, text=label)
                picker_tree.column(field, width=width, anchor='w')
            picker_tree.pack(fill='both', expand=True, padx=10, pady=5)
            for index, claim in enumerate(claims):
                picker_tree.insert('', 'end', iid=str(index), values=(
                    claim.get('provider') or 'Unknown', claim.get('value'),
                    f"{float(claim.get('confidence') or 0):.2f}", claim.get('source_url') or ''))
            picker_tree.selection_set('0')

            def apply_choice():
                chosen = picker_tree.selection()
                if not chosen:
                    messagebox.showinfo('Choose lifecycle status',
                                        'Select one provider observation.', parent=picker)
                    return
                claim_id = int(claims[int(chosen[0])]['id'])
                review_id = int(review['id'])
                data_root = self.data_root

                def action():
                    db = connect(data_root)
                    try:
                        result = set_reviewed_media_status(db, review_id, claim_id)
                        db.commit()
                        return result
                    finally:
                        db.close()

                def applied(result):
                    picker.destroy()
                    self._done(f"Set lifecycle status to {result['status']} ({result['provider']})")
                    load_reviews()

                self._worker('Applying reviewed lifecycle status', action, applied)

            buttons = ttk.Frame(picker)
            buttons.pack(fill='x', padx=10, pady=(4, 10))
            ttk.Button(buttons, text='Use selected status', command=apply_choice).pack(side='left')
            ttk.Button(buttons, text='Cancel', command=picker.destroy).pack(side='right')

        def choose_series_position():
            selection = tree.selection()
            review = rows_by_id.get(selection[0]) if selection else None
            if (not review or review.get('issue_type') != 'conflicting_metadata' or
                    (review.get('detail') or {}).get('field') != 'series_index'):
                messagebox.showinfo('Needs Review',
                                    'Select a series-position disagreement first.', parent=dialog)
                return
            claims = [claim for claim in (review.get('detail') or {}).get('claims', [])
                      if claim.get('id') is not None and claim.get('value') is not None]
            if not claims:
                messagebox.showinfo('Needs Review',
                                    'This review item has no selectable position observations.',
                                    parent=dialog)
                return
            picker = tk.Toplevel(dialog)
            picker.title('Choose canonical series position')
            picker.geometry('800x330')
            picker.transient(dialog)
            ttk.Label(picker, text='Choose the provider position to use. Other source observations remain in the graph, and this choice stays canonical across catalog refreshes.',
                      wraplength=760, justify='left').pack(anchor='w', padx=10, pady=(10, 6))
            fields = ('provider', 'position', 'confidence', 'source')
            picker_tree = ttk.Treeview(picker, columns=fields, show='headings',
                                       selectmode='browse', height=8)
            for field, label, width in (('provider', 'Provider', 130), ('position', 'Position', 115),
                                        ('confidence', 'Confidence', 100), ('source', 'Evidence URL', 410)):
                picker_tree.heading(field, text=label)
                picker_tree.column(field, width=width, anchor='w')
            picker_tree.pack(fill='both', expand=True, padx=10, pady=5)
            for index, claim in enumerate(claims):
                picker_tree.insert('', 'end', iid=str(index), values=(
                    claim.get('provider') or 'Unknown', claim.get('value'),
                    f"{float(claim.get('confidence') or 0):.2f}", claim.get('source_url') or ''))
            picker_tree.selection_set('0')

            def apply_choice():
                chosen = picker_tree.selection()
                if not chosen:
                    messagebox.showinfo('Choose series position',
                                        'Select one provider observation.', parent=picker)
                    return
                claim_id = int(claims[int(chosen[0])]['id'])
                review_id = int(review['id'])
                data_root = self.data_root

                def action():
                    db = connect(data_root)
                    try:
                        result = set_reviewed_media_series_index(db, review_id, claim_id)
                        db.commit()
                        return result
                    finally:
                        db.close()

                def applied(result):
                    picker.destroy()
                    self._done(f"Set canonical series position to {result['series_index']} ({result['provider']})")
                    load_reviews()

                self._worker('Applying reviewed series position', action, applied)

            buttons = ttk.Frame(picker)
            buttons.pack(fill='x', padx=10, pady=(4, 10))
            ttk.Button(buttons, text='Use selected position', command=apply_choice).pack(side='left')
            ttk.Button(buttons, text='Cancel', command=picker.destroy).pack(side='right')

        tree.bind('<<TreeviewSelect>>', show_detail)
        controls = ttk.Frame(dialog)
        controls.pack(fill='x', padx=10, pady=(0, 9))
        decisions = ttk.Frame(controls)
        decisions.pack(fill='x')
        canonical = ttk.Frame(controls)
        canonical.pack(fill='x', pady=(3, 0))
        ttk.Button(decisions, text='Accept (adds series member when applicable)',
                   command=lambda: resolve('accepted')).pack(side='left')
        ttk.Button(decisions, text='Reject', command=lambda: resolve('rejected')).pack(side='left', padx=5)
        ttk.Button(decisions, text='Dismiss', command=lambda: resolve('dismissed')).pack(side='left')
        ttk.Button(canonical, text='Choose canonical release date',
                   command=choose_release_date).pack(side='left')
        ttk.Button(canonical, text='Choose canonical status',
                   command=choose_lifecycle_status).pack(side='left', padx=5)
        ttk.Button(canonical, text='Choose series position',
                   command=choose_series_position).pack(side='left', padx=5)
        ttk.Button(canonical, text='Refresh', command=load_reviews).pack(side='left', padx=5)
        ttk.Button(canonical, text='Close', command=dialog.destroy).pack(side='right')
        load_reviews()

    def open_watch_policy_editor(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('Continuity watch policies')
        dialog.geometry('700x620')
        dialog.transient(self.root)
        ttk.Label(dialog, text='Choose what the watch system does when it discovers each relationship type. Auto-add is limited to high-confidence relationships.',
                  wraplength=660, justify='left').pack(anchor='w', padx=10, pady=(10, 8))
        media_type = tk.StringVar(value='book')
        policy_action = tk.StringVar(value='discover')
        top = ttk.Frame(dialog)
        top.pack(fill='x', padx=10, pady=4)
        ttk.Label(top, text='Media type').pack(side='left')
        selector = ttk.Combobox(top, textvariable=media_type, state='readonly', width=20,
                                 values=('book', 'comic', 'manga', 'video', 'game',
                                         'webfiction', 'fanfiction'))
        selector.pack(side='left', padx=6)
        columns = ('relationship', 'action')
        tree = ttk.Treeview(dialog, columns=columns, show='headings', selectmode='browse', height=18)
        tree.heading('relationship', text='Relationship or release type')
        tree.heading('action', text='Current action')
        tree.column('relationship', width=390, anchor='w')
        tree.column('action', width=170, anchor='w')
        tree.pack(fill='both', expand=True, padx=10, pady=6)
        action_row = ttk.Frame(dialog)
        action_row.pack(fill='x', padx=10, pady=4)
        ttk.Label(action_row, text='Set selected action').pack(side='left')
        ttk.Combobox(action_row, textvariable=policy_action, state='readonly', width=18,
                     values=('auto_add', 'discover', 'metadata_only', 'ignore')).pack(side='left', padx=6)
        pending = {}

        def render():
            for iid in tree.get_children():
                tree.delete(iid)
            base = dict(DEFAULT_WATCH_POLICY)
            base.update(get_watch_policy(self.db, 'global', ''))
            base.update(get_watch_policy(self.db, 'media_type', media_type.get()))
            pending.clear()
            pending.update(base)
            for key, value in base.items():
                tree.insert('', 'end', iid=key, values=(key.replace('_', ' '), value))

        def stage_action():
            selection = tree.selection()
            if not selection:
                return
            key = selection[0]
            pending[key] = policy_action.get()
            tree.set(key, 'action', pending[key])

        def save():
            try:
                set_watch_policy(self.db, 'media_type', media_type.get(), pending)
                self.db.commit()
            except Exception as exc:
                messagebox.showerror('Watch policies', str(exc), parent=dialog)
                return
            self._done(f"Saved {media_type.get()} watch policies")
            dialog.destroy()

        selector.bind('<<ComboboxSelected>>', lambda _event: render())
        ttk.Button(action_row, text='Stage action', command=stage_action).pack(side='left')
        controls = ttk.Frame(dialog)
        controls.pack(fill='x', padx=10, pady=(5, 10))
        ttk.Button(controls, text='Save policies', command=save).pack(side='right')
        ttk.Button(controls, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)
        render()

    def edit_watch_release_date(self):
        selected = self.watched_view.selection()
        if not selected:
            return
        row = next((item for item in self.watch_rows if str(item.get('id')) == selected[0]), None)
        if not row:
            return
        if row.get('kind') != 'book':
            messagebox.showinfo('Release date', 'Select a book watch to set its release date.')
            return
        value = simpledialog.askstring(
            'Set release date',
            f"Release date for {row.get('title', 'book')} (YYYY-MM-DD). Leave blank to clear it:",
            initialvalue=row.get('release_date') or '', parent=self.root)
        if value is None:
            return
        try:
            updated = set_watch_release_date(self.db, int(row['id']), value)
        except ValueError as exc:
            messagebox.showerror('Release date', str(exc))
            return
        self.refresh_watches()
        self.refresh_queue()
        if updated.get('release_date'):
            self._done(f"Saved release date {updated['release_date']}; source search is scheduled for that date")
        else:
            self._done('Cleared release date; the announced work will wait for a confirmed date')

    def discover_watched_relationships(self):
        selected = self.watched_view.selection()
        if not selected:
            messagebox.showinfo('Discover related media', 'Select a watched item first.')
            return
        watch = next((item for item in self.watch_rows if str(item.get('id')) == selected[0]), None)
        if not watch:
            return
        title = str(watch.get('title') or '').strip()
        if not title:
            return
        self._worker(f'Searching Wikidata for {title}', lambda: search_wikidata(title),
                     lambda results: self.show_wikidata_matches(watch, results))

    def show_wikidata_matches(self, watch, results):
        if not results:
            self._done('No Wikidata entities found')
            messagebox.showinfo('Discover related media',
                                f'Wikidata returned no entity matches for {watch.get("title", "the selection")}.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Choose the Wikidata entity')
        dialog.geometry('780x410')
        dialog.transient(self.root)
        ttk.Label(dialog, text=f'Select the Wikidata entry that represents {watch.get("title", "this watch")}.').pack(
            anchor='w', padx=10, pady=(10, 3))
        ttk.Label(dialog, text='Sourced relationships will be saved to the media graph; selecting a series keeps it as one watched item.').pack(
            anchor='w', padx=10, pady=(0, 6))
        matches = ttk.Treeview(dialog, columns=('title', 'description', 'qid'),
                               show='headings', selectmode='browse')
        for column, heading, width in (('title', 'Entity', 230), ('description', 'Description', 440),
                                       ('qid', 'Wikidata ID', 90)):
            matches.heading(column, text=heading)
            matches.column(column, width=width, anchor='w')
        matches.pack(fill='both', expand=True, padx=10, pady=5)
        for index, item in enumerate(results):
            matches.insert('', 'end', iid=str(index), values=(
                item.get('title', ''), item.get('description', ''), item.get('qid', '')))

        def associate(_event=None):
            selected_item = matches.selection()
            if not selected_item:
                return
            candidate = results[int(selected_item[0])]
            dialog.destroy()
            data_root = self.data_root
            watch_id = int(watch['id'])
            entity_id = watch.get('media_entity_id')

            def action():
                db = connect(data_root)
                try:
                    current = db.execute('SELECT * FROM watched_items WHERE id=?',
                                         (watch_id,)).fetchone()
                    linked_id = entity_id or (int(current['media_entity_id']) if current and current['media_entity_id'] else None)
                    imported = import_wikidata_entity(db, candidate['qid'], watch_entity_id=linked_id)
                    if current and linked_id and int(imported.get('root_entity_id') or 0) == int(linked_id):
                        _apply_wikidata_watch_policy(db, dict(current), imported)
                    imported['graph_diff'] = capture_graph_snapshot(db, imported['root_entity_id'])
                    db.commit()
                    return imported
                finally:
                    db.close()

            self._worker(f'Importing Wikidata relationships for {watch.get("title", "watch")}',
                         action, self.show_wikidata_relationships)

        matches.bind('<Double-1>', associate)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=(4, 10))
        ttk.Button(buttons, text='Use selected', command=associate).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)
        self._done(f'{len(results)} Wikidata candidate(s)')

    def show_wikidata_relationships(self, result):
        relationships = result.get('relationships') or []
        dialog = tk.Toplevel(self.root)
        dialog.title(f'Related media for {result.get("title", "selection")}')
        dialog.geometry('760x430')
        dialog.transient(self.root)
        diff = result.get('graph_diff') or {}
        change_count = len(diff.get('changes') or [])
        if diff.get('baseline'):
            graph_status = 'Continuity baseline saved.'
        elif diff.get('unchanged'):
            graph_status = 'No graph changes since the previous discovery.'
        else:
            graph_status = f'{change_count} graph change(s) recorded.'
        unresolved = int(result.get('unresolved_entities') or 0)
        message = (f"Saved {len(relationships)} sourced relationship(s) across "
                   f"{result.get('related_entities', 0)} related item(s). {graph_status}")
        if unresolved:
            message += f' {unresolved} related entity label(s) could not be fetched and were queued for review.'
        ttk.Label(dialog, text=message, wraplength=730).pack(anchor='w', padx=10, pady=(10, 4))
        ttk.Label(dialog, text='Provider relationships are stored with their Wikidata IDs and evidence.').pack(
            anchor='w', padx=10, pady=(0, 6))
        tree = ttk.Treeview(dialog, columns=('relation', 'title', 'type', 'position'),
                            show='headings', selectmode='browse')
        for column, heading, width in (('relation', 'Relationship', 180), ('title', 'Related entity', 320),
                                       ('type', 'Type', 120), ('position', 'Position', 90)):
            tree.heading(column, text=heading)
            tree.column(column, width=width, anchor='w')
        tree.pack(fill='both', expand=True, padx=10, pady=5)
        for index, item in enumerate(relationships):
            tree.insert('', 'end', iid=str(index), values=(
                item.get('relationship_type', '').replace('_', ' ').title(),
                item.get('target_title', ''), item.get('target_type', '').title(),
                item.get('series_position') or ''))
        ttk.Button(dialog, text='Close', command=dialog.destroy).pack(anchor='e', padx=10, pady=(4, 10))
        self._done(f'Saved {len(relationships)} Wikidata relationship(s)')

    def open_rights_evidence(self):
        try:
            ensure_rights_evidence(self.rights_evidence_path)
            if hasattr(os, 'startfile'):
                os.startfile(str(self.rights_evidence_path))
            else:
                webbrowser.open(self.rights_evidence_path.resolve().as_uri())
        except Exception as exc:
            self.logger.exception('Could not open local rights evidence file')
            messagebox.showerror('Rights evidence', f'Could not open {self.rights_evidence_path}: {exc}')

    def edit_watch_rights(self):
        selected = self.watched_view.selection()
        if not selected:
            messagebox.showinfo('Rights evidence', 'Select a comic series or issue watch first.')
            return
        row = next((item for item in self.watch_rows if str(item.get('id')) == selected[0]), None)
        if not row or row.get('kind') not in ('comic', 'comic_issue', 'manga', 'webcomic', 'webtoon'):
            messagebox.showinfo('Rights evidence', 'Select a comic, manga, webcomic, webtoon, or comic issue watch.')
            return
        try:
            document = load_rights_evidence(self.rights_evidence_path)
            rights = effective_work_rights(document, row, self.db)
        except Exception as exc:
            self.logger.exception('Could not read local rights evidence')
            messagebox.showerror('Rights evidence', str(exc))
            return

        dialog = tk.Toplevel(self.root)
        dialog.title('Edit local rights evidence')
        dialog.geometry('620x310')
        dialog.transient(self.root)
        ttk.Label(dialog, text=f"Local approval for {row.get('title', 'selected comic')}",
                  font=('', 10, 'bold')).pack(anchor='w', padx=12, pady=(12, 4))
        ttk.Label(dialog, text=('The YES/NO value and evidence are saved in rights-evidence.json. '
                                'A YES authorizes downloads for this comic watch. Issue watches inherit '
                                'the series record unless you edit the issue itself.'),
                  wraplength=585, justify='left').pack(anchor='w', padx=12, pady=(0, 10))
        form = ttk.Frame(dialog)
        form.pack(fill='x', padx=12)
        ttk.Label(form, text='Approval').grid(row=0, column=0, sticky='w', pady=4)
        choice_map = {
            'No - not reviewed': 'not_reviewed',
            'Yes - public domain': 'public_domain',
            'Yes - permission granted': 'permission_granted',
            'No - restricted': 'restricted',
        }
        reverse_map = {value: label for label, value in choice_map.items()}
        choice = tk.StringVar(value=reverse_map.get((rights or {}).get('status'), 'No - not reviewed'))
        ttk.Combobox(form, textvariable=choice, state='readonly', width=32,
                     values=list(choice_map)).grid(row=0, column=1, sticky='w', pady=4)
        ttk.Label(form, text='Evidence note').grid(row=1, column=0, sticky='w', pady=4)
        evidence = tk.StringVar(value=(rights or {}).get('evidence', ''))
        ttk.Entry(form, textvariable=evidence, width=68).grid(row=1, column=1, sticky='ew', pady=4)
        ttk.Label(form, text='Evidence URL').grid(row=2, column=0, sticky='w', pady=4)
        reference = tk.StringVar(value=(rights or {}).get('reference', ''))
        ttk.Entry(form, textvariable=reference, width=68).grid(row=2, column=1, sticky='ew', pady=4)
        form.columnconfigure(1, weight=1)

        def save():
            try:
                set_work_rights(self.rights_evidence_path, row, choice_map[choice.get()],
                                evidence.get(), reference.get())
            except Exception as exc:
                messagebox.showerror('Rights evidence', str(exc), parent=dialog)
                return
            dialog.destroy()
            self.refresh_watches()
            self._done('Local rights evidence saved')

        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=12, pady=(16, 12))
        ttk.Button(buttons, text='Save', command=save).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)

    def _schedule_watch_scan(self):
        self._run_watch_scan(force=False, automatic=True)
        self.root.after(60_000, self._schedule_watch_scan)

    def _run_watch_scan(self, force=False, automatic=False):
        if self.watch_scan_running:
            return
        self.watch_scan_running = True
        data_root = self.data_root
        folder = self.library_folder.get().strip() or str(self.data_root / 'downloads')
        source_options = self._watch_sources()
        if self.comicvine_api_key:
            source_options['comicvine_api_key'] = self.comicvine_api_key
        source_options['tmdb_api_key'] = self.tmdb_api_key
        source_options['youtube_api_key'] = self.youtube_api_key
        source_options['igdb_client_id'] = self.igdb_client_id
        source_options['igdb_client_secret'] = self.igdb_client_secret

        def run():
            try:
                db = connect(data_root)
                try:
                    checked = process_due_watches(db, folder, force=force, source_options=source_options)
                    rows = list_watches(db) if checked else None
                    graph_changes = self._consume_graph_changes(db)
                finally:
                    db.close()
                error = None
            except Exception as exc:
                self.logger.error('Watch scan failed: %s\n%s', exc, traceback.format_exc())
                checked, rows, graph_changes, error = 0, None, [], exc

            def finish():
                self.watch_scan_running = False
                if error:
                    if not automatic:
                        self._done('Watch check failed')
                        messagebox.showerror('Media Intake', str(error))
                    return
                if rows is not None:
                    self.refresh_watches()
                graph_summary = self._graph_change_summary(graph_changes)
                if checked:
                    self.refresh_queue()
                    self._done(f'Checked {checked} watched item(s)' +
                               (f' · {graph_summary}' if graph_summary else ''))
                elif graph_summary:
                    self._done(graph_summary)
                elif force:
                    self.refresh_queue()
                    self._done('No watched items are due yet')
            self.root.after(0, finish)

        threading.Thread(target=run, daemon=True).start()

    def refresh_queue(self):
        all_rows = list_items(self.db)
        self.queue_rows = [row for row in all_rows
                           if row.get('state') not in {'downloaded', 'imported'}]
        self.library_rows = self._completed_library_items(all_rows)
        delivery_notifications = []
        for row in self.library_rows:
            try:
                item = json.loads(row.get('metadata') or '{}')
            except (TypeError, ValueError):
                item = {}
            delivery = item.get('kindle_delivery') or {}
            status = delivery.get('status')
            if (status in {'arrived', 'sent_unconfirmed'} and
                    int(row['id']) not in self._seen_kindle_delivery_notifications):
                self._seen_kindle_delivery_notifications.add(int(row['id']))
                title = str(item.get('title') or 'Book')
                if status == 'arrived':
                    delivery_notifications.append(f'Copied to Kindle: {title}')
                else:
                    delivery_notifications.append(
                        f'Email sent for {title}; Kindle arrival is unconfirmed')
        selected = self.queue_view.selection()
        for row in self.queue_view.get_children(): self.queue_view.delete(row)
        for row in self.queue_rows:
            item = json.loads(row['metadata'])
            if item.get('watch_search'):
                message = str(row.get('progress_message') or '')
                if row['state'] == 'upcoming':
                    state = 'Upcoming'
                elif message.startswith('Search complete'):
                    state = 'Watching'
                else:
                    state = 'Searching'
            else:
                state = 'Downloading' if row['state'] == 'downloading' else row['state']
            self.queue_view.insert('', 'end', iid=str(row['id']), values=(
                row['id'], item.get('title', ''), item.get('author', ''), item.get('format', ''),
                state, self._queue_source_label(row), self._queue_progress_label(row),
                row.get('file_path') or ''))
        if selected and self.queue_view.exists(selected[0]):
            self.queue_view.selection_set(selected[0])
        self._render_library_rows()
        if delivery_notifications:
            self.status.set(' | '.join(delivery_notifications[:3]) +
                            (f' (+{len(delivery_notifications) - 3} more)'
                             if len(delivery_notifications) > 3 else ''))
        return delivery_notifications

    @staticmethod
    def _completed_library_items(rows):
        return [row for row in rows
                if row.get('state') in {'downloaded', 'imported'} and row.get('file_path')]

    def _current_kindle_delivery_notification_ids(self):
        notified = set()
        for row in list_items(self.db):
            try:
                item = json.loads(row.get('metadata') or '{}')
            except (TypeError, ValueError):
                continue
            if (item.get('kindle_delivery') or {}).get('status') in {
                    'arrived', 'sent_unconfirmed'}:
                notified.add(int(row['id']))
        return notified

    @staticmethod
    def _kindle_delivery_label(row):
        try:
            item = json.loads(row.get('metadata') or '{}')
        except (TypeError, ValueError):
            item = {}
        delivery = item.get('kindle_delivery') or {}
        status = delivery.get('status')
        if status == 'arrived':
            return 'Copied · ' + str(delivery.get('arrived_at') or '')[:16].replace('T', ' ')
        if status == 'sent_unconfirmed':
            targets = delivery.get('targets') or []
            if targets:
                sent = sum(target.get('status') == 'sent_unconfirmed' for target in targets)
                if sent < len(targets):
                    return f'Email sent to {sent}/{len(targets)} Kindles; some failed'
                return f'Email sent to {sent} Kindle(s); arrival unconfirmed'
            return 'Email sent; arrival unconfirmed'
        if status == 'sending_email':
            return 'Sending email to Kindle…'
        if status == 'sending':
            return 'Converting / copying…'
        if status == 'waiting_for_device':
            return 'Waiting for connected Kindle'
        if status == 'failed':
            return 'Failed: ' + str(delivery.get('error') or 'see logs')[:100]
        return 'Not sent'

    @staticmethod
    def _queue_source_label(row):
        try:
            item = json.loads(row.get('metadata') or '{}')
        except (TypeError, ValueError):
            item = {}
        if item.get('watch_search'):
            message = str(row.get('progress_message') or '')
            if message.startswith('Searching '):
                return message[len('Searching '):].rstrip('… .') or 'Watched sources'
            return 'Watched sources'
        irc = item.get('irc') if isinstance(item.get('irc'), dict) else {}
        if irc:
            if item.get('download_priority_label'):
                return str(item['download_priority_label'])
            name = str(item.get('irc_source_name') or irc.get('source_name') or '').strip()
            endpoint = ' '.join(str(irc.get(key) or '').strip()
                                for key in ('server', 'channel') if irc.get(key))
            detail = ' '.join(value for value in (name, endpoint) if value)
            return f'IRC: {detail}' if detail else 'IRC'
        key = str(item.get('source_key') or '').casefold()
        label = SOURCE_KEY_LABELS.get(key)
        if label:
            return label
        provider = str(item.get('provider') or '').strip()
        if provider:
            return provider
        source = str(item.get('source') or '').strip()
        parsed = urllib.parse.urlsplit(source)
        if parsed.netloc:
            return parsed.netloc.removeprefix('www.')
        if parsed.scheme == 'irc':
            return f"IRC: {parsed.netloc} {parsed.path.lstrip('/')}".strip()
        return source or 'Unknown source'

    @staticmethod
    def _format_bytes(value):
        amount = max(0, int(value or 0))
        for unit in ('B', 'KB', 'MB', 'GB'):
            if amount < 1024 or unit == 'GB':
                return f'{amount:.1f} {unit}' if unit != 'B' else f'{amount} B'
            amount /= 1024

    def _queue_progress_label(self, row):
        try:
            item = json.loads(row.get('metadata') or '{}')
        except (TypeError, ValueError):
            item = {}
        if item.get('watch_search'):
            if row.get('state') == 'upcoming':
                if item.get('publication_status') == 'announced; release date unconfirmed':
                    return 'Awaiting a confirmed release date'
                return f"Release date: {item.get('release_date') or 'not listed'}"
            return row.get('progress_message') or 'Preparing connector search'
        if row.get('state') != 'downloading':
            return ''
        source = self._queue_source_label(row)
        done = row.get('progress_bytes')
        total = row.get('progress_total')
        message = row.get('progress_message') or 'Preparing download'
        if total:
            percent = min(100, int((done or 0) * 100 / total))
            return f'{source} · {percent}% ({self._format_bytes(done)} / {self._format_bytes(total)})'
        if done:
            return f'{source} · {message} · {self._format_bytes(done)} received'
        return f'{source} · {message}'

    def backup_database(self):
        from datetime import date
        from .backup import backup_database
        path = filedialog.asksaveasfilename(
            title='Back up the app database', defaultextension='.sqlite3',
            initialfile=f'media-intake-backup-{date.today().isoformat()}.sqlite3',
            filetypes=[('SQLite database', '*.sqlite3'), ('All files', '*.*')])
        if not path:
            return
        try:
            backup_database(self.db, path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            messagebox.showerror('Back up database', str(exc))
            return
        self._done(f'Database backed up to {path}')
        messagebox.showinfo('Back up database', (
            f'Saved {path}.\n\nIt holds the queue, watch list, settings and follows, not the downloaded files. '
            'To restore, close the app and put it in the data folder as intake.sqlite3. Saved passwords '
            'and API keys only unlock for this Windows user.'))

    def export_watch_list(self):
        from .backup import export_watch_list
        path = filedialog.asksaveasfilename(
            title='Export watch list', defaultextension='.csv', initialfile='watch-list.csv',
            filetypes=[('CSV files', '*.csv')])
        if not path:
            return
        try:
            count = export_watch_list(self.db, path)
        except OSError as exc:
            messagebox.showerror('Export watch list', str(exc))
            return
        self._done(f'Exported {count} watched item(s) to {path}')

    def import_goodreads_list_csv(self):
        path = filedialog.askopenfilename(
            title='Choose Goodreads library export',
            initialdir=str(Path.home() / 'Downloads'),
            filetypes=[('CSV files', '*.csv'), ('All files', '*.*')])
        if not path:
            return
        try:
            export = parse_goodreads_export(path)
        except Exception as exc:
            messagebox.showerror('Goodreads list import', str(exc))
            return

        books = export['books']
        all_choice = 'All books in export'
        shelf_choices = [all_choice, *export.get('shelves', [])]
        dialog = tk.Toplevel(self.root)
        dialog.title('Import Goodreads list')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text=f'{Path(path).name}: {len(books)} books found').grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 10))
        ttk.Label(body, text='Import shelf').grid(row=1, column=0, sticky='w', pady=3)
        shelf_var = tk.StringVar(value=shelf_choices[1] if len(shelf_choices) > 1 else all_choice)
        shelf_box = ttk.Combobox(body, textvariable=shelf_var, values=shelf_choices,
                                 state='readonly', width=38)
        shelf_box.grid(row=1, column=1, sticky='ew', pady=3)
        ttk.Label(body, text='Library divider').grid(row=2, column=0, sticky='w', pady=3)
        default_name = shelf_var.get() if shelf_var.get() != all_choice else Path(path).stem
        divider_var = tk.StringVar(value=default_name)
        divider_entry = ttk.Entry(body, textvariable=divider_var, width=40)
        divider_entry.grid(row=2, column=1, sticky='ew', pady=3)
        ttk.Label(body, text='Automatically email new downloads to').grid(
            row=3, column=0, sticky='w', pady=3)
        targets = get_kindle_email_targets(self.db)
        target_map = {'Do not auto-send': None}
        for index, target in enumerate(targets, start=1):
            email = str(target.get('email') or '').strip()
            if not email:
                continue
            name = str(target.get('name') or f'Kindle {index}').strip()
            label = f'{name} — {email}'
            if label in target_map:
                label = f'{label} ({index})'
            target_map[label] = {'name': name, 'email': email}
        target_var = tk.StringVar(value='Do not auto-send')
        target_box = ttk.Combobox(body, textvariable=target_var,
                                  values=list(target_map), state='readonly', width=38)
        target_box.grid(row=3, column=1, sticky='ew', pady=3)
        preview_var = tk.StringVar()
        preview = ttk.Label(body, textvariable=preview_var)
        preview.grid(row=4, column=0, columnspan=2, sticky='w', pady=(8, 2))

        def chosen_books():
            shelf = shelf_var.get()
            if shelf == all_choice:
                return list(books)
            return [book for book in books if shelf in (book.get('goodreads_shelves') or [])]

        def update_preview(_event=None):
            target = target_map.get(target_var.get())
            message = f'{len(chosen_books())} books will be watched and searched across connected download sources.'
            active_profile = next((profile for profile in self.library_profiles
                                   if str(profile['id']) == str(self.active_profile_id.get())), None)
            if active_profile:
                message += f" They'll be added to the '{active_profile['name']}' profile library."
            if target:
                message += (f" New downloads will be converted to EPUB when supported, then emailed to "
                            f"{target.get('name') or target.get('email')}.")
            preview_var.set(message)

        shelf_box.bind('<<ComboboxSelected>>', update_preview)
        target_box.bind('<<ComboboxSelected>>', update_preview)
        update_preview()
        if not targets:
            ttk.Label(body, text='Add Kindle email destinations in Settings > Kindle to enable automatic email delivery.',
                      wraplength=450).grid(row=5, column=0, columnspan=2, sticky='w', pady=(4, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=6, column=0, columnspan=2, sticky='e', pady=(12, 0))
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right')

        def begin_import():
            selected = chosen_books()
            divider = divider_var.get().strip()
            if not selected:
                messagebox.showerror('Goodreads list import', 'The selected shelf has no books.', parent=dialog)
                return
            if not divider:
                messagebox.showerror('Goodreads list import', 'Enter a name for the Library divider.', parent=dialog)
                return
            target = target_map.get(target_var.get())
            dialog.destroy()
            data_root = self.data_root
            sources = self._watch_sources()
            profile_id = self.active_profile_id.get().strip() or None

            def action():
                db = connect(data_root)
                try:
                    return import_goodreads_list(
                        db, selected, divider, sources, kindle_target=target,
                        profile_id=profile_id)
                finally:
                    db.close()

            def completed(result):
                self.refresh_watches()
                self.refresh_queue()
                self.refresh_library()
                self._done(
                    f"Goodreads list '{divider}' added: {result['added']} new, "
                    f"{result['existing']} already watched; searching {result['scheduled']} books")
                if result['scheduled']:
                    self.root.after(100, self._run_added_watch_scan)

            self._worker(f'Importing Goodreads list “{divider}”…', action, completed)

        ttk.Button(buttons, text='Import list', command=begin_import).pack(side='right', padx=(0, 6))
        dialog.bind('<Return>', lambda _event: begin_import())
        dialog.bind('<Escape>', lambda _event: dialog.destroy())
        dialog.grab_set()
        dialog.wait_window()

    def convert_library_to_epub(self):
        data_root = self.data_root

        def action():
            db = connect(data_root)
            try:
                return convert_library_books_to_epub(db)
            finally:
                db.close()

        def completed(results):
            self.refresh_library()
            converted = sum(result.get('status') == 'converted' for result in results)
            already = sum(result.get('status') == 'already_epub' for result in results)
            failed = [result for result in results if result.get('status') == 'failed']
            message = f'EPUB copies created: {converted}; already EPUB: {already}; could not convert: {len(failed)}.'
            if failed:
                reasons = list(dict.fromkeys(result.get('error') for result in failed if result.get('error')))
                if reasons:
                    message += ' ' + '; '.join(reasons[:2])
            self._done(message)

        self._worker('Converting library books to EPUB…', action, completed)

    def refresh_library(self):
        self.library_rows = self._completed_library_items(list_items(self.db))
        watches = {int(row['id']): dict(row) for row in self.db.execute(
            'SELECT id,kind,title,author,parent_id,metadata,file_path FROM watched_items')}
        preferred_authors = [row['author'] for row in watches.values()
                             if row.get('author') and row['author'].casefold() != 'unknown']
        self.library_display_by_id = {}
        for row in self.library_rows:
            try:
                item = json.loads(row.get('metadata') or '{}')
            except (TypeError, ValueError):
                item = {}
            watch_id = item.get('watch_request_id') or item.get('watch_id')
            watch = watches.get(int(watch_id)) if str(watch_id or '').isdigit() else None
            watch_metadata = {}
            if watch:
                try:
                    watch_metadata = json.loads(watch.get('metadata') or '{}')
                except (TypeError, ValueError):
                    watch_metadata = {}
            parent = watches.get(int(watch['parent_id'])) if watch and watch.get('parent_id') else None
            if parent and parent.get('kind') == 'series':
                try:
                    parent_metadata = json.loads(parent.get('metadata') or '{}')
                except (TypeError, ValueError):
                    parent_metadata = {}
            else:
                parent_metadata = {}

            title = (watch.get('title') if watch and watch.get('title') and
                     not re.fullmatch(r'Q[1-9]\d{0,11}', watch['title'])
                     else item.get('title'))
            author = watch.get('author') if watch else ''
            if not author or author.casefold() == 'unknown':
                author = item.get('author') or ''
            if (not author or str(author).casefold() == 'unknown') and parent:
                parent_author = str(parent.get('author') or '').strip()
                author = (parent_author if parent_author.casefold() != 'unknown' else
                          parent.get('title') if parent.get('kind') == 'author' else author)
            series = (watch_metadata.get('series') or item.get('series') or
                      (parent.get('title') if parent and parent.get('kind') == 'series' else ''))
            series_index = watch_metadata.get('series_index')
            if series_index is None:
                series_index = item.get('series_index')
            if series_index is None:
                series_index = parent_metadata.get('series_index')
            lists = []
            for value in (item.get('goodreads_lists'), watch_metadata.get('goodreads_lists'),
                          item.get('book_lists'), watch_metadata.get('book_lists')):
                if value:
                    lists.extend(value if isinstance(value, list) else [value])
            lists = list(dict.fromkeys(str(value).strip() for value in lists if str(value).strip()))
            profile_ids = (item.get('library_profile_ids') or
                           watch_metadata.get('library_profile_ids') or [])
            if not isinstance(profile_ids, list):
                profile_ids = [profile_ids]
            profile_ids = list(dict.fromkeys(str(value).strip() for value in profile_ids
                                              if str(value).strip()))
            author = standardize_author(author, preferred_authors)
            title = standardize_title(title)
            try:
                category = str(item.get('category') or '').casefold()
                kind = str(item.get('kind') or (watch.get('kind') if watch else '') or '').casefold()
                is_book = (kind in {'book', 'lightnovel'} or
                           category in {'books', 'light novel', 'light novels'} or
                           (not kind and Path(str(row.get('file_path') or '')).suffix.casefold()
                            in {'.epub', '.mobi', '.azw3', '.txt', '.pdf'}))
            except (TypeError, ValueError):
                is_book = False
            file_format = Path(str(row.get('file_path') or '')).suffix.lstrip('.').upper()
            conversion = item.get('epub_conversion') or {}
            if conversion.get('status') == 'failed':
                file_format = f'{file_format} · EPUB failed'
            self.library_display_by_id[str(row['id'])] = {
                'title': title or 'Untitled', 'author': author,
                'series': str(series or '').strip(),
                'series_index': series_index, 'lists': lists,
                'profile_ids': profile_ids,
                'is_book': is_book, 'format': file_format,
            }
        self._render_library_navigation()
        self._render_library_rows()

    def _render_library_navigation(self):
        tree = getattr(self, 'library_author_tree', None)
        if not tree:
            return
        selected_filter = dict(self.library_filter)
        for node in tree.get_children(''):
            tree.delete(node)
        self.library_tree_filters = {}
        books = [(row, self.library_display_by_id.get(str(row['id']), {}))
                 for row in self.library_rows]
        book_entries = [(row, display) for row, display in books if display.get('is_book')]
        next_node = 0

        def node_id(prefix):
            nonlocal next_node
            next_node += 1
            return f'{prefix}:{next_node}'

        all_id = 'library:all'
        tree.insert('', 'end', iid=all_id, text=f'All library items ({len(books)})', open=True)
        self.library_tree_filters[all_id] = {'kind': 'all'}
        profiled_entries = [(row, display) for row, display in books
                            if display.get('profile_ids')]
        if self.library_profiles:
            profiles_id = 'library:profiles'
            tree.insert('', 'end', iid=profiles_id,
                        text=f'Profiles ({len(self.library_profiles)})', open=True)
            self.library_tree_filters[profiles_id] = {'kind': 'all_profiles'}
            for profile in self.library_profiles:
                profile_id = str(profile['id'])
                entries = [(row, display) for row, display in profiled_entries
                           if profile_id in display.get('profile_ids', [])]
                node = f'library:profile:{profile_id}'
                tree.insert(profiles_id, 'end', iid=node,
                            text=f"{profile['name']} ({len(entries)})", open=False)
                self.library_tree_filters[node] = {
                    'kind': 'profile', 'profile_id': profile_id}
                profile_books = [(row, display) for row, display in entries
                                 if display.get('is_book')]
                profile_other = [(row, display) for row, display in entries
                                 if not display.get('is_book')]
                if profile_books:
                    child = node_id('profile-books')
                    tree.insert(node, 'end', iid=child,
                                text=f'Books ({len(profile_books)})')
                    self.library_tree_filters[child] = {
                        'kind': 'profile_books', 'profile_id': profile_id}
                if profile_other:
                    child = node_id('profile-other')
                    tree.insert(node, 'end', iid=child,
                                text=f'Other downloads ({len(profile_other)})')
                    self.library_tree_filters[child] = {
                        'kind': 'profile_other', 'profile_id': profile_id}
        authors_id = 'library:authors'
        tree.insert('', 'end', iid=authors_id, text=f'Authors ({len(book_entries)})', open=True)
        self.library_tree_filters[authors_id] = {'kind': 'books'}
        grouped = {}
        for row, display in book_entries:
            author = display.get('author') or 'Unknown'
            grouped.setdefault(author, []).append((row, display))
        for author in sorted(grouped, key=str.casefold):
            author_entries = grouped[author]
            author_id = node_id('author')
            tree.insert(authors_id, 'end', iid=author_id,
                        text=f'{author} ({len(author_entries)})', open=False)
            self.library_tree_filters[author_id] = {'kind': 'author', 'author': author}
            series_groups = {}
            standalone = []
            for row, display in author_entries:
                series = display.get('series') or ''
                if series:
                    series_groups.setdefault(series, []).append((row, display))
                else:
                    standalone.append((row, display))
            for series in sorted(series_groups, key=str.casefold):
                series_entries = series_groups[series]
                series_id = node_id('series')
                tree.insert(author_id, 'end', iid=series_id,
                            text=f'{series} ({len(series_entries)})')
                self.library_tree_filters[series_id] = {
                    'kind': 'series', 'author': author, 'series': series}
            if standalone:
                standalone_id = node_id('standalone')
                tree.insert(author_id, 'end', iid=standalone_id,
                            text=f'Standalone titles ({len(standalone)})')
                self.library_tree_filters[standalone_id] = {
                    'kind': 'standalone', 'author': author}

        collection_groups = {}
        for row, display in book_entries:
            for label in display.get('lists') or []:
                collection_groups.setdefault(label, []).append((row, display))
        if collection_groups:
            lists_id = 'library:goodreads'
            list_count = sum(len(entries) for entries in collection_groups.values())
            tree.insert('', 'end', iid=lists_id,
                        text=f'Book lists ({list_count})', open=True)
            self.library_tree_filters[lists_id] = {'kind': 'goodreads_lists'}
            for label in sorted(collection_groups, key=str.casefold):
                list_id = node_id('list')
                entries = collection_groups[label]
                tree.insert(lists_id, 'end', iid=list_id,
                            text=f'{label} ({len(entries)})')
                self.library_tree_filters[list_id] = {'kind': 'goodreads_list', 'label': label}

        other_entries = [(row, display) for row, display in books if not display.get('is_book')]
        if other_entries:
            other_id = 'library:other'
            tree.insert('', 'end', iid=other_id,
                        text=f'Other downloads ({len(other_entries)})')
            self.library_tree_filters[other_id] = {'kind': 'other'}

        for node, value in self.library_tree_filters.items():
            if value == selected_filter:
                tree.selection_set(node)
                tree.see(node)
                return
        tree.selection_set(all_id)
        self.library_filter = {'kind': 'all'}

    def _on_library_folder_select(self, _event=None):
        selected = self.library_author_tree.selection()
        if selected:
            selected_filter = dict(self.library_tree_filters.get(selected[0], {'kind': 'all'}))
            if selected_filter.get('profile_id'):
                profile = next((item for item in self.library_profiles
                                if str(item['id']) == selected_filter['profile_id']), None)
                self.library_profile_view.set(profile['name'] if profile else 'ALL')
            elif selected_filter.get('kind') in {'all', 'all_profiles'}:
                self.library_profile_view.set('ALL')
            self.library_filter = selected_filter
            self._render_library_rows()

    def _render_library_rows(self):
        if not getattr(self, 'library_view', None):
            return
        selected = self.library_view.selection()
        for row in self.library_view.get_children():
            self.library_view.delete(row)
        filters = self.library_filter
        visible = []
        selected_profile = next((item for item in self.library_profiles
                                 if item['name'] == self.library_profile_view.get()), None)
        selected_profile_id = str(selected_profile['id']) if selected_profile else None
        for row in self.library_rows:
            display = self.library_display_by_id.get(str(row['id']), {})
            if (selected_profile_id and
                    selected_profile_id not in display.get('profile_ids', [])):
                continue
            kind = filters.get('kind', 'all')
            if (kind == 'profile' and
                    filters.get('profile_id') not in display.get('profile_ids', [])):
                continue
            if (kind in {'profile_books', 'profile_other'} and
                    filters.get('profile_id') not in display.get('profile_ids', [])):
                continue
            if kind in {'books', 'author', 'series', 'standalone', 'goodreads_lists', 'goodreads_list',
                        'profile_books'}:
                if not display.get('is_book'):
                    continue
            if kind == 'profile_other' and display.get('is_book'):
                continue
            if kind in {'author', 'series', 'standalone'} and display.get('author') != filters.get('author'):
                continue
            if kind == 'series' and display.get('series') != filters.get('series'):
                continue
            if kind == 'standalone' and display.get('series'):
                continue
            if kind == 'goodreads_list' and filters.get('label') not in display.get('lists', []):
                continue
            if kind == 'other' and display.get('is_book'):
                continue
            visible.append((row, display))

        def series_position(value):
            try:
                return (0, float(value))
            except (TypeError, ValueError):
                return (1, str(value or '').casefold())

        visible.sort(key=lambda pair: (
            str(pair[1].get('author') or 'Unknown').casefold(),
            not bool(pair[1].get('series')),
            str(pair[1].get('series') or '').casefold(),
            series_position(pair[1].get('series_index')),
            str(pair[1].get('title') or '').casefold()))
        for row, display in visible:
            try:
                item = json.loads(row.get('metadata') or '{}')
            except (TypeError, ValueError):
                item = {}
            library_path = self._library_display_path(row, display)
            self.library_view.insert('', 'end', iid=str(row['id']), values=(
                display.get('title') or '', display.get('author') or '',
                display.get('series') or '', display.get('series_index') or '',
                display.get('format') or str(item.get('format') or '').upper(),
                self._queue_source_label(row), row.get('state', ''),
                self._kindle_delivery_label(row), str(library_path or '')))
        if selected and self.library_view.exists(selected[0]):
            self.library_view.selection_set(selected[0])

    def _library_display_path(self, row, display=None):
        path = Path(row['file_path']) if row.get('file_path') else None
        if not path:
            return None
        profile = next((item for item in self.library_profiles
                        if item['name'] == self.library_profile_view.get()), None)
        display = display or self.library_display_by_id.get(str(row['id']), {})
        if profile and str(profile['id']) in display.get('profile_ids', []):
            candidate = Path(profile.get('library_folder') or '') / path.name
            if candidate.is_file():
                return candidate
        return path

    def _selected_library_path(self):
        selected = self.library_view.selection()
        if not selected:
            return None
        row = next((item for item in self.library_rows
                    if str(item.get('id')) == selected[0]), None)
        return self._library_display_path(row) if row else None

    def open_selected_library_file(self):
        path = self._selected_library_path()
        if not path:
            return
        if not path.is_file():
            messagebox.showerror('Media Intake', f'Library file is missing:\n{path}')
            return
        try:
            if hasattr(os, 'startfile'):
                os.startfile(str(path))
            else:
                webbrowser.open(path.resolve().as_uri())
        except Exception as exc:
            self.logger.exception('Could not open library file %s', path)
            messagebox.showerror('Media Intake', f'Could not open the file: {exc}\n{path}')

    def open_selected_library_folder(self):
        path = self._selected_library_path()
        if not path:
            return
        folder = path.parent
        try:
            if hasattr(os, 'startfile'):
                os.startfile(str(folder))
            else:
                webbrowser.open(folder.resolve().as_uri())
        except Exception as exc:
            self.logger.exception('Could not open library folder %s', folder)
            messagebox.showerror('Media Intake', f'Could not open the folder: {exc}\n{folder}')

    def send_selected_to_kindle(self):
        path = self._selected_library_path()
        if not path:
            messagebox.showinfo('Kindle delivery', 'Select a downloaded book in Library first.')
            return
        selected = self.library_view.selection()
        item_id = int(selected[0])
        data_root = self.data_root

        def action():
            db = connect(data_root)
            try:
                return deliver_to_kindle(db, item_id, data_root, force=True)
            finally:
                db.close()

        def completed(row):
            self.refresh_queue()
            delivery = self._row_kindle_delivery(row)
            if delivery.get('status') == 'arrived':
                self._done(f"Copied to Kindle: {self._row_title(row)}")
            elif delivery.get('status') == 'sent_unconfirmed':
                targets = delivery.get('targets') or []
                sent = sum(target.get('status') == 'sent_unconfirmed' for target in targets)
                total = len(targets)
                count_text = f' to {sent}/{total} Kindles' if total else ''
                self._done(f"Email sent{count_text} for {self._row_title(row)}; check Kindle library for delivery")
            elif delivery.get('status') == 'waiting_for_device':
                self._done('Waiting for a connected Kindle. The book will be retried automatically if auto-send is on.')
            else:
                self._done(f"Kindle send failed: {delivery.get('error') or 'see Library status'}")

        self._worker('Sending book to Kindle…', action, completed)

    @staticmethod
    def _row_kindle_delivery(row):
        try:
            return (json.loads(row.get('metadata') or '{}').get('kindle_delivery') or {})
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _row_title(row):
        try:
            return str(json.loads(row.get('metadata') or '{}').get('title') or 'Book')
        except (TypeError, ValueError):
            return 'Book'

    def _retry_waiting_kindle_sends(self):
        if (self._kindle_retry_running or not self.kindle_auto_send.get() or
                self.kindle_delivery_method.get().upper() != 'USB'):
            return
        pending_ids = pending_kindle_delivery_ids(self.db)
        if not pending_ids:
            return
        if not find_kindle_documents_folder(self.kindle_device_path.get().strip()):
            return
        self._kindle_retry_running = True
        data_root = self.data_root

        def run():
            db = None
            try:
                db = connect(data_root)
                retry_pending_kindle_deliveries(db, data_root)
            except Exception:
                self.logger.exception('Retrying pending Kindle transfers failed')
            finally:
                if db:
                    db.close()
                try:
                    self.root.after(0, self._finish_kindle_retry)
                except tk.TclError:
                    pass

        threading.Thread(target=run, daemon=True).start()

    def _finish_kindle_retry(self):
        self._kindle_retry_running = False
        self.refresh_queue()

    def download_queued(self, item_ids=None):
        if self._download_batch_active:
            if item_ids:
                self.root.after(1000, lambda: self.download_queued(item_ids))
            return
        if item_ids is None:
            item_ids = []
            for row in list_items(self.db, state='queued'):
                try:
                    metadata = json.loads(row.get('metadata') or '{}')
                except (TypeError, ValueError):
                    metadata = {}
                if not metadata.get('watch_search'):
                    item_ids.append(int(row['id']))
        if not item_ids:
            messagebox.showinfo('Media Intake', 'There are no queued files to download.')
            return
        from .core import download_settings
        parallel = download_settings(self.db)['parallel']
        self._download_batch_active = True
        self.download_batch_button.configure(state='disabled')
        data_root = self.data_root
        destination = self.library_folder.get().strip() or None

        def action():
            outcomes = []
            with ThreadPoolExecutor(max_workers=min(parallel, len(item_ids))) as pool:
                futures = {pool.submit(self._download_one, data_root, destination, item_id): item_id
                           for item_id in item_ids}
                for future in as_completed(futures):
                    outcomes.append((futures[future], future.result()))
            return outcomes

        def finished(outcomes):
            self._download_batch_active = False
            self.download_batch_button.configure(state='normal')
            self.refresh_queue()
            succeeded_rows = [result for _item_id, result in outcomes
                              if isinstance(result, dict)]
            succeeded = len(succeeded_rows)
            failed = len(outcomes) - succeeded
            arrived = sum(self._row_kindle_delivery(row).get('status') == 'arrived'
                          for row in succeeded_rows)
            emailed = sum(self._row_kindle_delivery(row).get('status') == 'sent_unconfirmed'
                          for row in succeeded_rows)
            waiting = [row for row in list_items(self.db, state='needs_shortlink')
                       if int(row['id']) in {item_id for item_id, _result in outcomes}]
            if waiting:
                self._open_shortlink_for_item(int(waiting[0]['id']))
            self._done(f'Downloaded {succeeded} queued file(s)' +
                       (f'; {len(waiting)} shortlink(s) need the helper' if waiting else '') +
                       (f'; copied {arrived} to Kindle' if arrived else '') +
                       (f'; emailed {emailed} to Kindle (arrival unconfirmed)' if emailed else '') +
                       (f'; {failed} failed' if failed else ''))

        def failed(error):
            self._download_batch_active = False
            self.download_batch_button.configure(state='normal')
            messagebox.showerror('Media Intake', f'{error}\n\nDetails saved to {self.log_file}')

        self._worker(f'Downloading {len(item_ids)} queued file(s), up to {parallel} at once…',
                     action, finished, on_error=failed)

    @staticmethod
    def _download_one(data_root, destination, item_id):
        db = connect(data_root)
        try:
            return download(db, item_id, data_root, destination=destination)
        except Exception as exc:
            logging.getLogger('media_intake').exception(
                'Queued download %s failed during parallel transfer', item_id)
            return str(exc)
        finally:
            db.close()

    def _poll_queue(self):
        try:
            self.refresh_queue()
            self._retry_waiting_kindle_sends()
            active = any(row.get('state') in {'downloading', 'searching'}
                         for row in self.queue_rows)
            self._autostart_downloads()
            delay = 500 if active else 2500
            self.root.after(delay, self._poll_queue)
        except tk.TclError:
            return

    def download_selected(self):
        if self._download_batch_active:
            return
        selected = self.queue_view.selection()
        if not selected: return
        item_id = int(selected[0])
        queued = next((row for row in self.queue_rows if int(row['id']) == item_id), None)
        if queued:
            try:
                metadata = json.loads(queued.get('metadata') or '{}')
            except (TypeError, ValueError):
                metadata = {}
            if metadata.get('watch_search'):
                messagebox.showinfo('Media Intake',
                                    'This book is waiting for its automatic source search. It will start downloading when a matching file is found.')
                return
        destination = self.library_folder.get().strip() or None
        data_root = self.data_root
        from . import transfer
        from .core import ShortlinkPending
        if transfer.is_active(('item', item_id)):
            self._done('That download is already running')
            return
        def action():
            db = connect(data_root)
            try: return download(db, item_id, data_root, destination=destination)
            except (transfer.TransferPaused, transfer.TransferCancelled) as exc:
                return {'_stopped': str(exc)}
            except ShortlinkPending as exc:
                return {'_shortlink': str(exc)}
            finally: db.close()

        def completed(result):
            self.refresh_queue()
            if result.get('_stopped'):
                self._done(result['_stopped'])
                return
            if result.get('_shortlink'):
                self._open_shortlink_for_item(item_id)
                return
            delivery = self._row_kindle_delivery(result)
            if delivery.get('status') == 'arrived':
                self._done(f"Copied to Kindle: {self._row_title(result)}")
            elif delivery.get('status') == 'sent_unconfirmed':
                self._done(f"Email sent for {self._row_title(result)}; check Kindle library for delivery")
            else:
                self._done('Downloaded')

        self._worker('Downloading…', action, completed)


def main(data_root=None, window_title=None):
    root = tk.Tk()
    MediaIntakeApp(root, data_root)
    if window_title:
        root.title(window_title)
    root.mainloop()


def _default_data_root(is_frozen=None, executable=None, home=None):
    """Keep a frozen portable build's queue, browser profile, and logs beside its EXE."""
    if is_frozen is None:
        is_frozen = getattr(sys, 'frozen', False)
    if is_frozen:
        return Path(executable or sys.executable).resolve().parent / 'MediaIntakeData'
    return Path(home or Path.home()) / 'MediaIntakeData'


if __name__ == '__main__':
    main()

