"""Webcomics library and evidence-preserving chapter browser for the existing GUI."""
import json
import tkinter as tk
from tkinter import ttk, simpledialog, filedialog, messagebox
from pathlib import Path
from ..core import connect
from .storage import library, set_override
from .watcher import add_series


class WebcomicsPanel:
    def __init__(self, app, parent):
        self.app = app
        self.rows = {}
        self.url, self.title = tk.StringVar(), tk.StringVar()
        self.adapter = tk.StringVar(value='auto')
        self.archive_existing = tk.BooleanVar(value=False)
        self.auto_archive = tk.BooleanVar(value=True)
        self.recipe = None
        form = ttk.LabelFrame(parent, text='Add Webcomic', padding=8)
        form.pack(fill='x')
        ttk.Label(form, text='Public comic or feed URL').grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=self.url, width=70).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Label(form, text='Title (optional)').grid(row=1, column=0, sticky='w')
        ttk.Entry(form, textvariable=self.title).grid(row=1, column=1, sticky='ew', padx=6)
        ttk.Combobox(form, textvariable=self.adapter, state='readonly', width=14,
                     values=('auto', 'feed', 'static', 'wordpress', 'comicfury', 'webtoon', 'xkcd')).grid(row=0, column=2)
        ttk.Button(form, text='Resolve and watch', command=self.add).grid(row=0, column=3, padx=6)
        ttk.Button(form, text='Load recipe…', command=self.load_recipe).grid(row=1, column=2, padx=6)
        ttk.Checkbutton(form, text='Archive existing entries', variable=self.archive_existing).grid(row=2, column=1, sticky='w')
        ttk.Checkbutton(form, text='Automatically archive future entries', variable=self.auto_archive).grid(row=2, column=2, columnspan=2, sticky='w')
        form.columnconfigure(1, weight=1)
        ttk.Label(parent, text='Initial discovery saves the available index. Backlogs archive in batches; feeds may expose only recent releases.').pack(anchor='w', pady=6)
        actions = ttk.Frame(parent)
        actions.pack(fill='x')
        ttk.Button(actions, text='Refresh library', command=self.refresh).pack(side='left')
        ttk.Button(actions, text='Check watches now', command=lambda: app._run_watch_scan(force=True)).pack(side='left', padx=6)
        ttk.Button(actions, text='Series detail', command=self.detail).pack(side='left')
        ttk.Button(actions, text='Watch settings', command=self.settings).pack(side='left', padx=6)
        ttk.Button(actions, text='Catalog matches', command=self.catalog_matches).pack(side='left')
        columns = ('title', 'creator', 'source', 'entries', 'archived', 'latest', 'checked', 'health')
        self.view = ttk.Treeview(parent, columns=columns, show='headings', height=13)
        widths = (190, 130, 85, 55, 65, 150, 140, 160)
        for name, width in zip(columns, widths):
            self.view.heading(name, text=name.title())
            self.view.column(name, width=width, minwidth=45)
        self.view.pack(fill='both', expand=True, pady=8)
        self.view.bind('<Double-1>', lambda _event: self.detail())
        self.refresh()

    def load_recipe(self):
        path = filedialog.askopenfilename(title='Webcomic recipe JSON', filetypes=[('JSON', '*.json')])
        if path:
            try:
                from .extraction import Recipe
                self.recipe = Recipe.from_dict(json.loads(Path(path).read_text(encoding='utf-8'))).__dict__
                self.app._done('Loaded recipe ' + self.recipe['id'])
            except (ValueError, OSError) as exc:
                messagebox.showerror('Webcomic recipe', str(exc))

    def add(self):
        url, title = self.url.get().strip(), self.title.get().strip()
        if not url:
            messagebox.showinfo('Add Webcomic', 'Enter a comic, archive, individual page, or feed URL.')
            return
        config = {'auto_archive': self.auto_archive.get(),
                  'rights_evidence_path': str(self.app.rights_evidence_path)}
        profile = self.app.active_profile_id.get().strip()
        if profile:
            config['library_profile_ids'] = [profile]
        if self.adapter.get() != 'auto':
            config['adapter'] = self.adapter.get()
        if self.recipe:
            config.update({'recipe': self.recipe, 'adapter': self.recipe['id']})
        archive_existing = self.archive_existing.get()
        data_root = self.app.data_root
        def action():
            db = connect(data_root)
            try:
                return add_series(db, url, title=title or None, config=config,
                                  archive_existing=archive_existing)
            finally:
                db.close()
        def added(_watch_id):
            self.refresh()
            self.app.refresh_watches()
            self.app._done('Webcomic resolved and watched')
        self.app._worker('Resolving Webcomic…', action, added)

    def refresh(self):
        selected = self.view.selection()
        self.rows = {str(row['id']): row for row in library(self.app.db)}
        self.view.delete(*self.view.get_children())
        for key, row in self.rows.items():
            health = row['health'] + (': ' + row['last_error'][:70] if row.get('last_error') else '')
            self.view.insert('', 'end', iid=key, values=(row['title'], row['author'], row['adapter'],
                row['entries'], row['archived'], row['latest_release'] or '', row['last_checked_at'] or '', health))
        if selected and selected[0] in self.rows:
            self.view.selection_set(selected[0])

    def selected(self):
        values = self.view.selection()
        return self.rows.get(values[0]) if values else None

    def catalog_matches(self):
        from . import metadata_bridge as bridge
        source = self.selected()
        if not source:
            return
        app, series_id = self.app, source['series_id']
        credentials = {'comicvine_api_key': getattr(app, 'comicvine_api_key', ''),
                       'taddy_user_id': getattr(app, 'taddy_user_id', ''), 'taddy_api_key': getattr(app, 'taddy_api_key', '')}
        dialog = tk.Toplevel(app.root)
        dialog.title(f"Catalog matches: {source['title']}")
        dialog.geometry('900x480')
        catalogs = bridge.available_catalogs(credentials)
        note = tk.StringVar(value='Catalogs: ' + ', '.join(bridge.CATALOGS[key]['label'] for key in catalogs) +
                            '. Accepted matches enrich the series on every check; the source\'s own title wins '
                            'unless you lock one.')
        ttk.Label(dialog, textvariable=note, wraplength=860, justify='left').pack(anchor='w', padx=12, pady=(12, 6))
        view = ttk.Treeview(dialog, columns=('catalog', 'creators', 'score', 'status'), show='tree headings',
                            selectmode='browse')
        view.heading('#0', text='Catalog title')
        view.column('#0', width=320)
        for column, label, width in (('catalog', 'Catalog', 110), ('creators', 'Creators', 220),
                                     ('score', 'Match', 60), ('status', 'Status', 90)):
            view.heading(column, text=label)
            view.column(column, width=width, anchor='w')
        view.pack(fill='both', expand=True, padx=12)
        def render():
            view.delete(*view.get_children())
            for match in bridge.matches(app.db, series_id):
                candidate = match['candidate']
                view.insert('', 'end', iid=str(match['id']), text=candidate.get('title'), values=(
                    bridge.CATALOGS.get(match['provider'], {}).get('label', match['provider']),
                    ', '.join(item['name'] for item in candidate.get('creators') or []),
                    f"{round(match['score'] * 100)}%", match['status']))
        def search():
            data_root = app.data_root
            def action():
                from ..core import connect
                db = connect(data_root)
                try:
                    return bridge.find_matches(db, series_id, catalogs, credentials)
                finally:
                    db.close()
            def done(outcome):
                render()
                if outcome[1]:
                    note.set('Some catalogs failed: ' + '; '.join(outcome[1]))
            app._worker('Searching catalogs…', action, done)
        def set_status(status):
            selected = view.selection()
            if selected:
                bridge.set_match_status(app.db, int(selected[0]), status)
                bridge.refresh_series(app.db, series_id)
                render()
                self.refresh()
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=8)
        ttk.Button(bar, text='Search catalogs', command=search).pack(side='left')
        ttk.Button(bar, text='Accept match', command=lambda: set_status('accepted')).pack(side='left', padx=6)
        ttk.Button(bar, text='Reject match', command=lambda: set_status('rejected')).pack(side='left')
        lock_row = ttk.Frame(dialog)
        lock_row.pack(fill='x', padx=12, pady=(0, 12))
        field = tk.StringVar(value='title')
        value = tk.StringVar()
        ttk.Label(lock_row, text='Lock field').pack(side='left')
        ttk.Combobox(lock_row, textvariable=field, values=('title', 'description', 'cover_url', 'status'),
                     state='readonly', width=12).pack(side='left', padx=6)
        ttk.Entry(lock_row, textvariable=value, width=50).pack(side='left')
        def lock():
            if value.get().strip():
                bridge.lock_field(app.db, series_id, field.get(), value.get().strip())
                bridge.refresh_series(app.db, series_id)
                self.refresh()
        def unlock():
            bridge.unlock_field(app.db, series_id, field.get())
            bridge.refresh_series(app.db, series_id)
            self.refresh()
        ttk.Button(lock_row, text='Lock', command=lock).pack(side='left', padx=6)
        ttk.Button(lock_row, text='Unlock', command=unlock).pack(side='left')
        render()

    def settings(self):
        source = self.selected()
        if not source:
            return
        dialog = tk.Toplevel(self.app.root)
        dialog.title('Webcomic watch settings')
        config = json.loads(source['config'])
        hours = tk.StringVar(value=str(config.get('poll_hours', 24)))
        auto = tk.BooleanVar(value=config.get('auto_archive', True))
        ttk.Label(dialog, text='Poll interval (hours, 1–168)').pack(padx=12, pady=8)
        ttk.Entry(dialog, textvariable=hours).pack(padx=12)
        ttk.Checkbutton(dialog, text='Archive new entries automatically', variable=auto).pack(padx=12, pady=8)
        def save():
            try:
                value = float(hours.get())
                if not 1 <= value <= 168:
                    raise ValueError('Choose 1 to 168 hours')
                config.update({'poll_hours': value, 'auto_archive': auto.get()})
                self.app.db.execute('UPDATE webcomic_sources SET config=? WHERE id=?', (json.dumps(config), source['id']))
                self.app.db.commit()
                dialog.destroy()
            except ValueError as exc:
                messagebox.showerror('Watch settings', str(exc))
        ttk.Button(dialog, text='Save', command=save).pack(pady=8)

    def detail(self):
        source = self.selected()
        if not source:
            return
        dialog = tk.Toplevel(self.app.root)
        dialog.title(source['title'] + ' — Webcomic')
        dialog.geometry('1050x700')
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill='both', expand=True)
        browser = ttk.Frame(notebook, padding=8)
        notebook.add(browser, text='Chapters and entries')
        tree = ttk.Treeview(browser, columns=('state', 'type', 'date'), selectmode='browse')
        tree.heading('#0', text='Creator organization / entry / image')
        for key in ('state', 'type', 'date'):
            tree.heading(key, text=key.title())
            tree.column(key, width=120)
        tree.pack(fill='both', expand=True)
        chapters = {}
        for row in self.app.db.execute('SELECT * FROM webcomic_chapters WHERE source_id=?', (source['id'],)):
            chapter = json.loads(row['metadata'])
            iid = 'chapter:' + row['chapter_key']
            chapters[row['chapter_key']] = iid
            tree.insert('', 'end', iid=iid, text=chapter['title'], values=('', chapter['organization'], f"{chapter['confidence']:.0%}"))
        entries = {}
        offset = [0]
        def more():
            for row in self.app.db.execute('SELECT * FROM webcomic_entries WHERE source_id=? ORDER BY ordinal,id LIMIT 250 OFFSET ?',
                                           (source['id'], offset[0])):
                entry = json.loads(row['metadata'])
                iid = 'entry:' + str(row['id'])
                entries[iid] = row['entry_key']
                tree.insert(chapters.get(row['chapter_key'], ''), 'end', iid=iid,
                            text=entry['title'], values=(row['state'], entry['content_type'], entry['published_at'] or ''))
                for asset in entry['assets']:
                    tree.insert(iid, 'end', text=f"Image {asset['ordinal']}: {asset['source_url']}",
                                values=('Archived' if asset.get('checksum') else 'Available', asset.get('media_type') or '', ''))
            offset[0] += 250
        more()
        bar = ttk.Frame(browser)
        bar.pack(fill='x', pady=8)
        ttk.Button(bar, text='Load next 250 entries', command=more).pack(side='left')
        def correct():
            selected = tree.selection()
            key = entries.get(selected[0]) if selected else None
            if key is None:
                return
            value = simpledialog.askstring('Manual chapter', 'Chapter / arc name (blank leaves this entry ungrouped):', parent=dialog)
            if value is not None:
                set_override(self.app.db, source['id'], key, 'chapter', value)
                self.app._done('Chapter override saved')
                dialog.destroy()
                self.detail()
        ttk.Button(bar, text='Correct selected chapter…', command=correct).pack(side='left', padx=8)
        series = self.app.db.execute('SELECT metadata FROM webcomic_series WHERE id=?', (source['series_id'],)).fetchone()
        pages = [('Metadata and provenance', json.loads(series[0])), ('Source and watch state', source),
                 ('Update history', [dict(row) for row in self.app.db.execute('SELECT * FROM webcomic_events WHERE source_id=? ORDER BY id DESC LIMIT 200', (source['id'],))])]
        for title, value in pages:
            frame = ttk.Frame(notebook)
            notebook.add(frame, text=title)
            text = tk.Text(frame, wrap='word')
            text.insert('1.0', json.dumps(value, ensure_ascii=False, indent=2))
            text.configure(state='disabled')
            text.pack(fill='both', expand=True)
