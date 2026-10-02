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
