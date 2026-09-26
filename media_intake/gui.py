"""Small Tkinter desktop front end for the media intake queue."""
import json
import os
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .core import connect, download, enqueue, list_items
from .comics import mangadex_id_from_url, search_anilist, search_mangadex, search_taddy
from .fmhy import candidates
from .serial import discover_feed, enqueue_serial
from .sources import resolve_detail, search_forum_threads, search_openlibrary, search_site
from .watched import (add_from_goodreads, add_watch, get_setting, list_watches,
                      fetch_goodreads, process_due_watches, remove_watch,
                      search_goodreads, set_setting)


class MediaIntakeApp:
    def __init__(self, root, data_root=None):
        self.root = root
        self.root.title('Media Intake')
        self.root.geometry('1050x680')
        self.data_root = Path(data_root or (Path.home() / 'MediaIntakeData'))
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
        self._build()
        self.refresh_queue()
        self.refresh_watches()
        self.root.after(1500, self._schedule_watch_scan)

    def _build(self):
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='Data folder:').pack(side='left')
        ttk.Entry(toolbar, textvariable=tk.StringVar(value=str(self.data_root)), width=58, state='readonly').pack(side='left', padx=6)
        ttk.Button(toolbar, text='Choose…', command=self.choose_root).pack(side='left')
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
                             'Snahp forum threads']).pack(side='left')
        ttk.Entry(controls, textvariable=self.site_base, width=28).pack(side='left', padx=6)
        ttk.Button(controls, text='Search', command=self.search).pack(side='left')
        ttk.Button(controls, text='Load saved forum results', command=self.load_saved_forum_results).pack(side='left', padx=6)
        ttk.Label(parent, text='Optional site base URL (for LibGen or Anna’s Archive)').pack(anchor='w', pady=(4, 6))

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
        ttk.Button(buttons, text='Open selected thread', command=self.open_selected_thread).pack(side='left', padx=6)

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
        ttk.Button(first, text='AniList', command=self.lookup_anilist).pack(side='left', padx=(0, 4))
        ttk.Button(first, text='MangaDex', command=self.lookup_mangadex).pack(side='left', padx=(0, 4))
        ttk.Button(first, text='Taddy', command=self.lookup_taddy).pack(side='left')

        second = ttk.Frame(add)
        second.pack(fill='x', padx=6, pady=(3, 6))
        for label, variable, width in (('Title', self.watch_title, 25), ('Author', self.watch_author, 18),
                                       ('Release date (YYYY-MM-DD)', self.watch_release_date, 15)):
            ttk.Label(second, text=label).pack(side='left')
            ttk.Entry(second, textvariable=variable, width=width).pack(side='left', padx=(4, 10))
        ttk.Label(second, text='Type').pack(side='left')
        ttk.Combobox(second, textvariable=self.watch_kind, state='readonly', width=12,
                     values=['Auto', 'Book', 'Series', 'Author', 'Manga', 'Comic', 'Webcomic', 'Webtoon',
                             'Web serial', 'Fanfiction']).pack(side='left', padx=6)
        ttk.Button(second, text='Add to watched', command=self.add_watched).pack(side='left')

        feed = ttk.Frame(add)
        feed.pack(fill='x', padx=6, pady=(0, 6))
        ttk.Label(feed, text='Chapter feed (RSS/Atom/SSS)').pack(side='left')
        ttk.Entry(feed, textvariable=self.watch_feed_url, width=66).pack(side='left', padx=6, fill='x', expand=True)
        ttk.Label(feed, text='HTML selector').pack(side='left', padx=(6, 2))
        ttk.Entry(feed, textvariable=self.watch_selector, width=18).pack(side='left')
        ttk.Label(add, text='Web serial feeds save new chapters as EPUB. Comics accept RSS/Atom with an image selector or a Taddy SSS feed; Taddy search needs API credentials.').pack(anchor='w', padx=8, pady=(0, 5))

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
        ttk.Label(options, text='Comic watches search these connectors for CBZ files; web serials search for ebook files and check their chapter feeds. FMHY is discovery-only.').pack(anchor='w', padx=8, pady=(0, 5))

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
        ttk.Button(buttons, text='Remove selected', command=self.delete_watched).pack(side='left')

    def _busy(self, message):
        self.status.set(message)

    def _done(self, message):
        self.status.set(message)

    def _worker(self, message, action, callback=None):
        self._busy(message)
        def run():
            try:
                result = action()
            except Exception as exc:
                self.root.after(0, lambda error=exc: (self._done('Error'), messagebox.showerror('Media Intake', str(error))))
                return
            if callback:
                self.root.after(0, lambda: callback(result))
            else:
                self.root.after(0, lambda: self._done('Ready'))
        threading.Thread(target=run, daemon=True).start()

    def search(self):
        query = self.query.get().strip()
        if not query:
            messagebox.showinfo('Media Intake', 'Enter a title, author, or search term.')
            return
        source = self.source.get()
        base = self.site_base.get().strip() or None
        def action():
            if source == 'Open Library':
                return search_openlibrary(query)
            if source == 'FMHY directory':
                return [{'title': f"Search {site['name']}", 'author': site['section'],
                         'format': 'link', 'source': site['search_url'], 'url': site['search_url'],
                         'fmhy': True} for site in candidates(query, limit=30)]
            if source == 'Snahp forum threads':
                return search_forum_threads(query)
            site = 'libgen' if source == 'LibGen' else 'annas'
            found = search_site(site, query, base=base)
            resolved = []
            for result in found[:20]:
                if not result.get('detail_url'): continue
                try:
                    resolved.extend(resolve_detail(site, result['detail_url'], result.get('title', query), result.get('author', 'Unknown'), result.get('format')))
                except Exception:
                    continue
            return resolved
        self._worker(f'Searching {source}…', action, self.show_results)

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
        def action():
            db = connect(self.data_root)
            try: return enqueue(db, item)
            finally: db.close()
        self._worker('Adding to queue…', action, lambda _result: self._done('Queued'))

    def activate_result(self):
        selected = self.results_view.selection()
        if selected and self.results[int(selected[0])].get('forum_thread'):
            self.open_selected_thread()
        else:
            self.queue_result()

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
            'openlibrary': self.watch_openlibrary.get(),
            'libgen': self.watch_libgen.get(),
            'libgen_base': self.watch_libgen_base.get().strip(),
            'annas': self.watch_annas.get(),
            'annas_base': self.watch_annas_base.get().strip(),
            'opds_feeds': opds_feeds,
            'catalogs': catalogs,
            'irc_configs': irc_configs,
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
        self._worker(f'Searching Goodreads for {query}…',
                     lambda: search_goodreads(query), self.show_goodreads_matches)

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

    def show_comic_matches(self, provider, results):
        if not results:
            self._done(f'No {provider} matches found')
            messagebox.showinfo(f'{provider} lookup', f'No manga matches found in {provider}.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f'Choose a {provider} manga')
        dialog.geometry('760x430')
        dialog.transient(self.root)
        ttk.Label(dialog, text=f'Choose the {provider} entry to associate its metadata with this watch.').pack(anchor='w', padx=10, pady=(10, 5))
        matches = ttk.Treeview(dialog, columns=('title', 'author', 'kind', 'release'), show='headings', selectmode='browse')
        for column, heading, width in (('title', 'Title', 330), ('author', 'Creator', 190),
                                       ('kind', 'Type', 90), ('release', 'Start date', 95)):
            matches.heading(column, text=heading)
            matches.column(column, width=width, anchor='w')
        matches.pack(fill='both', expand=True, padx=10, pady=5)
        for index, item in enumerate(results):
            matches.insert('', 'end', iid=str(index), values=(
                item.get('title', ''), item.get('author', ''), item.get('kind', '').title(),
                item.get('release_date') or ''))

        def associate(_event=None):
            selected = matches.selection()
            if not selected:
                return
            item = results[int(selected[0])]
            dialog.destroy()
            self.associate_comic_metadata(item)

        matches.bind('<Double-1>', associate)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=(5, 10))
        ttk.Button(buttons, text='Use selected', command=associate).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)
        self._done(f'{len(results)} {provider} match(es)')

    def associate_comic_metadata(self, item):
        self.watch_comic_profile = {
            'provider': item.get('provider'),
            'anilist_id': item.get('anilist_id'),
            'mangadex_id': item.get('mangadex_id'),
            'taddy_id': item.get('taddy_id'),
            'sss_url': item.get('sss_url'),
            'catalog_metadata': {key: value for key, value in item.items()
                                 if key not in ('provider', 'kind', 'title', 'author',
                                                'release_date', 'source_url',
                                                'anilist_id', 'mangadex_id', 'taddy_id',
                                                'sss_url', 'catalog_metadata')},
        }
        self.watch_url.set(item.get('source_url', ''))
        if item.get('sss_url'):
            self.watch_feed_url.set(item['sss_url'])
        self.watch_title.set(item.get('title', ''))
        self.watch_author.set('' if item.get('author') == 'Unknown' else item.get('author', ''))
        self.watch_release_date.set(item.get('release_date') or '')
        self.watch_kind.set(item.get('kind', 'manga').title())
        if not item.get('mangadex_id') and not item.get('sss_url'):
            self._done(f"{item.get('provider', 'Catalog')} metadata filled in; add a chapter feed to enable automatic downloads")
        else:
            source_name = 'Taddy SSS' if item.get('sss_url') else 'MangaDex'
            self._done(f"{item.get('provider', 'Catalog')} metadata filled in; {source_name} chapter tracking is ready")

    def show_goodreads_matches(self, results):
        if not results:
            self._done('No Goodreads matches found')
            messagebox.showinfo('Goodreads lookup', 'No books, series, or authors matched that search.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Choose a Goodreads entry')
        dialog.geometry('650x400')
        dialog.transient(self.root)
        ttk.Label(dialog, text='Choose the Goodreads entry whose metadata you want to use.').pack(anchor='w', padx=10, pady=(10, 5))
        matches = ttk.Treeview(dialog, columns=('kind', 'title'), show='headings', selectmode='browse')
        matches.heading('kind', text='Type')
        matches.heading('title', text='Goodreads title')
        matches.column('kind', width=95, anchor='w')
        matches.column('title', width=510, anchor='w')
        matches.pack(fill='both', expand=True, padx=10, pady=5)
        for index, item in enumerate(results):
            matches.insert('', 'end', iid=str(index), values=(item['kind'].title(), item['title']))

        def associate(_event=None):
            selected = matches.selection()
            if not selected:
                return
            result = results[int(selected[0])]
            dialog.destroy()
            self._worker('Loading Goodreads metadata…',
                         lambda: fetch_goodreads(result['source_url']), self.associate_goodreads_metadata)

        matches.bind('<Double-1>', associate)
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=(5, 10))
        ttk.Button(buttons, text='Use selected', command=associate).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right', padx=6)
        self._done(f'{len(results)} Goodreads match(es)')

    def associate_goodreads_metadata(self, metadata):
        self.watch_comic_profile = {}
        self.watch_url.set(metadata.get('source_url', ''))
        self.watch_title.set(metadata.get('title', ''))
        self.watch_author.set('' if metadata.get('author') == 'Unknown' else metadata.get('author', ''))
        self.watch_release_date.set(metadata.get('release_date') or '')
        self.watch_kind.set(metadata.get('kind', 'book').title())
        self._done('Goodreads metadata filled in; review and add it to Watched')

    def add_watched(self):
        url = self.watch_url.get().strip()
        title = self.watch_title.get().strip()
        author = self.watch_author.get().strip() or 'Unknown'
        release = self.watch_release_date.get().strip() or None
        kind = self.watch_kind.get().casefold()
        kind = {'web serial': 'webfiction'}.get(kind, kind)
        kind = None if kind == 'auto' else kind
        if kind is None and mangadex_id_from_url(self.watch_url.get().strip()):
            kind = 'manga'
        sources = self._watch_sources()
        feed_url = self.watch_feed_url.get().strip()
        selector = self.watch_selector.get().strip() or 'article,main'
        comic_profile = dict(self.watch_comic_profile)

        def action():
            db = connect(self.data_root)
            try:
                watch_title = title
                watch_url = url
                watch_author = author
                watch_kind = kind or 'book'
                if watch_url and watch_kind in ('book', 'series', 'author'):
                    try:
                        return add_from_goodreads(db, watch_url, kind, watch_title or None, watch_author, release, sources)
                    except Exception:
                        if not watch_title:
                            raise
                metadata = {'sources': sources}
                if watch_kind in ('series', 'author'):
                    metadata['known_books'] = []
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
                    if not comic_profile.get('mangadex_id') and not comic_profile.get('feed_url'):
                        raise ValueError('Choose a MangaDex/Taddy title or enter an RSS/Atom/SSS chapter feed URL')
                    metadata['comic'] = comic_profile
                if watch_kind in ('webfiction', 'fanfiction'):
                    serial_feed = feed_url or watch_url
                    if not serial_feed:
                        raise ValueError('Enter a public RSS/Atom chapter feed URL for this web serial')
                    discovered = discover_feed(serial_feed, watch_kind, limit=200)
                    watch_title = watch_title or discovered.get('title') or ''
                    if watch_author == 'Unknown':
                        watch_author = discovered.get('author') or 'Unknown'
                    metadata['serial'] = {'feed_url': serial_feed, 'selector': selector,
                                          'known_chapters': [], 'known_files': []}
                    watch_url = watch_url or serial_feed
                watch_id = add_watch(db, watch_kind, watch_title, watch_author, watch_url or feed_url or None,
                                     release, metadata)
                return watch_id, None
            finally:
                db.close()

        def added(result):
            self.watch_url.set('')
            self.watch_title.set('')
            self.watch_author.set('')
            self.watch_release_date.set('')
            self.watch_feed_url.set('')
            self.watch_comic_profile = {}
            self.refresh_watches()
            if result[1] and result[1].get('kind') == 'series':
                self._done('Series added; upcoming books will be watched as Goodreads lists them')
            elif result[1] and result[1].get('kind') == 'author':
                self._done('Author added; new Goodreads books will be watched')
            elif kind in ('comic', 'manga', 'webcomic', 'webtoon'):
                self._done('Comic watch added; new chapters will be checked daily')
            elif kind in ('webfiction', 'fanfiction'):
                self._done('Web serial watch added; new chapters will be checked daily and saved as EPUBs')
            elif result[1] and result[1].get('release_date'):
                self._done('Watched; release date saved')
            else:
                self._done('Added to watched')

        self._worker('Reading Goodreads…' if url and kind in ('book', 'series', 'author') else 'Adding watch…', action, added)

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


if __name__ == '__main__':
    main()

