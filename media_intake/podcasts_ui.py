"""Podcasts tab: find and follow shows, see episodes, and control automatic downloads."""
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import podcasts
from .core import connect


class PodcastsPanel:
    def __init__(self, app, parent):
        self.app = app
        self.results = []
        top = ttk.LabelFrame(parent, text='Find a podcast', padding=8)
        top.pack(fill='x')
        self.query = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query, width=50)
        entry.pack(side='left')
        entry.bind('<Return>', lambda _event: self.search())
        ttk.Button(top, text='Search directory', command=self.search).pack(side='left', padx=6)
        ttk.Label(top, text='or paste a feed URL and press Follow').pack(side='left')
        self.found = ttk.Treeview(parent, columns=('author', 'episodes', 'genre'), show='tree headings', height=6,
                                  selectmode='browse')
        self.found.heading('#0', text='Show')
        self.found.column('#0', width=330)
        for column, label, width in (('author', 'Host / publisher', 220), ('episodes', 'Episodes', 80),
                                     ('genre', 'Genre', 140)):
            self.found.heading(column, text=label)
            self.found.column(column, width=width, anchor='w')
        self.found.pack(fill='x', pady=(6, 0))
        follow_row = ttk.Frame(parent)
        follow_row.pack(fill='x', pady=6)
        ttk.Label(follow_row, text='When following, download').pack(side='left')
        self.backlog = tk.StringVar(value='Latest 3 episodes')
        ttk.Combobox(follow_row, textvariable=self.backlog, state='readonly', width=22,
                     values=('No back catalogue', 'Latest 3 episodes', 'Latest 10 episodes', 'Every episode')).pack(
            side='left', padx=6)
        ttk.Button(follow_row, text='Follow', command=self.follow).pack(side='left')

        shows_frame = ttk.LabelFrame(parent, text='Followed podcasts', padding=8)
        shows_frame.pack(fill='both', expand=True, pady=(8, 0))
        self.shows = ttk.Treeview(shows_frame, columns=('auto', 'keep', 'every', 'checked', 'status'),
                                  show='tree headings', height=6, selectmode='browse')
        self.shows.heading('#0', text='Show')
        self.shows.column('#0', width=280)
        for column, label, width in (('auto', 'Auto-download', 95), ('keep', 'Keep latest', 80),
                                     ('every', 'Check every', 85), ('checked', 'Last checked (UTC)', 150),
                                     ('status', 'Status', 220)):
            self.shows.heading(column, text=label)
            self.shows.column(column, width=width, anchor='w')
        self.shows.pack(fill='x')
        self.shows.bind('<<TreeviewSelect>>', lambda _event: self.show_episodes())
        bar = ttk.Frame(shows_frame)
        bar.pack(fill='x', pady=6)
        for label, command in (('Check now', self.check_selected), ('Check all', self.check_all),
                               ('Toggle auto-download', self.toggle_auto), ('Keep latest…', self.set_keep),
                               ('Check every…', self.set_interval), ('Unfollow', self.unfollow),
                               ('Import OPML…', self.import_opml), ('Export OPML…', self.export_opml)):
            ttk.Button(bar, text=label, command=command).pack(side='left', padx=(0, 6))
        self.episodes = ttk.Treeview(shows_frame, columns=('published', 'number', 'duration', 'state'),
                                     show='tree headings', selectmode='extended')
        self.episodes.heading('#0', text='Episode')
        self.episodes.column('#0', width=380)
        for column, label, width in (('published', 'Published (UTC)', 150), ('number', 'S/E', 70),
                                     ('duration', 'Duration', 80), ('state', 'State', 90)):
            self.episodes.heading(column, text=label)
            self.episodes.column(column, width=width, anchor='w')
        self.episodes.pack(fill='both', expand=True)
        episode_bar = ttk.Frame(shows_frame)
        episode_bar.pack(fill='x', pady=(6, 0))
        ttk.Button(episode_bar, text='Download selected episodes', command=self.download_selected).pack(side='left')
        self.refresh()

    # Helpers -------------------------------------------------------------
    def _db(self):
        return self.app.db

    def _selected_show(self):
        selected = self.shows.selection()
        return int(selected[0]) if selected else None

    def refresh(self):
        keep = self.shows.selection()
        self.shows.delete(*self.shows.get_children())
        for row in self._db().execute('SELECT * FROM podcast_shows ORDER BY title COLLATE NOCASE'):
            settings = podcasts._settings(row)
            counts = dict(self._db().execute(
                'SELECT state, COUNT(*) FROM podcast_episodes WHERE show_id=? GROUP BY state', (row['id'],)).fetchall())
            status = row['last_error'] or (f"{counts.get('downloaded', 0)} downloaded, {counts.get('queued', 0)} queued, "
                                           f"{counts.get('new', 0)} new")
            self.shows.insert('', 'end', iid=str(row['id']), text=row['title'], values=(
                'on' if settings.get('auto_download', True) else 'off', settings.get('keep_latest') or 'all',
                f"{settings.get('check_hours')} h", (row['last_checked'] or '')[:19].replace('T', ' '), status))
        if keep and self.shows.exists(keep[0]):
            self.shows.selection_set(keep[0])
        self.show_episodes()

    def show_episodes(self):
        self.episodes.delete(*self.episodes.get_children())
        show_id = self._selected_show()
        if show_id is None:
            return
        for row in self._db().execute('''SELECT * FROM podcast_episodes WHERE show_id=?
                ORDER BY COALESCE(published,'') DESC, id DESC LIMIT 500''', (show_id,)):
            number = ''
            if row['episode'] is not None:
                number = (f"S{row['season']}E{row['episode']}" if row['season'] is not None else f"E{row['episode']}")
            self.episodes.insert('', 'end', iid=str(row['id']), text=row['title'], values=(
                (row['published'] or '')[:16].replace('T', ' '), number, row['duration'] or '', row['state']))

    def _run(self, message, action, done):
        data_root = self.app.data_root
        def work():
            db = connect(data_root)
            try:
                return action(db)
            finally:
                db.close()
        def finished(result):
            self.refresh()
            self.app.refresh_queue()
            done(result)
        self.app._worker(message, work, finished)

    # Actions -------------------------------------------------------------
    def search(self):
        query = self.query.get().strip()
        if not query:
            return
        if query.startswith(('http://', 'https://')):
            self.follow()
            return
        def found(shows):
            self.results = shows
            self.found.delete(*self.found.get_children())
            for index, show in enumerate(shows):
                self.found.insert('', 'end', iid=str(index), text=show['title'], values=(
                    show['author'], show.get('episodes') or '', show.get('genre') or ''))
            self.app._done(f'{len(shows)} podcast(s) found')
        self.app._worker(f'Searching podcasts for {query}…', lambda: podcasts.search_directory(query), found)

    def follow(self):
        selected = self.found.selection()
        typed = self.query.get().strip()
        if selected:
            feed = self.results[int(selected[0])]['feed_url']
        elif typed.startswith(('http://', 'https://')):
            feed = typed
        else:
            messagebox.showinfo('Follow podcast', 'Select a search result or paste a feed URL.')
            return
        choice = self.backlog.get()
        backlog, latest = {'No back catalogue': ('none', 0), 'Latest 3 episodes': ('latest', 3),
                           'Latest 10 episodes': ('latest', 10), 'Every episode': ('all', 0)}[choice]
        self._run('Following podcast…', lambda db: podcasts.follow(db, feed, backlog=backlog, latest=latest),
                  lambda _show_id: self.app._done(f'Following; {choice.lower()} queued for download'))

    def import_opml(self):
        path = filedialog.askopenfilename(title='Import podcast subscriptions',
                                          filetypes=[('OPML', '*.opml *.xml'), ('All files', '*.*')])
        if not path:
            return
        try:
            with open(path, 'rb') as source:
                body = source.read(5_000_000)
            feeds = podcasts.parse_opml(body)
        except (OSError, ValueError) as exc:
            messagebox.showerror('Import OPML', str(exc))
            return
        if not feeds:
            messagebox.showinfo('Import OPML', 'That file lists no podcast feeds.')
            return

        def done(summary):
            lines = [f"Followed {len(summary['added'])} new show(s); {len(summary['existing'])} already followed."]
            if summary['failed']:
                lines += ['', 'Could not follow:'] + summary['failed'][:15]
            lines += ['', 'No back catalogue was queued; new episodes download as they are released.']
            messagebox.showinfo('Import OPML', '\n'.join(lines))
        self._run(f'Importing {len(feeds)} podcast feed(s)…', lambda db: podcasts.import_opml(db, body), done)

    def export_opml(self):
        path = filedialog.asksaveasfilename(title='Export podcast subscriptions', defaultextension='.opml',
                                            initialfile='podcasts.opml', filetypes=[('OPML', '*.opml')])
        if not path:
            return
        try:
            with open(path, 'wb') as target:
                target.write(podcasts.export_opml(self._db()))
        except OSError as exc:
            messagebox.showerror('Export OPML', str(exc))
            return
        self.app._done(f'Exported podcast subscriptions to {path}')

    def check_selected(self):
        show_id = self._selected_show()
        if show_id is not None:
            self._run('Checking podcast…', lambda db: podcasts.check_show(db, show_id, force=True),
                      lambda new: self.app._done(f'{new} new episode(s)'))

    def check_all(self):
        def action(db):
            ids = [row[0] for row in db.execute('SELECT id FROM podcast_shows')]
            total, errors = 0, 0
            for show_id in ids:
                try:
                    total += podcasts.check_show(db, show_id)
                except Exception:
                    errors += 1
            return total, errors
        self._run('Checking all podcasts…', action,
                  lambda outcome: self.app._done(f'{outcome[0]} new episode(s)' +
                                                 (f'; {outcome[1]} feed(s) failed' if outcome[1] else '')))

    def _update(self, **changes):
        show_id = self._selected_show()
        if show_id is not None:
            podcasts.update_settings(self._db(), show_id, **changes)
            self.refresh()

    def toggle_auto(self):
        show_id = self._selected_show()
        if show_id is None:
            return
        row = self._db().execute('SELECT settings FROM podcast_shows WHERE id=?', (show_id,)).fetchone()
        self._update(auto_download=not podcasts._settings(row).get('auto_download', True))

    def set_keep(self):
        from tkinter import simpledialog
        value = simpledialog.askinteger('Keep latest episodes', 'Keep only this many downloaded episodes '
                                        '(older files are deleted). 0 keeps everything.', minvalue=0, maxvalue=10000,
                                        parent=self.shows)
        if value is not None:
            self._update(keep_latest=value)

    def set_interval(self):
        from tkinter import simpledialog
        value = simpledialog.askfloat('Check every', 'Hours between checks for this podcast:', minvalue=0.25,
                                      maxvalue=168, parent=self.shows)
        if value is not None:
            self._update(check_hours=value)

    def unfollow(self):
        show_id = self._selected_show()
        if show_id is not None and messagebox.askyesno('Unfollow', 'Stop following this podcast? Downloaded files are kept.'):
            podcasts.unfollow(self._db(), show_id)
            self.refresh()

    def download_selected(self):
        ids = [int(iid) for iid in self.episodes.selection()]
        if ids:
            self._run('Queueing episodes…', lambda db: [podcasts.queue_episode(db, episode_id) for episode_id in ids],
                      lambda queued: self.app._done(f'Queued {len(queued)} episode(s)'))
