"""Small Tkinter desktop front end for the media intake queue."""
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import connect, download, enqueue, list_items
from .fmhy import candidates
from .serial import enqueue_serial
from .sources import resolve_detail, search_openlibrary, search_site


class MediaIntakeApp:
    def __init__(self, root, data_root=None):
        self.root = root
        self.root.title('Media Intake')
        self.root.geometry('1050x680')
        self.data_root = Path(data_root or (Path.home() / 'MediaIntakeData'))
        self.db = connect(self.data_root)
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
        self._build()
        self.refresh_queue()

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
        tabs.add(search_tab, text='Search and add')
        tabs.add(queue_tab, text='Queue')
        self._build_search(search_tab)
        self._build_queue(queue_tab)

    def choose_root(self):
        chosen = filedialog.askdirectory(initialdir=str(self.data_root), title='Choose Media Intake data folder')
        if not chosen: return
        self.data_root = Path(chosen)
        self.db.close()
        self.db = connect(self.data_root)
        self.refresh_queue()

    def _build_search(self, parent):
        controls = ttk.Frame(parent)
        controls.pack(fill='x')
        ttk.Label(controls, text='Search').pack(side='left')
        entry = ttk.Entry(controls, textvariable=self.query, width=42)
        entry.pack(side='left', padx=6)
        entry.bind('<Return>', lambda _event: self.search())
        ttk.Combobox(controls, textvariable=self.source, state='readonly', width=20,
                     values=['Open Library', 'LibGen', "Anna's Archive", 'FMHY directory']).pack(side='left')
        ttk.Entry(controls, textvariable=self.site_base, width=28).pack(side='left', padx=6)
        ttk.Button(controls, text='Search', command=self.search).pack(side='left')
        ttk.Label(parent, text='Optional site base URL (for LibGen or Anna’s Archive)').pack(anchor='w', pady=(4, 6))

        columns = ('title', 'author', 'format', 'source')
        self.results_view = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        for col, width in (('title', 330), ('author', 220), ('format', 80), ('source', 330)):
            self.results_view.heading(col, text=col.title())
            self.results_view.column(col, width=width, anchor='w')
        self.results_view.pack(fill='both', expand=True)
        self.results_view.bind('<Double-1>', lambda _event: self.queue_result())
        buttons = ttk.Frame(parent)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Queue selected result', command=self.queue_result).pack(side='left')

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
                self.root.after(0, lambda: (self._done('Error'), messagebox.showerror('Media Intake', str(exc))))
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
        if item.get('fmhy'):
            messagebox.showinfo('FMHY directory result', 'This is a discovery link. Open it in a browser and add a resolved direct file URL.')
            return
        def action():
            db = connect(self.data_root)
            try: return enqueue(db, item)
            finally: db.close()
        self._worker('Adding to queue…', action, lambda _result: self._done('Queued'))

    def add_direct(self):
        item = {'title': self.title.get().strip(), 'author': self.author.get().strip() or 'Unknown',
                'url': self.url.get().strip(), 'format': self.format.get().strip(), 'source': self.url.get().strip()}
        def action():
            db = connect(self.data_root)
            try: return enqueue(db, item)
            finally: db.close()
        self._worker('Adding to queue…', action, lambda _result: self._done('Queued'))

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
        def action():
            db = connect(self.data_root)
            try: return download(db, item_id, self.data_root)
            finally: db.close()
        self._worker('Downloading…', action, lambda _result: (self.refresh_queue(), self._done('Downloaded')))


def main(data_root=None):
    root = tk.Tk()
    MediaIntakeApp(root, data_root)
    root.mainloop()


if __name__ == '__main__':
    main()

