"""GUI for the source connectors: Settings → Sources, the release picker and result summaries."""
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import urllib.parse
import webbrowser

from . import source_health
from .connectors import (ALL, BY_LABEL, CONNECTORS, USENET_CLIENT_FIELDS, describe_missing, load_all_configs, load_source_config,
                         missing_settings, save_source_config, search_one)
from .connectors.base import SourceError, human_size
from .core import connect, enqueue
from .rights_evidence import load_rights_evidence, source_download_rights

ROLE_LABELS = {'torrent': 'Torrent index', 'usenet': 'Usenet indexer', 'direct': 'Direct downloads',
               'metadata': 'Metadata', 'browser': 'Browser search'}
SEARCH_LABEL_SUFFIX = {'torrent': 'torrent', 'usenet': 'Usenet', 'direct': 'download', 'metadata': 'metadata',
                       'browser': 'browser search'}


def single_source_choices():
    """Entries for the Search tab's "Search one source" list."""
    return [f'{spec.label} ({SEARCH_LABEL_SUFFIX[spec.role]})' for spec in CONNECTORS.values()]


def spec_for_choice(choice):
    label = str(choice or '').rsplit(' (', 1)[0]
    return BY_LABEL.get(label)


def connector_options(app, options):
    """Add connector settings and health recording to a search's source options."""
    data_root = app.data_root

    def health(key, ok, detail):
        db = connect(data_root)
        try:
            source_health.record(db, key, 'search', ok, detail=detail)
        finally:
            db.close()
    jackett = dict(getattr(app, 'external_dl_settings', {}) or {})
    return dict(options, source_configs=load_all_configs(app.db, data_root / 'cache', jackett), source_health=health,
                rights_evidence_path=options.get('rights_evidence_path') or str(app.rights_evidence_path))


def approval_text(rights_path, key):
    try:
        approved = source_download_rights(load_rights_evidence(rights_path), key)
    except Exception:
        approved = None
    return 'YES' if approved else 'NO'


def format_summary(item):
    """What a result offers, in plain words: '1080p, 720p · torrent', 'EPUB, PDF', 'NZB'."""
    variants = item.get('download_variants') or ([item] if item.get('connector_row') else [])
    if not variants or item.get('discovery_only'):
        return item.get('format', '')
    labels, kinds = [], []
    for row in variants:
        label = (str(row.get('resolution') or '') or str(row.get('file_format') or '').upper() or
                 (str(row.get('format') or '').lstrip('.').upper()
                  if row.get('format') not in ('.torrent', '.nzb') else ''))
        if row.get('platform') and not label:
            label = str(row['platform'])
        if label and label not in labels:
            labels.append(label)
        kind = {'.torrent': 'torrent', '.nzb': 'Usenet'}.get(row.get('format'), 'file')
        if kind not in kinds:
            kinds.append(kind)
    text = ', '.join(labels[:4]) + ('…' if len(labels) > 4 else '')
    return ' · '.join(part for part in (text, '/'.join(kinds)) if part)


def source_summary(item):
    variants = item.get('download_variants') or []
    base = item.get('display_source') or item.get('source', '')
    if len(variants) > 1:
        providers = list(dict.fromkeys(row.get('provider') for row in variants if row.get('provider')))
        return f'{len(variants)} releases · ' + ', '.join(providers[:3]) + ('…' if len(providers) > 3 else '')
    return base


def wants_release_picker(item):
    return any(row.get('connector_row') for row in item.get('download_variants') or [])


def queue_rows(app, rows):
    """Queue chosen releases; torrents need libtorrent or go to the external manager."""
    from .torrents import available
    torrents = [row for row in rows if row.get('format') == '.torrent']
    if torrents and not available():
        app._send_to_downloader(torrents)
        rows = [row for row in rows if row.get('format') != '.torrent']
        if not rows:
            return

    def action():
        db = connect(app.data_root)
        try:
            return [enqueue(db, dict(row, rights_evidence_path=row.get('rights_evidence_path') or
                                     str(app.rights_evidence_path))) for row in rows]
        finally:
            db.close()

    def queued(items):
        app.refresh_queue()
        from .core import download_settings
        automatic = download_settings(app.db).get('autostart')
        app._done(f'Queued {len(items)} release(s); ' + ('they start automatically' if automatic else
                                                       'start them from the Queue tab'))
    app._worker('Adding to queue…', action, queued)


class ReleasePicker:
    """Choose one or more releases of a work: quality, size, seeders and where it was found."""
    COLUMNS = (('release', 'Release', 330), ('quality', 'Quality / format', 150), ('size', 'Size', 75),
               ('health', 'Seeders / grabs', 95), ('source', 'Found on', 170), ('date', 'Published', 95))

    def __init__(self, app, item):
        self.app = app
        self.rows = list(item.get('download_variants') or [item])
        dialog = self.dialog = tk.Toplevel(app.root)
        dialog.title(f"Releases of {item.get('title') or 'this work'}")
        dialog.geometry('1000x440')
        dialog.transient(app.root)
        ttk.Label(dialog, text=(f"{len(self.rows)} release(s) found. Higher seeders/grabs usually download faster. "
                                'Torrents use the built-in torrent engine; Usenet releases go to SABnzbd/NZBGet.'),
                  wraplength=960, justify='left').pack(anchor='w', padx=10, pady=(10, 6))
        frame = ttk.Frame(dialog)
        frame.pack(fill='both', expand=True, padx=10)
        self.view = ttk.Treeview(frame, columns=[c[0] for c in self.COLUMNS], show='headings', selectmode='extended')
        for column, label, width in self.COLUMNS:
            self.view.heading(column, text=label, command=lambda c=column: self.sort(c))
            self.view.column(column, width=width, anchor='w')
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.view.yview)
        self.view.configure(yscrollcommand=scroll.set)
        self.view.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.render()
        self.view.bind('<Double-1>', lambda _event: self.queue())
        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=10, pady=10)
        ttk.Button(buttons, text='Queue selected', command=self.queue).pack(side='left')
        ttk.Button(buttons, text='Open release page', command=self.open_page).pack(side='left', padx=6)
        ttk.Button(buttons, text='Close', command=dialog.destroy).pack(side='right')
        if self.rows:
            self.view.selection_set('0')

    @staticmethod
    def values(row):
        health = row.get('seeders') if row.get('seeders') is not None else row.get('grabs')
        mirrors = row.get('mirror_providers') or [row.get('provider')]
        return (row.get('release_title') or row.get('title') or '', row.get('quality') or
                str(row.get('format') or '').lstrip('.').upper(), human_size(row.get('size')),
                '' if health is None else health, ', '.join(str(m) for m in mirrors if m),
                str(row.get('published') or '')[:16])

    def render(self):
        for child in self.view.get_children():
            self.view.delete(child)
        for index, row in enumerate(self.rows):
            self.view.insert('', 'end', iid=str(index), values=self.values(row))

    def sort(self, column):
        keys = {'size': lambda row: -(row.get('size') or 0),
                'health': lambda row: -((row.get('seeders') if row.get('seeders') is not None else row.get('grabs'))
                                        or 0),
                'date': lambda row: str(row.get('published') or ''), 'source': lambda row: str(row.get('provider')),
                'quality': lambda row: str(row.get('quality') or ''),
                'release': lambda row: str(row.get('release_title') or row.get('title') or '').casefold()}
        self.rows.sort(key=keys[column])
        self.render()

    def selected(self):
        return [self.rows[int(index)] for index in self.view.selection()]

    def queue(self):
        rows = self.selected()
        if not rows:
            return
        self.dialog.destroy()
        queue_rows(self.app, rows)

    def open_page(self):
        rows = self.selected()
        url = rows[0].get('details_url') if rows else ''
        parts = urllib.parse.urlsplit(str(url or ''))
        if parts.scheme in ('http', 'https') and parts.hostname:
            webbrowser.open(url)
        else:
            messagebox.showinfo('Open release page', 'This release has no web page.', parent=self.dialog)


class SourcesSettingsPanel:
    """Settings → Sources: switch sources on/off, enter accounts and keys, choose Jackett routing."""

    def __init__(self, app, parent):
        self.app = app
        ttk.Label(parent, text='Sources', font=('TkDefaultFont', 11, 'bold')).pack(anchor='w')
        ttk.Label(parent, wraplength=900, justify='left', text=(
            'Torrent, Usenet, library and catalogue sites. Select one to see what it needs. Sources that provide '
            'files are searched and downloaded only when their entry in your local rights-evidence.json is YES '
            '(use "Open rights file"). Metadata sources need no approval. A source that is down or blocked only '
            'shows an error for itself; the others keep working.')).pack(anchor='w', pady=(4, 8))
        self.view = ttk.Treeview(parent, columns=('type', 'categories', 'status'), show='tree headings',
                                 selectmode='browse', height=15)
        self.view.heading('#0', text='Source')
        self.view.column('#0', width=150, stretch=False)
        for column, label, width in (('type', 'Type', 120), ('categories', 'Categories', 260),
                                     ('status', 'Status', 470)):
            self.view.heading(column, text=label)
            self.view.column(column, width=width, anchor='w')
        self.view.pack(fill='x')
        self.view.bind('<<TreeviewSelect>>', lambda _event: self.show_selected())
        self.form = ttk.LabelFrame(parent, text='Selected source', padding=8)
        self.form.pack(fill='x', pady=(8, 0))
        ttk.Label(self.form, text='Select a source above to change its settings.').grid(row=0, column=0, sticky='w')
        actions = ttk.Frame(parent)
        actions.pack(fill='x', pady=(8, 0))
        ttk.Button(actions, text='Open rights file', command=app.open_rights_evidence).pack(side='left')
        ttk.Button(actions, text='Refresh status', command=self.refresh).pack(side='left', padx=6)
        self.status = tk.StringVar(value='')
        ttk.Label(parent, textvariable=self.status, wraplength=900, justify='left').pack(anchor='w', pady=(6, 0))
        self.client_box = ttk.LabelFrame(parent, text='Usenet downloads (SABnzbd or NZBGet)', padding=8)
        self.client_box.pack(fill='x', pady=(12, 0))
        self._build_client()
        self.refresh()

    # -- list
    def status_text(self, spec, config, health):
        parts = []
        if not config.get('enabled'):
            parts.append('Off')
        if spec.file_capable:
            parts.append(f'Downloads approved: {approval_text(self.app.rights_evidence_path, spec.key)}')
        missing = missing_settings(spec, config)
        if missing and not (spec.role == 'torrent' and config.get('access') == 'jackett'):
            parts.append('Needs ' + describe_missing(spec, missing))
        if spec.role == 'torrent' and spec.jackett_indexer and config.get('access') != 'auto':
            parts.append(f"via {config.get('access')}")
        if health and health.get('failure_streak') and health.get('last_error'):
            error = str(health['last_error']).split(': ', 1)[-1]
            parts.append(f"{'failing' if health['status'] == 'failing' else 'last attempt failed'}: {error[:70]}")
        elif health:
            parts.append(health['status'])
        return ' · '.join(parts) or 'Ready'

    def refresh(self):
        selected = self.view.selection()
        for child in self.view.get_children():
            self.view.delete(child)
        try:
            health = {row['source_key']: row for row in source_health.summaries(self.app.db)}
        except Exception:
            health = {}
        for spec in CONNECTORS.values():
            config = load_source_config(self.app.db, spec.key)
            self.view.insert('', 'end', iid=spec.key, text=spec.label, values=(
                ROLE_LABELS[spec.role], 'All categories' if spec.categories == ALL else
                ', '.join(sorted(spec.categories)), self.status_text(spec, config, health.get(spec.key))))
        if selected and self.view.exists(selected[0]):
            self.view.selection_set(selected[0])

    # -- form
    def show_selected(self):
        for child in self.form.winfo_children():
            child.destroy()
        selection = self.view.selection()
        if not selection:
            return
        spec = CONNECTORS[selection[0]]
        config = load_source_config(self.app.db, spec.key)
        self.form.configure(text=spec.label)
        ttk.Label(self.form, text=spec.summary, wraplength=760, justify='left').grid(
            row=1, column=0, columnspan=2, sticky='w', pady=(2, 8))
        self.enabled = tk.BooleanVar(value=bool(config.get('enabled')))
        label = ('Show a link to this source in category results' if spec.role == 'browser'
                 else 'Include in "Search all connected sources"')
        ttk.Checkbutton(self.form, text=label, variable=self.enabled).grid(row=2, column=0, columnspan=2,
                                                                           sticky='w')
        self.fields = {}
        row = 3
        for field, field_label, secret in spec.settings:
            ttk.Label(self.form, text=field_label).grid(row=row, column=0, sticky='w', pady=2)
            variable = tk.StringVar(value=config.get(field) or '')
            entry = ttk.Entry(self.form, textvariable=variable, width=38, show='•' if secret else '')
            entry.grid(row=row, column=1, sticky='w', pady=2)
            if field == 'base' and spec.default_base:
                ttk.Label(self.form, text=f'blank = {spec.default_base}', foreground='gray').grid(
                    row=row, column=2, sticky='w', padx=4)
            self.fields[field] = variable
            row += 1
        self.access = tk.StringVar(value=config.get('access') or 'auto')
        if spec.role == 'torrent' and spec.jackett_indexer:
            ttk.Label(self.form, text='Search through').grid(row=row, column=0, sticky='w', pady=2)
            ttk.Combobox(self.form, textvariable=self.access, state='readonly', width=12,
                         values=('auto', 'direct', 'jackett')).grid(row=row, column=1, sticky='w')
            row += 1
            ttk.Label(self.form, wraplength=760, justify='left', foreground='gray', text=(
                f'auto: the site directly, then your Jackett "{spec.jackett_indexer}" indexer if the site is '
                'blocked or down. jackett: always through Jackett (Settings → Downloads).')).grid(
                row=row, column=0, columnspan=3, sticky='w')
            row += 1
        buttons = ttk.Frame(self.form)
        buttons.grid(row=row, column=0, columnspan=3, sticky='w', pady=(10, 0))
        ttk.Button(buttons, text='Save', command=lambda: self.save(spec)).pack(side='left')
        self.test_query = tk.StringVar(value={'Games': 'tetris', 'Anime': 'cowboy bebop'}.get(
            next(iter(sorted(spec.categories))), 'night of the living dead'))
        ttk.Entry(buttons, textvariable=self.test_query, width=22).pack(side='left', padx=(12, 4))
        ttk.Button(buttons, text='Test search', command=lambda: self.test(spec)).pack(side='left')

    def save(self, spec):
        values = {field: variable.get() for field, variable in self.fields.items()}
        values['enabled'] = self.enabled.get()
        values['access'] = self.access.get()
        try:
            session_only = save_source_config(self.app.db, spec.key, values)
        except ValueError as exc:
            self.status.set(str(exc))
            return
        note = (f" {', '.join(session_only)} kept for this session only: protected storage needs Windows. "
                f"Set it with environment variables instead (see docs/SOURCES.md).") if session_only else ''
        self.status.set(f'{spec.label} saved.{note}')
        self.refresh()
        self.app._update_search_category_info()
        self.app._render_download_priority()

    def test(self, spec):
        self.save(spec)
        query = self.test_query.get().strip() or 'test'
        category = 'Videos' if 'Videos' in spec.categories else sorted(spec.categories)[0]
        configs = load_all_configs(self.app.db, self.app.data_root / 'cache',
                                   dict(getattr(self.app, 'external_dl_settings', {}) or {}))
        self.status.set(f'Testing {spec.label}…')

        def run():
            try:
                page = search_one(spec.key, query, category, 1, configs)
                message = (f'{spec.label} works ({page.via}): {len(page.rows)} result(s) for “{query}” in '
                           f'{category}.' + (f' {page.notice}' if page.notice else ''))
            except SourceError as exc:
                message = str(exc)
            except Exception as exc:
                message = f'{spec.label}: unexpected problem ({exc}).'
            self.app.root.after(0, lambda: (self.status.set(message), self.refresh()))
        threading.Thread(target=run, daemon=True).start()

    # -- Usenet client
    def _build_client(self):
        config = load_source_config(self.app.db, 'usenet_client')
        self.client_fields = {}
        for row, (field, label, secret) in enumerate(USENET_CLIENT_FIELDS):
            ttk.Label(self.client_box, text=label).grid(row=row // 2, column=(row % 2) * 2, sticky='w', padx=(0, 6),
                                                        pady=2)
            variable = tk.StringVar(value=config.get(field) or ('sabnzbd' if field == 'client' else
                                                                'fail' if field == 'nzb_mode' else ''))
            if field in ('client', 'nzb_mode'):
                widget = ttk.Combobox(self.client_box, textvariable=variable, state='readonly', width=14,
                                      values=('sabnzbd', 'nzbget') if field == 'client' else ('fail', 'save'))
            else:
                widget = ttk.Entry(self.client_box, textvariable=variable, width=30, show='•' if secret else '')
            widget.grid(row=row // 2, column=(row % 2) * 2 + 1, sticky='w', pady=2)
            self.client_fields[field] = variable
        buttons = ttk.Frame(self.client_box)
        buttons.grid(row=10, column=0, columnspan=4, sticky='w', pady=(8, 0))
        ttk.Button(buttons, text='Save', command=self.save_client).pack(side='left')
        ttk.Button(buttons, text='Test connection', command=self.test_client).pack(side='left', padx=6)

    def save_client(self):
        values = {field: variable.get() for field, variable in self.client_fields.items()}
        session_only = save_source_config(self.app.db, 'usenet_client', values)
        self.status.set('Usenet program saved.' + (' Secrets kept for this session only.' if session_only else ''))

    def test_client(self):
        from .connectors.usenet import test_client
        self.save_client()
        config = load_source_config(self.app.db, 'usenet_client')
        self.status.set('Testing the Usenet program…')

        def run():
            try:
                message = test_client(config)
            except SourceError as exc:
                message = str(exc)
            except Exception as exc:
                message = f'Usenet program: unexpected problem ({exc}).'
            self.app.root.after(0, lambda: self.status.set(message))
        threading.Thread(target=run, daemon=True).start()
