"""Small Tkinter desktop front end for the media intake queue."""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import traceback
import tkinter as tk
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from logging.handlers import RotatingFileHandler

from .core import connect, download, enqueue, list_items
from .comics import (list_comicvine_issues, mangadex_id_from_url, search_anilist,
                     search_comicvine, search_mangadex, search_taddy)
from .comic_sources import search_getcomics, search_public_domain_comic_mirror
from .fmhy import candidates
from .media_catalogs import (search_lnori, search_mangaupdates, search_novelarchive,
                             search_ranobedb)
from .rights_evidence import (effective_work_rights, ensure_rights_evidence,
                              load_rights_evidence, set_work_rights)
from .serial import discover_feed, enqueue_serial
from .sources import (resolve_detail, search_forum_threads, search_openlibrary,
                      search_openlibrary_metadata, search_site)
from .watched import (add_from_goodreads, add_from_goodreads_metadata, add_watch, get_setting, list_watches,
                      fetch_goodreads, parse_goodreads_html, parse_goodreads_search_html,
                      process_due_watches, remove_watch,
                      search_book_metadata, set_setting)
from .media_graph import capture_graph_snapshot
from .wikidata import (expand_watched_book_series, import_wikidata_entity,
                       search_wikidata)


class MediaIntakeApp:
    def __init__(self, root, data_root=None):
        self.root = root
        self.root.title('Media Intake')
        self.root.geometry('1050x680')
        self.data_root = Path(data_root or _default_data_root())
        self.rights_evidence_path = ensure_rights_evidence(self.data_root / 'rights-evidence.json')
        self._configure_logging()
        self.db = connect(self.data_root)
        self.default_irc_config = Path(__file__).resolve().parent.parent / 'examples' / 'irc-search.json'
        irc_default = str(self.default_irc_config) if self.default_irc_config.is_file() else ''
        self.library_folder = tk.StringVar(value=get_setting(self.db, 'library_folder', str(self.data_root / 'downloads')))
        self.results = []
        self.queue_rows = []
        self.status = tk.StringVar(value='Ready')
        self.query = tk.StringVar()
        self.source = tk.StringVar(value='Open Library')
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
        self.watch_selector = tk.StringVar(value='article,main')
        self.watch_comic_profile = {}
        self.watch_lightnovel_profile = {}
        self.watch_comic_mirror_base = tk.StringVar(value=get_setting(self.db, 'watch_comic_mirror_base', ''))
        self.comicvine_api_key = os.environ.get('COMICVINE_API_KEY', '').strip()
        self.watch_goodreads_profile = {}
        self.watch_openlibrary = tk.BooleanVar(value=get_setting(self.db, 'watch_openlibrary', '1') == '1')
        self.watch_libgen = tk.BooleanVar(value=get_setting(self.db, 'watch_libgen', '1') == '1')
        self.watch_annas = tk.BooleanVar(value=get_setting(self.db, 'watch_annas', '1') == '1')
        self.watch_libgen_base = tk.StringVar(value=get_setting(self.db, 'watch_libgen_base', ''))
        self.watch_annas_base = tk.StringVar(value=get_setting(self.db, 'watch_annas_base', ''))
        self.watch_opds_feeds = tk.StringVar(value=get_setting(self.db, 'watch_opds_feeds', ''))
        self.watch_catalogs = tk.StringVar(value=get_setting(self.db, 'watch_catalogs', ''))
        self.watch_irc_configs = tk.StringVar(value=get_setting(self.db, 'watch_irc_configs', irc_default))
        self.watch_rows = []
        self.watch_scan_running = False
        self._saved_watch_sources = None
        self._forum_download_watch = False
        self._forum_download_folders = []
        self._forum_download_baseline = {}
        self._forum_download_stability = {}
        self._embedded_browser_process = None
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
        self._build()
        self.refresh_queue()
        self.refresh_watches()
        self.root.after(1500, self._schedule_watch_scan)

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

    def _build(self):
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='Data folder:').pack(side='left')
        ttk.Entry(toolbar, textvariable=tk.StringVar(value=str(self.data_root)), width=58, state='readonly').pack(side='left', padx=6)
        ttk.Button(toolbar, text='Choose…', command=self.choose_root).pack(side='left')
        ttk.Button(toolbar, text='Open logs', command=self.open_logs).pack(side='left', padx=6)
        ttk.Label(toolbar, textvariable=self.status).pack(side='right')

        tabs = ttk.Notebook(self.root)
        tabs.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        search_tab = ttk.Frame(tabs, padding=8)
        queue_tab = ttk.Frame(tabs, padding=8)
        watched_tab = ttk.Frame(tabs, padding=8)
        tabs.add(search_tab, text='Search and add')
        tabs.add(queue_tab, text='Queue')
        tabs.add(watched_tab, text='Watched')
        self._build_search(search_tab)
        self._build_queue(queue_tab)
        self._build_watched(watched_tab)

    def choose_root(self):
        chosen = filedialog.askdirectory(initialdir=str(self.data_root), title='Choose Media Intake data folder')
        if not chosen: return
        self.data_root = Path(chosen)
        self.rights_evidence_path = ensure_rights_evidence(self.data_root / 'rights-evidence.json')
        self._configure_logging()
        self.db.close()
        self.db = connect(self.data_root)
        self._saved_watch_sources = None
        self.library_folder.set(get_setting(self.db, 'library_folder', str(self.data_root / 'downloads')))
        self.watch_openlibrary.set(get_setting(self.db, 'watch_openlibrary', '1') == '1')
        self.watch_libgen.set(get_setting(self.db, 'watch_libgen', '1') == '1')
        self.watch_annas.set(get_setting(self.db, 'watch_annas', '1') == '1')
        self.watch_libgen_base.set(get_setting(self.db, 'watch_libgen_base', ''))
        self.watch_annas_base.set(get_setting(self.db, 'watch_annas_base', ''))
        self.watch_opds_feeds.set(get_setting(self.db, 'watch_opds_feeds', ''))
        self.watch_catalogs.set(get_setting(self.db, 'watch_catalogs', ''))
        self.watch_comic_mirror_base.set(get_setting(self.db, 'watch_comic_mirror_base', ''))
        irc_default = str(self.default_irc_config) if self.default_irc_config.is_file() else ''
        self.watch_irc_configs.set(get_setting(self.db, 'watch_irc_configs', irc_default))
        self.refresh_queue()
        self.refresh_watches()

    def _build_search(self, parent):
        controls = ttk.Frame(parent)
        controls.pack(fill='x')
        ttk.Label(controls, text='Search').pack(side='left')
        entry = ttk.Entry(controls, textvariable=self.query, width=42)
        entry.pack(side='left', padx=6)
        entry.bind('<Return>', lambda _event: self.search())
        ttk.Combobox(controls, textvariable=self.source, state='readonly', width=20,
                     values=['Open Library', 'LibGen', "Anna's Archive", 'FMHY directory',
                             'Snahp forum threads', 'NovelArchive', 'LNORI', 'RanobeDB',
                             'MangaUpdates', 'GetComics', 'Public-domain comic mirror']).pack(side='left')
        ttk.Entry(controls, textvariable=self.site_base, width=28).pack(side='left', padx=6)
        ttk.Button(controls, text='Search', command=self.search).pack(side='left')
        ttk.Button(controls, text='Snahp browser helper', command=self.open_forum_helper).pack(side='left', padx=6)
        ttk.Label(parent, text='Optional site base URL (LibGen, Anna’s Archive, or a separate public-domain comic mirror)').pack(anchor='w', pady=(4, 6))

        columns = ('title', 'author', 'format', 'source')
        self.results_view = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        for col, width in (('title', 330), ('author', 220), ('format', 80), ('source', 330)):
            self.results_view.heading(col, text=col.title())
            self.results_view.column(col, width=width, anchor='w')
        self.results_view.pack(fill='both', expand=True)
        self.results_view.bind('<Double-1>', lambda _event: self.activate_result())
        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Queue selected result', command=self.queue_result).pack(side='left')
        ttk.Button(buttons, text='Open selected link', command=self.open_selected_link).pack(side='left', padx=6)

        add = ttk.LabelFrame(parent, text='Add a direct file')
        add.pack(fill='x', pady=(12, 0))
        for label, variable, width in (('Title', self.title, 22), ('Author', self.author, 20), ('URL', self.url, 46), ('Format', self.format, 8)):
            ttk.Label(add, text=label).pack(side='left', padx=(6, 2))
            ttk.Entry(add, textvariable=variable, width=width).pack(side='left')
        ttk.Button(add, text='Add to queue', command=self.add_direct).pack(side='left', padx=6)

    def _build_queue(self, parent):
        columns = ('id', 'title', 'author', 'format', 'state', 'file')
        self.queue_view = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        for col, width in (('id', 45), ('title', 260), ('author', 180), ('format', 70), ('state', 100), ('file', 360)):
            self.queue_view.heading(col, text=col.title())
            self.queue_view.column(col, width=width, anchor='w')
        self.queue_view.pack(fill='both', expand=True)
        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Refresh', command=self.refresh_queue).pack(side='left')
        ttk.Button(buttons, text='Download selected', command=self.download_selected).pack(side='left', padx=6)

    def _build_watched(self, parent):
        destination = ttk.LabelFrame(parent, text='Saved library folder')
        destination.pack(fill='x', pady=(0, 8))
        destination_entry = ttk.Entry(destination, textvariable=self.library_folder, width=96)
        destination_entry.pack(side='left', fill='x', expand=True, padx=6, pady=6)
        destination_entry.bind('<FocusOut>', lambda _event: set_setting(self.db, 'library_folder', self.library_folder.get().strip()))
        ttk.Button(destination, text='Choose folder', command=self.choose_library_folder).pack(side='left', padx=(0, 6))

        add = ttk.LabelFrame(parent, text='Watch a book, series, manga, comic, webtoon, webcomic, or web serial')
        add.pack(fill='x', pady=(0, 8))
        first = ttk.Frame(add)
        first.pack(fill='x', padx=6, pady=(6, 3))
        ttk.Label(first, text='Metadata URL').pack(side='left')
        ttk.Entry(first, textvariable=self.watch_url, width=44).pack(side='left', padx=6, fill='x', expand=True)
        ttk.Button(first, text='Goodreads', command=self.lookup_goodreads).pack(side='left', padx=(0, 4))
        ttk.Button(first, text='Import Goodreads HTML', command=self.load_goodreads_html).pack(side='left', padx=(0, 4))
        ttk.Button(first, text='AniList', command=self.lookup_anilist).pack(side='left', padx=(0, 4))
        ttk.Button(first, text='MangaDex', command=self.lookup_mangadex).pack(side='left', padx=(0, 4))
        ttk.Button(first, text='Taddy', command=self.lookup_taddy).pack(side='left')

        second = ttk.Frame(add)
        second.pack(fill='x', padx=6, pady=(3, 6))
        for label, variable, width in (('Title / search', self.watch_title, 25), ('Author', self.watch_author, 18),
                                       ('Release date (YYYY-MM-DD)', self.watch_release_date, 15)):
            ttk.Label(second, text=label).pack(side='left')
            entry = ttk.Entry(second, textvariable=variable, width=width)
            entry.pack(side='left', padx=(4, 10))
            if variable is self.watch_title:
                self.watch_title_entry = entry
                entry.bind('<KeyRelease>', self._schedule_title_suggestions)
                entry.bind('<Down>', self._focus_title_suggestions)
        ttk.Label(second, text='Type').pack(side='left')
        ttk.Combobox(second, textvariable=self.watch_kind, state='readonly', width=12,
                     values=['Auto', 'Book', 'Series', 'Author', 'Light novel', 'Manga', 'Comic', 'Webcomic', 'Webtoon',
                             'Web serial', 'Fanfiction']).pack(side='left', padx=6)
        ttk.Button(second, text='Add to watched', command=self.add_watched).pack(side='left')

        feed = ttk.Frame(add)
        feed.pack(fill='x', padx=6, pady=(0, 6))
        ttk.Label(feed, text='Chapter feed (RSS/Atom/SSS)').pack(side='left')
        ttk.Entry(feed, textvariable=self.watch_feed_url, width=66).pack(side='left', padx=6, fill='x', expand=True)
        ttk.Label(feed, text='HTML selector').pack(side='left', padx=(6, 2))
        ttk.Entry(feed, textvariable=self.watch_selector, width=18).pack(side='left')
        ttk.Label(add, text='Fanfiction: enter a story URL for one EPUB that updates daily (FanFicFare). Web serials use RSS/Atom chapter feeds; comics can use RSS/Atom or Taddy SSS.').pack(anchor='w', padx=8, pady=(0, 5))

        catalogs = ttk.Frame(add)
        catalogs.pack(fill='x', padx=6, pady=(0, 6))
        ttk.Label(catalogs, text='Catalog lookup').pack(side='left')
        ttk.Button(catalogs, text='NovelArchive', command=self.lookup_novelarchive).pack(side='left', padx=(6, 3))
        ttk.Button(catalogs, text='LNORI', command=self.lookup_lnori).pack(side='left', padx=3)
        ttk.Button(catalogs, text='RanobeDB', command=self.lookup_ranobedb).pack(side='left', padx=3)
        ttk.Button(catalogs, text='MangaUpdates English status', command=self.lookup_mangaupdates).pack(side='left', padx=3)
        ttk.Button(catalogs, text='Comic Vine releases', command=self.lookup_comicvine).pack(side='left', padx=3)

        options = ttk.LabelFrame(parent, text='Connected sites to search for watched items')
        options.pack(fill='x', pady=(0, 8))
        line = ttk.Frame(options)
        line.pack(fill='x', padx=6, pady=(5, 2))
        ttk.Checkbutton(line, text='Open Library', variable=self.watch_openlibrary,
                        command=self._watch_sources).pack(side='left')
        ttk.Checkbutton(line, text='LibGen', variable=self.watch_libgen,
                        command=self._watch_sources).pack(side='left', padx=(10, 0))
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
        ttk.Label(line, text='IRC configs (; separated)').pack(side='left')
        irc_configs_entry = ttk.Entry(line, textvariable=self.watch_irc_configs, width=80)
        irc_configs_entry.pack(side='left', padx=5, fill='x', expand=True)
        irc_configs_entry.bind('<FocusOut>', lambda _event: self._watch_sources())
        ttk.Button(line, text='Choose', command=self.choose_irc_configs).pack(side='left')
        line = ttk.Frame(options)
        line.pack(fill='x', padx=6, pady=(2, 5))
        ttk.Label(line, text='Public-domain comic mirror base').pack(side='left')
        mirror_entry = ttk.Entry(line, textvariable=self.watch_comic_mirror_base, width=72)
        mirror_entry.pack(side='left', padx=6, fill='x', expand=True)
        mirror_entry.bind('<FocusOut>', lambda _event: self._watch_sources())
        ttk.Label(options, text='Comic watches search this separate mirror for CBZ/PDF pages marked Public Domain or CC0. GetComics results open in a browser.').pack(anchor='w', padx=8, pady=(0, 5))

        columns = ('kind', 'title', 'author', 'release', 'status', 'checked', 'file')
        self.watched_view = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        for col, width in (('kind', 85), ('title', 190), ('author', 130), ('release', 95),
                           ('status', 170), ('checked', 130), ('file', 180)):
            self.watched_view.heading(col, text={'release': 'Release date', 'checked': 'Last checked'}.get(col, col.title()))
            self.watched_view.column(col, width=width, anchor='w')
        self.watched_view.pack(fill='both', expand=True)

        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Refresh', command=self.refresh_watches).pack(side='left')
        ttk.Button(buttons, text='Check watched now', command=lambda: self._run_watch_scan(force=True)).pack(side='left', padx=6)
        ttk.Button(buttons, text='Discover related', command=self.discover_watched_relationships).pack(side='left', padx=6)
        ttk.Button(buttons, text='Remove selected', command=self.delete_watched).pack(side='left')
        ttk.Button(buttons, text='Edit rights evidence', command=self.edit_watch_rights).pack(side='left', padx=6)
        ttk.Button(buttons, text='Open rights file', command=self.open_rights_evidence).pack(side='left')

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

    def search(self):
        query = self.query.get().strip()
        if not query:
            messagebox.showinfo('Media Intake', 'Enter a title, author, or search term.')
            return
        source = self.source.get()
        if source == 'Snahp forum threads':
            self.open_forum_helper()
            return
        base = self.site_base.get().strip() or None
        def action():
            if source == 'Open Library':
                return search_openlibrary(query)
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

    def show_results(self, results):
        self.results = results
        for row in self.results_view.get_children(): self.results_view.delete(row)
        for index, item in enumerate(results):
            self.results_view.insert('', 'end', iid=str(index), values=(item.get('title', ''), item.get('author', ''), item.get('format', ''), item.get('source', '')))
        self._done(f'{len(results)} result(s)')

    def queue_result(self):
        selected = self.results_view.selection()
        if not selected: return
        item = self.results[int(selected[0])]
        if item.get('forum_thread'):
            messagebox.showinfo('Forum thread result', 'Forum results are thread references and cannot be added to the download queue.')
            return
        if item.get('fmhy'):
            messagebox.showinfo('FMHY directory result', 'This is a discovery link. Open it in a browser and add a resolved direct file URL.')
            return
        if item.get('discovery_only'):
            messagebox.showinfo('Catalog discovery result', 'This source provides a catalog or release-page link, not a verified downloadable file. Open the link to review it.')
            return
        def action():
            db = connect(self.data_root)
            try: return enqueue(db, item)
            finally: db.close()
        self._worker('Adding to queue…', action, lambda _result: self._done('Queued'))

    def activate_result(self):
        selected = self.results_view.selection()
        if selected and (self.results[int(selected[0])].get('forum_thread')
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
            initialdir=str(self.data_root), title='Choose IRC search configurations',
            filetypes=[('JSON configuration', '*.json'), ('All files', '*.*')])
        if chosen:
            self.watch_irc_configs.set(';'.join(chosen))
            self._watch_sources()

    def _watch_sources(self):
        opds_feeds = [value.strip() for value in self.watch_opds_feeds.get().split(';') if value.strip()]
        catalogs = [value.strip() for value in self.watch_catalogs.get().split(';') if value.strip()]
        irc_configs = [value.strip() for value in self.watch_irc_configs.get().split(';') if value.strip()]
        sources = {
            'rights_evidence_path': str(self.rights_evidence_path),
            'openlibrary': self.watch_openlibrary.get(),
            'libgen': self.watch_libgen.get(),
            'libgen_base': self.watch_libgen_base.get().strip(),
            'annas': self.watch_annas.get(),
            'annas_base': self.watch_annas_base.get().strip(),
            'opds_feeds': opds_feeds,
            'catalogs': catalogs,
            'irc_configs': irc_configs,
            'comic_mirror_base': self.watch_comic_mirror_base.get().strip(),
        }
        if sources != self._saved_watch_sources:
            for key, value in sources.items():
                if isinstance(value, bool):
                    stored = '1' if value else '0'
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
        user_id = os.environ.get('TADDY_USER_ID', '').strip()
        api_key = os.environ.get('TADDY_API_KEY', '').strip()
        if not user_id:
            user_id = simpledialog.askstring('Taddy credentials', 'Taddy numeric user ID:') or ''
        if not user_id:
            return
        if not api_key:
            api_key = simpledialog.askstring('Taddy credentials', 'Taddy API key (used for this lookup only):', show='*') or ''
        if not api_key:
            return
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
                    self._done('Metadata filled in; add an authorized RSS/Atom feed to enable chapter tracking')
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

    def show_goodreads_matches(self, results):
        if not results:
            self._done('No book metadata matches found')
            messagebox.showinfo('Book lookup', 'No books, series, or authors matched across the metadata catalogs.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Choose a book metadata match')
        dialog.geometry('650x400')
        dialog.transient(self.root)
        ttk.Label(dialog, text='Click a result to add it to Watched and search the connected book sources.').pack(anchor='w', padx=10, pady=(10, 5))
        matches = ttk.Treeview(dialog, columns=('provider', 'kind', 'title'), show='headings', selectmode='browse')
        matches.heading('provider', text='Source')
        matches.heading('kind', text='Type')
        matches.heading('title', text='Title')
        matches.column('provider', width=125, anchor='w')
        matches.column('kind', width=95, anchor='w')
        matches.column('title', width=385, anchor='w')
        matches.pack(fill='both', expand=True, padx=10, pady=5)
        for index, item in enumerate(results):
            matches.insert('', 'end', iid=str(index), values=(
                item.get('provider') or 'Goodreads', item['kind'].title(), item['title']))

        def associate(_event=None):
            selected = matches.selection()
            if not selected:
                return
            result = results[int(selected[0])]
            dialog.destroy()
            self.watch_goodreads_profile = {}
            self._goodreads_pending_search_query = ''
            self._goodreads_pending_search_url = result['source_url']
            self.watch_url.set(result['source_url'])
            self.watch_title.set(result['title'])
            self.watch_author.set('')
            self.watch_release_date.set('')
            self.watch_kind.set(result['kind'].title())
            self._worker('Loading Goodreads metadata…',
                         lambda: (result if result.get('provider', 'Goodreads').casefold() != 'goodreads'
                                  else fetch_goodreads(result['source_url'])),
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
        kind = self.watch_kind.get().casefold()
        kind = {'web serial': 'webfiction', 'light novel': 'lightnovel'}.get(kind, kind)
        kind = None if kind == 'auto' else kind
        if kind is None and mangadex_id_from_url(self.watch_url.get().strip()):
            kind = 'manga'
        sources = self._watch_sources()
        feed_url = self.watch_feed_url.get().strip()
        selector = self.watch_selector.get().strip() or 'article,main'
        comic_profile = dict(self.watch_comic_profile)
        lightnovel_profile = dict(self.watch_lightnovel_profile)
        goodreads_profile = dict(self.watch_goodreads_profile)

        def action():
            db = connect(self.data_root)
            try:
                watch_title = title
                watch_url = url
                watch_author = author
                watch_kind = kind or 'book'
                goodreads_failed = False
                host = (urllib.parse.urlsplit(watch_url).hostname or '').casefold()
                is_goodreads_url = host == 'goodreads.com' or host.endswith('.goodreads.com')
                if watch_url and watch_kind in ('book', 'series', 'author'):
                    if goodreads_profile and goodreads_profile.get('source_url') == watch_url:
                        return add_from_goodreads_metadata(
                            db, goodreads_profile, watch_url, kind, watch_title or None,
                            watch_author, release, sources)
                    if is_goodreads_url:
                        try:
                            return add_from_goodreads(db, watch_url, kind, watch_title or None, watch_author, release, sources)
                        except Exception as exc:
                            goodreads_failed = True
                            self.logger.warning('Goodreads metadata fetch failed for %s; retaining entered metadata: %s',
                                                watch_url, exc)
                            if not watch_title:
                                raise
                metadata = {'sources': sources}
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
                if watch_kind == 'lightnovel':
                    if not lightnovel_profile.get('ranobedb_id'):
                        match = re.search(r'/series/(\d+)(?:/|$)', urllib.parse.urlsplit(watch_url).path)
                        if match:
                            lightnovel_profile['ranobedb_id'] = match.group(1)
                    if (not lightnovel_profile.get('ranobedb_id')
                            and urllib.parse.urlsplit(watch_url).hostname not in {'lnori.com', 'novelarchive.cc'}):
                        raise ValueError('Look up a RanobeDB series for English release dates, or use an LNORI/NovelArchive series URL')
                    metadata['lightnovel'] = lightnovel_profile
                if watch_kind == 'webfiction':
                    serial_feed = feed_url or watch_url
                    if not serial_feed:
                        raise ValueError('Enter a public RSS/Atom feed or a NovelArchive series URL')
                    is_novelarchive = urllib.parse.urlsplit(serial_feed).hostname == 'novelarchive.cc'
                    if not is_novelarchive:
                        discovered = discover_feed(serial_feed, watch_kind, limit=200)
                        watch_title = watch_title or discovered.get('title') or ''
                        if watch_author == 'Unknown':
                            watch_author = discovered.get('author') or 'Unknown'
                    metadata['serial'] = {'feed_url': serial_feed, 'selector': selector,
                                          'known_chapters': [], 'known_files': []}
                    watch_url = watch_url or serial_feed
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
            self.watch_comic_profile = {}
            self.watch_lightnovel_profile = {}
            self.watch_goodreads_profile = {}
            self.refresh_watches()
            if goodreads_metadata.get('kind') == 'series':
                self._done('Series added; upcoming books will be watched as Goodreads lists them')
            elif goodreads_metadata.get('kind') == 'author':
                self._done('Author added; new Goodreads books will be watched')
            elif kind in ('comic', 'manga', 'webcomic', 'webtoon'):
                self._done('Comic watch added; new chapters will be checked daily')
            elif kind == 'lightnovel':
                self._done('Light-novel series added; English volumes will be searched on their release dates')
            elif kind in ('webfiction', 'fanfiction'):
                self._done('Fanfiction watch added; its EPUB will update with new chapters daily' if kind == 'fanfiction'
                           else 'Web serial watch added; new chapters will be checked daily and saved as EPUBs')
            elif goodreads_metadata.get('release_date'):
                self._done('Watched; release date saved')
            else:
                self._done('Added to watched')
            if auto_scan:
                self.root.after(100, self._run_added_watch_scan)

        self._worker('Reading Goodreads…' if url and kind in ('book', 'series', 'author') else 'Adding watch…', action, added)

    def _run_added_watch_scan(self):
        if self.watch_scan_running:
            self.root.after(500, self._run_added_watch_scan)
            return
        self._run_watch_scan(force=True, automatic=True)

    def refresh_watches(self):
        self.watch_rows = list_watches(self.db)
        for row in self.watched_view.get_children():
            self.watched_view.delete(row)
        state_names = {'upcoming': 'Unreleased', 'watching': 'Watching', 'searching': 'Searching',
                       'downloaded': 'Saved to library', 'failed': 'Retry scheduled', 'series': 'Watching series',
                       'author': 'Watching author'}
        for row in self.watch_rows:
            state = state_names.get(row.get('state'), row.get('state', ''))
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
            if row.get('last_error') and row.get('state') not in ('downloaded', 'upcoming'):
                state += ': ' + row['last_error'][:42]
            self.watched_view.insert('', 'end', iid=str(row['id']), values=(
                row.get('kind', '').title(), row.get('title', ''), row.get('author', ''), row.get('release_date') or '', state,
                row.get('last_checked_at') or '', row.get('file_path') or ''))

    def delete_watched(self):
        selected = self.watched_view.selection()
        if not selected:
            return
        remove_watch(self.db, int(selected[0]))
        self.refresh_watches()

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
        ttk.Label(dialog, text='Sourced relationships will be saved. Explicit book members of a watched book series can be added automatically.').pack(
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
                    current = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                         (watch_id,)).fetchone()
                    linked_id = entity_id or (int(current['media_entity_id']) if current and current['media_entity_id'] else None)
                    imported = import_wikidata_entity(db, candidate['qid'], watch_entity_id=linked_id)
                    imported['expansion'] = expand_watched_book_series(db, watch_id, imported)
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
        expansion = result.get('expansion') or {}
        added_watches = expansion.get('added') or []
        review_items = expansion.get('review') or []
        message = (f"Saved {len(relationships)} sourced relationship(s) across "
                   f"{result.get('related_entities', 0)} related item(s). {graph_status}")
        if added_watches:
            message += f' Added {len(added_watches)} explicit book-series member(s) to Watched.'
        if review_items:
            message += f' Skipped {len(review_items)} uncertain item(s) and queued them for review.'
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
        if added_watches:
            self.refresh_watches()
            self.root.after(100, self._run_added_watch_scan)
        self._done(f'Saved {len(relationships)} Wikidata relationship(s); added {len(added_watches)} series member(s)')

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

        def run():
            try:
                db = connect(data_root)
                try:
                    checked = process_due_watches(db, folder, force=force, source_options=source_options)
                    rows = list_watches(db) if checked else None
                finally:
                    db.close()
                error = None
            except Exception as exc:
                self.logger.error('Watch scan failed: %s\n%s', exc, traceback.format_exc())
                checked, rows, error = 0, None, exc

            def finish():
                self.watch_scan_running = False
                if error:
                    if not automatic:
                        self._done('Watch check failed')
                        messagebox.showerror('Media Intake', str(error))
                    return
                if rows is not None:
                    self.refresh_watches()
                if checked:
                    self.refresh_queue()
                    self._done(f'Checked {checked} watched item(s)')
                elif force:
                    self._done('No watched items are due yet')
            self.root.after(0, finish)

        threading.Thread(target=run, daemon=True).start()

    def refresh_queue(self):
        self.queue_rows = list_items(self.db)
        for row in self.queue_view.get_children(): self.queue_view.delete(row)
        for row in self.queue_rows:
            item = json.loads(row['metadata'])
            self.queue_view.insert('', 'end', iid=str(row['id']), values=(row['id'], item.get('title', ''), item.get('author', ''), item.get('format', ''), row['state'], row.get('file_path') or ''))

    def download_selected(self):
        selected = self.queue_view.selection()
        if not selected: return
        item_id = int(selected[0])
        destination = self.library_folder.get().strip() or None
        data_root = self.data_root
        def action():
            db = connect(data_root)
            try: return download(db, item_id, data_root, destination=destination)
            finally: db.close()
        self._worker('Downloading…', action, lambda _result: (self.refresh_queue(), self._done('Downloaded')))


def main(data_root=None):
    root = tk.Tk()
    MediaIntakeApp(root, data_root)
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

