"""Webcomics library and evidence-preserving chapter browser for the existing GUI."""
import json
import time
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
        ttk.Button(form, text='Suggest recipe…', command=self.suggest_recipe).grid(row=1, column=3, padx=6)
        ttk.Checkbutton(form, text='Archive existing entries', variable=self.archive_existing).grid(row=2, column=1, sticky='w')
        ttk.Checkbutton(form, text='Automatically archive future entries', variable=self.auto_archive).grid(row=2, column=2, columnspan=2, sticky='w')
        form.columnconfigure(1, weight=1)
        ttk.Label(parent, text='Initial discovery saves the available index. Backlogs archive in batches; feeds may expose only recent releases.').pack(anchor='w', pady=6)
        actions = ttk.Frame(parent)
        actions.pack(fill='x')
        ttk.Button(actions, text='Refresh library', command=self.refresh).pack(side='left')
        ttk.Button(actions, text='Check selected now', command=self.check_selected).pack(side='left', padx=6)
        ttk.Button(actions, text='Series detail', command=self.detail).pack(side='left')
        ttk.Button(actions, text='Watch settings', command=self.settings).pack(side='left', padx=6)
        ttk.Button(actions, text='Catalog matches', command=self.catalog_matches).pack(side='left')
        more = ttk.Frame(parent)
        more.pack(fill='x', pady=(4, 0))
        ttk.Button(more, text='Pause / resume', command=self.toggle_pause).pack(side='left')
        ttk.Button(more, text='Archive backlog now', command=self.archive_backlog).pack(side='left', padx=6)
        ttk.Button(more, text='Retry failed entries', command=self.retry_failed).pack(side='left')
        ttk.Button(more, text='Open site', command=self.open_site).pack(side='left', padx=6)
        ttk.Button(more, text='Audit images', command=self.audit_images).pack(side='left')
        ttk.Button(more, text='Health', command=self.health).pack(side='left', padx=6)
        ttk.Button(more, text='Mirrors', command=self.mirrors).pack(side='left')
        ttk.Button(more, text='Wayback report', command=self.wayback_report).pack(side='left', padx=6)
        ttk.Button(more, text='Export…', command=self.export).pack(side='left')
        ttk.Label(more, text='Filter').pack(side='left', padx=(18, 4))
        self.filter_text = tk.StringVar()
        ttk.Entry(more, textvariable=self.filter_text, width=24).pack(side='left')
        self.filter_status = tk.StringVar(value='All')
        ttk.Combobox(more, textvariable=self.filter_status, state='readonly', width=16,
                     values=('All', 'Needs attention', 'Has waiting entries', 'Paused')).pack(side='left', padx=6)
        self.filter_text.trace_add('write', lambda *_: self.render())
        self.filter_status.trace_add('write', lambda *_: self.render())
        columns = ('title', 'creator', 'source', 'entries', 'archived', 'waiting', 'failed', 'latest', 'checked', 'health')
        self.view = ttk.Treeview(parent, columns=columns, show='headings', height=13)
        widths = (190, 120, 80, 55, 65, 60, 50, 150, 135, 170)
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
        if not self.recipe and self.adapter.get() == 'auto':
            from .infer import recipe_for
            saved = recipe_for(self.app.db, url)  # a recipe accepted earlier for this site
            if saved:
                self.recipe = saved
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

    def suggest_recipe(self):
        from .http import HttpClient
        from .infer import infer_recipe, save_recipe
        url = self.url.get().strip()
        if not url:
            messagebox.showinfo('Suggest recipe', 'Enter the comic site address first.')
            return
        data_root = self.app.data_root
        def action():
            db = connect(data_root)
            try:
                return infer_recipe(HttpClient(db), url)
            finally:
                db.close()
        def show(result):
            if not result:
                messagebox.showinfo('Suggest recipe', 'No convincing archive and comic-image layout was found on this site.')
                return
            dialog = tk.Toplevel(self.app.root)
            dialog.title('Suggested recipe')
            dialog.geometry('820x440')
            recipe = result['recipe']
            lines = [f"Confidence {result['confidence']:.0%}", ''] + [f'• {line}' for line in result['evidence']]
            lines += ['', f"Archive page: {recipe['archive_url']}", f"Entry links: {recipe['archive_selector']}",
                      f"Comic image: {recipe['image_selector']}", '', 'Dry run on sample entries:']
            for sample in result['samples']:
                lines.append(f"  {sample['url']}")
                lines += [f'      image: {image}' for image in sample['images']]
            text = tk.Text(dialog, wrap='word')
            text.insert('1.0', '\n'.join(lines))
            text.configure(state='disabled')
            text.pack(fill='both', expand=True, padx=12, pady=12)
            def accept():
                save_recipe(self.app.db, recipe)
                self.recipe = recipe
                dialog.destroy()
                self.add()
            bar = ttk.Frame(dialog)
            bar.pack(fill='x', padx=12, pady=(0, 12))
            ttk.Button(bar, text='Use this recipe and watch', command=accept).pack(side='left')
            ttk.Button(bar, text='Cancel', command=dialog.destroy).pack(side='right')
        self.app._worker('Studying the site layout…', action, show)

    def refresh(self):
        self.rows = {str(row['id']): row for row in library(self.app.db)}
        counts = {row[0]: (row[1] or 0, row[2] or 0) for row in self.app.db.execute(
            '''SELECT source_id, SUM(state IN ('pending','queued')), SUM(state='failed')
               FROM webcomic_entries WHERE removed=0 GROUP BY source_id''')}
        for row in self.rows.values():
            row['waiting'], row['failed'] = counts.get(row['id'], (0, 0))
            row['paused'] = bool(json.loads(row['config'] or '{}').get('paused'))
        self.render()

    def render(self):
        selected = self.view.selection()
        needle = self.filter_text.get().casefold().strip() if hasattr(self, 'filter_text') else ''
        status = self.filter_status.get() if hasattr(self, 'filter_status') else 'All'
        self.view.delete(*self.view.get_children())
        for key, row in self.rows.items():
            if needle and needle not in f"{row['title']} {row['author']} {row['url']}".casefold():
                continue
            problem = row['health'] != 'OK' or row['failed'] or row.get('last_error')
            if (status == 'Paused' and not row['paused'] or status == 'Needs attention' and not problem
                    or status == 'Has waiting entries' and not row['waiting']):
                continue
            health = ('Paused' if row['paused'] else row['health']) + (
                ': ' + row['last_error'][:70] if row.get('last_error') else '')
            self.view.insert('', 'end', iid=key, values=(row['title'], row['author'], row['adapter'],
                row['entries'], row['archived'], row['waiting'], row['failed'], row['latest_release'] or '',
                row['last_checked_at'] or '', health))
        if selected and self.view.exists(selected[0]):
            self.view.selection_set(selected[0])

    def check_selected(self):
        from .controls import check_source
        source = self.selected()
        if not source:
            return
        data_root, folder = self.app.data_root, self.app.library_folder.get().strip() or None
        def action():
            db = connect(data_root)
            try:
                return check_source(db, source['watch_id'], folder)
            finally:
                db.close()
        def done(watch):
            self.refresh()
            self.app._done(f"Checked {watch['title']}" + (f": {watch['last_error']}" if watch.get('last_error') else ''))
        self.app._worker(f"Checking {source['title']}…", action, done)

    def toggle_pause(self):
        from .controls import set_paused
        source = self.selected()
        if source:
            set_paused(self.app.db, source['id'], not source.get('paused'))
            self.refresh()
            self.app._done(('Paused ' if not source.get('paused') else 'Resumed ') + source['title'])

    def archive_backlog(self):
        from .controls import archive_backlog
        source = self.selected()
        if source:
            count = archive_backlog(self.app.db, source['id'])
            self.refresh()
            self.app._done(f"{count} existing entries will archive once; new releases follow the automatic setting")
            self.check_selected()

    def retry_failed(self):
        from .watcher import retry_failed_entries
        source = self.selected()
        if source:
            count = retry_failed_entries(self.app.db, source['id'])
            self.refresh()
            self.app._done(f'{count} failed entries will be retried')

    def export(self):
        from .export import export_source
        source = self.selected()
        if not source:
            return
        dialog = tk.Toplevel(self.app.root)
        dialog.title(f"Export {source['title']}")
        modes = {'One CBZ per chapter': 'chapter_cbz', 'One CBZ for the series': 'series_cbz',
                 'Image EPUB': 'epub', 'Original image folders': 'folders'}
        choice = tk.StringVar(value='One CBZ per chapter')
        ttk.Label(dialog, text='Format (original image bytes are kept; an OPDS catalog.json is written too)').pack(
            anchor='w', padx=12, pady=(12, 4))
        ttk.Combobox(dialog, textvariable=choice, values=list(modes), state='readonly', width=30).pack(anchor='w', padx=12)
        def run():
            folder = filedialog.askdirectory(title='Export to folder', parent=dialog)
            if not folder:
                return
            dialog.destroy()
            data_root = self.app.data_root
            def action():
                db = connect(data_root)
                try:
                    return export_source(db, source['id'], folder, modes[choice.get()])
                finally:
                    db.close()
            self.app._worker('Exporting…', action, lambda written: self.app._done(f'Exported {len(written)} item(s) to {folder}'))
        ttk.Button(dialog, text='Choose folder and export', command=run).pack(padx=12, pady=12)

    def wayback_report(self):
        from . import wayback
        from .http import HttpClient
        source = self.selected()
        if not source:
            return
        data_root = self.app.data_root
        def action():
            db = connect(data_root)
            try:
                return wayback.completeness_report(db, source['id'], HttpClient(db, interval=2))
            finally:
                db.close()
        def show(report):
            dialog = tk.Toplevel(self.app.root)
            dialog.title(f"Wayback captures: {source['title']}")
            dialog.geometry('860x480')
            lines = [f"Looked up {report['checked']} more page(s). {len(report['captured'])} captured, "
                     f"{len(report['gaps'])} without any capture (checked pages only).", '', 'Gaps:']
            lines += [f"  {gap['entry']}  {gap['original_url']}" for gap in report['gaps'][:200]] or ['  none']
            lines += ['', 'Captured (newest capture date, separate from publication date):']
            lines += [f"  {item['entry']}  published {item['published_at'] or '?'}  captured {item['captured_at'][:10]}  "
                      f"{item['capture_url']}" for item in report['captured'][:200]]
            text = tk.Text(dialog, wrap='none')
            text.insert('1.0', '\n'.join(lines))
            text.configure(state='disabled')
            text.pack(fill='both', expand=True, padx=12, pady=12)
        self.app._worker('Looking up Wayback captures…', action, show)

    def mirrors(self):
        from . import mirrors
        db = self.app.db
        found = mirrors.find_candidates(db)
        dialog = tk.Toplevel(self.app.root)
        dialog.title('Mirrors: the same comic from several sources')
        dialog.geometry('860x420')
        ttk.Label(dialog, wraplength=820, justify='left', text=(
            'Pairs that share a canonical address or catalog ID link automatically; a matching title and creator '
            'waits for you. A confirmed pair shares one series while each source keeps its own entries and '
            f'numbering. {len(found)} new pair(s) found.')).pack(anchor='w', padx=12, pady=(12, 6))
        titles = {row[0]: json.loads(row[1]).get('title') for row in db.execute('SELECT id, metadata FROM webcomic_series')}
        view = ttk.Treeview(dialog, columns=('b', 'confidence', 'evidence', 'status', 'edition'), show='tree headings',
                            selectmode='browse')
        view.heading('#0', text='Series')
        view.column('#0', width=200)
        for column, label, width in (('b', 'Mirror', 200), ('confidence', 'Match', 60), ('evidence', 'Evidence', 160),
                                     ('status', 'Status', 80), ('edition', 'Edition / language', 140)):
            view.heading(column, text=label)
            view.column(column, width=width, anchor='w')
        view.pack(fill='both', expand=True, padx=12)
        def render():
            view.delete(*view.get_children())
            for item in mirrors.links(db):
                view.insert('', 'end', iid=str(item['id']), text=titles.get(item['series_a']) or item['series_a'], values=(
                    titles.get(item['series_b']) or item['series_b'], f"{round(item['confidence'] * 100)}%",
                    ', '.join(item['evidence']), item['status'],
                    ' · '.join(value for value in (item.get('edition'), item.get('language')) if value)))
        def selected():
            chosen = view.selection()
            return int(chosen[0]) if chosen else None
        def confirm():
            link_id = selected()
            if link_id is None:
                return
            edition = simpledialog.askstring('Confirm mirror', 'Edition or language note (optional):', parent=dialog)
            mirrors.confirm(db, link_id, edition=edition or None)
            render()
            self.refresh()
        def reject():
            link_id = selected()
            if link_id is not None:
                mirrors.reject(db, link_id)
                render()
        def split():
            source = self.selected()
            if source and mirrors.split(db, source['id']):
                render()
                self.refresh()
                self.app._done(f"{source['title']} has its own series again")
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=12)
        ttk.Button(bar, text='Confirm', command=confirm).pack(side='left')
        ttk.Button(bar, text='Reject', command=reject).pack(side='left', padx=6)
        ttk.Button(bar, text='Split selected comic from its mirror', command=split).pack(side='left')
        ttk.Button(bar, text='Close', command=dialog.destroy).pack(side='right')
        render()

    def health(self):
        from . import health
        source = self.selected()
        if not source:
            return
        info = health.summary(self.app.db, source['id'])
        dialog = tk.Toplevel(self.app.root)
        dialog.title(f"Health: {source['title']}")
        dialog.geometry('820x520')
        lines = [f"Status: {info['health']}  ·  adapter {info['adapter']} v{info['adapter_version']}  ·  "
                 f"last success {info['last_success'] or 'never'}  ·  failures in a row {info['failures']}", '']
        lines += ['Warnings:'] + [f'  • {text}' for text in info['warnings']] if info['warnings'] else ['No warnings.']
        if info['history']:
            lines += ['', 'Recent checks (entries listed · stored · images per entry):']
            for sample in info['history'][-10:]:
                lines.append(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(sample['at']))}  "
                             f"{sample.get('snapshot_entries')} · {sample.get('entries')} · {sample.get('images_per_entry')}")
        lines += ['', 'Recent events:'] + [f"  {event['created_at']}  {event['event_type']}  {event['entry_key'] or ''}"
                                           for event in info['events'][:25]]
        text = tk.Text(dialog, wrap='word')
        text.insert('1.0', '\n'.join(lines))
        text.configure(state='disabled')
        text.pack(fill='both', expand=True, padx=12, pady=12)
        def reprocess():
            data_root = self.app.data_root
            def action():
                db = connect(data_root)
                try:
                    return health.reprocess_metadata(db, source['id'])
                finally:
                    db.close()
            self.app._worker('Reprocessing metadata…', action,
                             lambda count: (self.refresh(), self.app._done(f'Refreshed metadata for {count} entries (no downloads)')))
        bar = ttk.Frame(dialog)
        bar.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(bar, text='Reprocess metadata (no downloads)', command=reprocess).pack(side='left')
        ttk.Button(bar, text='Close', command=dialog.destroy).pack(side='right')

    def audit_images(self):
        from .audit import audit_source
        from .http import HttpClient
        source = self.selected()
        if not source:
            return
        data_root = self.app.data_root
        def action():
            db = connect(data_root)
            try:
                return audit_source(db, source['id'], HttpClient(db), sample=20)
            finally:
                db.close()
        def done(outcome):
            self.refresh()
            self.app._done(f"Audited {outcome['checked']} image(s): {outcome['unchanged']} unchanged, "
                           f"{outcome['changed']} changed (re-archiving), {outcome['errors']} unreachable")
        self.app._worker(f"Auditing {source['title']}…", action, done)

    def open_site(self):
        import webbrowser
        source = self.selected()
        if source:
            webbrowser.open(source['url'])

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
