import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .media_graph import initialize_media_graph

BOOK_FORMATS = {'.epub', '.pdf', '.mobi', '.azw3', '.txt'}
AUDIO_FORMATS = {'.mp3', '.m4b', '.m4a', '.flac', '.ogg', '.opus', '.wav', '.aac'}
COMIC_FORMATS = {'.cbz', '.cbr', '.zip', '.rar', '.7z'}
VIDEO_FORMATS = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.webm', '.mpg', '.mpeg', '.ts', '.wmv'}
GAME_FORMATS = {
    '.iso', '.chd', '.cso', '.xci', '.nsp', '.gba', '.gbc', '.gb', '.nds',
    '.3ds', '.cia', '.nes', '.sfc', '.smc', '.n64', '.z64', '.v64', '.wbfs',
}
GAME_PACKAGE_FORMATS = GAME_FORMATS | {
    '.zip', '.rar', '.7z', '.exe', '.msi', '.dmg', '.pkg', '.deb', '.bin',
}
SOFTWARE_FORMATS = {
    '.7z', '.apk', '.appimage', '.bin', '.cab', '.deb', '.dmg', '.exe', '.flatpak',
    '.iso', '.jar', '.msi', '.msix', '.pkg', '.rpm', '.snap', '.tar',
    '.tar.bz2', '.tar.gz', '.tar.xz', '.tar.zst', '.tbz2', '.tgz', '.txz',
    '.whl', '.zip', '.zst',
}
EXTENSIONS = (BOOK_FORMATS | AUDIO_FORMATS | COMIC_FORMATS | VIDEO_FORMATS |
              GAME_FORMATS | GAME_PACKAGE_FORMATS | SOFTWARE_FORMATS)
ATOM = '{http://www.w3.org/2005/Atom}'
DC = '{http://purl.org/dc/terms/}'
MAX_FILE_BYTES = 1024 * 1024 * 1024


def clean(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(value or ''))
    value = re.sub(r'\s+', ' ', value).strip(' .')
    return value[:120] or 'Unknown'


def file_name(item):
    series = item.get('series')
    number = item.get('series_index')
    if isinstance(number, float):
        number = f'{number:g}'
    prefix = f"{clean(series)} {clean(number)} - " if series and number is not None else ''
    return f"{prefix}{clean(item['title'])} - {clean(item.get('author') or 'Unknown')}{item['format']}"


def validate_item(item):
    for key in ('title', 'url', 'format', 'source'):
        if not item.get(key):
            raise ValueError(f'Missing {key}')
    ext = str(item['format']).lower()
    if ext not in EXTENSIONS:
        raise ValueError(f'Unsupported format: {ext}')
    url = urllib.parse.urlsplit(item['url'])
    if url.scheme not in ('http', 'https', 'irc+xdcc', 'serial+https', 'serial+http',
                          'fanfic+https', 'fanfic+http') or not url.netloc or url.username or url.password:
        raise ValueError('Expected an HTTP(S), IRC, serial, or fanfiction URL without embedded credentials')
    item['format'] = ext
    item.setdefault('author', 'Unknown')
    return item


def connect(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # Watch scans and manual batch downloads use independent worker connections.
    # Give SQLite time to serialize their short writes while network transfers run
    # concurrently. Keep check_same_thread at its safe default: each worker owns
    # the connection it creates.
    db = sqlite3.connect(root / 'intake.sqlite3', timeout=30)
    db.execute('PRAGMA busy_timeout=30000')
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', file_path TEXT, sha256 TEXT,
        error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        progress_bytes INTEGER, progress_total INTEGER, progress_message TEXT)''')
    item_columns = {row['name'] for row in db.execute('PRAGMA table_info(items)')}
    for name, declaration in (('progress_bytes', 'INTEGER'),
                              ('progress_total', 'INTEGER'),
                              ('progress_message', 'TEXT')):
        if name not in item_columns:
            db.execute(f'ALTER TABLE items ADD COLUMN {name} {declaration}')
    db.execute('''CREATE TABLE IF NOT EXISTS watched_items (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT 'Unknown', source_url TEXT, release_date TEXT,
        state TEXT NOT NULL, next_check_at TEXT, last_checked_at TEXT,
        last_error TEXT, file_path TEXT, parent_id INTEGER, metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(parent_id) REFERENCES watched_items(id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS gui_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS watched_source_url ON watched_items(source_url)')
    db.execute('CREATE INDEX IF NOT EXISTS watched_parent_kind ON watched_items(parent_id,kind)')
    db.execute('''CREATE INDEX IF NOT EXISTS items_watch_request ON items(
        CAST(CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.watch_request_id') END AS TEXT))''')
    db.execute('''CREATE INDEX IF NOT EXISTS items_watch_id ON items(
        CAST(CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.watch_id') END AS TEXT))''')
    initialize_media_graph(db)
    from .webcomics.storage import migrate as migrate_webcomics
    migrate_webcomics(db)
    from .book_batch import migrate as migrate_book_batches
    migrate_book_batches(db)
    db.commit()
    return db


def enqueue(db, item):
    item = validate_item(dict(item))
    db.execute("INSERT INTO items(url, metadata) VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET metadata=excluded.metadata, updated_at=CURRENT_TIMESTAMP", (item['url'], json.dumps(item, ensure_ascii=False)))
    db.commit()
    return dict(db.execute('SELECT * FROM items WHERE url=?', (item['url'],)).fetchone())


def queue_watch_search(db, watch_id, title, author='Unknown', release_date=None,
                       upcoming=False, series=None, series_index=None,
                       publication_status=None):
    """Keep a book visible in Queue while its watched sources are being searched."""
    watch_id = int(watch_id)
    request_url = f'watch-search://{watch_id}'
    item = {'title': str(title or 'Untitled'), 'author': str(author or 'Unknown'),
            'format': '—', 'watch_search': True, 'watch_id': watch_id,
            'release_date': release_date}
    if series:
        item['series'] = str(series)
    if series_index is not None:
        item['series_index'] = series_index
    if publication_status:
        item['publication_status'] = str(publication_status)
    payload = json.dumps(item, ensure_ascii=False)
    state = 'upcoming' if upcoming else 'searching'
    row = db.execute('''SELECT * FROM items WHERE
        CAST(CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.watch_request_id') END AS TEXT)=?
        ORDER BY id LIMIT 1''', (str(watch_id),)).fetchone()
    if row:
        return dict(row)
    db.execute('''INSERT INTO items(url,metadata,state) VALUES (?,?,?)
        ON CONFLICT(url) DO UPDATE SET metadata=excluded.metadata,state=excluded.state,
        error=NULL,progress_message=CASE WHEN excluded.state='searching'
            THEN 'Preparing connector search' ELSE NULL END,
        updated_at=CURRENT_TIMESTAMP''', (request_url, payload, state))
    db.commit()
    return dict(db.execute('SELECT * FROM items WHERE url=?', (request_url,)).fetchone())


def update_watch_search(db, watch_id, state='searching', error=None,
                        progress_message=None):
    """Update the visible search request without turning it into a fake download."""
    request_url = f'watch-search://{int(watch_id)}'
    db.execute('''UPDATE items SET state=?,error=?,progress_message=?,
        updated_at=CURRENT_TIMESTAMP WHERE url=?''',
        (state, str(error or '')[:1000] or None,
         str(progress_message or '')[:200] or None, request_url))
    db.commit()


def enqueue_for_watch(db, item, watch_id):
    """Resolve a watched-book queue request into its real, automatically started file."""
    watch_id = int(watch_id)
    item = validate_item(dict(item))
    item['watch_request_id'] = watch_id
    payload = json.dumps(item, ensure_ascii=False)
    existing = db.execute('SELECT * FROM items WHERE url=?', (item['url'],)).fetchone()

    # A connector may have already queued this exact source (or a previous scan may
    # have tried it). Reuse one row per watched work and preserve completed files.
    request_row = db.execute('''SELECT * FROM items WHERE
        (CAST(CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.watch_id') END AS TEXT)=?
         AND CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.watch_search') END)
        OR CAST(CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.watch_request_id') END AS TEXT)=?
        ORDER BY id LIMIT 1''', (str(watch_id), str(watch_id))).fetchone()

    if existing:
        if request_row and int(request_row['id']) != int(existing['id']):
            db.execute('DELETE FROM items WHERE id=?', (request_row['id'],))
        try:
            state = existing['state']
            if state in ('failed', 'searching', 'upcoming'):
                state = 'queued'
            db.execute('''UPDATE items SET metadata=?,state=?,error=NULL,
                progress_bytes=NULL,progress_total=NULL,progress_message=NULL,
                updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                       (payload, state, existing['id']))
        except Exception:
            raise
        db.commit()
        return dict(db.execute('SELECT * FROM items WHERE id=?', (existing['id'],)).fetchone())

    if request_row:
        db.execute('''UPDATE items SET url=?,metadata=?,state='queued',file_path=NULL,
            sha256=NULL,error=NULL,progress_bytes=NULL,progress_total=NULL,
            progress_message=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   (item['url'], payload, request_row['id']))
        db.commit()
        return dict(db.execute('SELECT * FROM items WHERE id=?', (request_row['id'],)).fetchone())
    return enqueue(db, item)


def list_items(db, state=None):
    sql = 'SELECT * FROM items' + (' WHERE state=?' if state else '') + ' ORDER BY id'
    return [dict(x) for x in db.execute(sql, (state,) if state else ())]


def update_download_progress(db, item_id, done=None, total=None, message=None):
    """Persist a queue item's current transfer phase and byte counts."""
    if done is not None:
        done = max(0, int(done))
    if total is not None:
        total = max(0, int(total))
    db.execute('''UPDATE items SET progress_bytes=?, progress_total=?, progress_message=?,
        updated_at=CURRENT_TIMESTAMP WHERE id=?''', (done, total, str(message or '')[:200], item_id))
    db.commit()


def open_url(request, timeout=30):
    """Open a URL and release HTTP error response streams before propagating them."""
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # HTTPError is also a response stream. Close it while keeping the
        # original exception available to callers for status-specific handling.
        exc.close()
        raise


def http_get(url, max_bytes=10_000_000):
    request = urllib.request.Request(url, headers={'User-Agent': 'MediaIntakePrototype/0.1 (personal library intake)'})
    with open_url(request, timeout=30) as response:
        size = response.headers.get('Content-Length')
        if size and int(size) > max_bytes:
            raise ValueError('Response exceeds size limit')
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError('Response exceeds size limit')
        return data, response.geturl()


def check_download(path, ext):
    """Reject common error pages and broken archive formats before queuing import."""
    if ext in ('.epub', '.cbz', '.zip'):
        if not zipfile.is_zipfile(path):
            raise ValueError(f'Invalid {ext} archive')
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError(f'Damaged {ext} archive')
            if ext == '.epub':
                try:
                    correct_mimetype = archive.read('mimetype') == b'application/epub+zip'
                except KeyError:
                    correct_mimetype = False
                if not correct_mimetype:
                    raise ValueError('Invalid EPUB mimetype')
    elif ext == '.pdf':
        with open(path, 'rb') as source:
            if not source.read(8).startswith(b'%PDF-'):
                raise ValueError('Invalid PDF header')


def search_catalog(path, query):
    items = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(items, list):
        raise ValueError('Catalog must contain a JSON array')
    terms = query.casefold().split()
    return [validate_item(dict(i)) for i in items if all(t in (str(i.get('title', '')) + ' ' + str(i.get('author', '')) + ' ' + str(i.get('series', ''))).casefold() for t in terms)]


def search_opds(feed_url, query, pages=3):
    """Read an OPDS/Atom acquisition feed. Supports rel=next pagination."""
    items, seen = [], set()
    url = feed_url
    terms = query.casefold().split()
    for _ in range(pages):
        if not url or url in seen:
            break
        seen.add(url)
        raw, final_url = http_get(url)
        root = ET.fromstring(raw)
        if root.tag != ATOM + 'feed':
            raise ValueError('Expected Atom/OPDS feed')
        for entry in root.findall(ATOM + 'entry'):
            title = entry.findtext(ATOM + 'title', default='').strip()
            author_node = entry.find(ATOM + 'author')
            author = author_node.findtext(ATOM + 'name', default='Unknown') if author_node is not None else 'Unknown'
            haystack = (title + ' ' + author).casefold()
            if not all(t in haystack for t in terms):
                continue
            links = [link for link in entry.findall(ATOM + 'link') if 'acquisition' in link.get('rel', '')]
            for link in links:
                href = link.get('href', '')
                ext = Path(urllib.parse.urlsplit(href).path).suffix.lower()
                media = link.get('type', '')
                if media == 'application/epub+zip': ext = '.epub'
                if media == 'application/pdf': ext = '.pdf'
                if ext not in EXTENSIONS: continue
                item = {'title': title, 'author': author, 'source': final_url,
                        'source_id': entry.findtext(ATOM + 'id', default=''),
                        'url': urllib.parse.urljoin(final_url, href), 'format': ext}
                items.append(validate_item(item))
        next_link = next((link.get('href') for link in root.findall(ATOM + 'link') if link.get('rel') == 'next'), None)
        url = urllib.parse.urljoin(final_url, next_link) if next_link else None
    return items


def _auto_deliver_to_kindle(db, item_id, fallback_root):
    from .kindle import maybe_auto_send_to_kindle

    database = db.execute('PRAGMA database_list').fetchone()
    data_root = Path(database['file']).parent if database and database['file'] else Path(fallback_root)
    try:
        from .epub_conversion import ensure_book_epub
        ensure_book_epub(db, item_id)
    except Exception:
        # A format conversion issue should be visible in item metadata/logs, but
        # must not turn a successful source download into a failed queue item.
        import logging
        logging.getLogger(__name__).exception(
            'Local EPUB conversion failed for completed queue item %s', item_id)
    return maybe_auto_send_to_kindle(db, item_id, data_root)


def download(db, item_id, root, max_bytes=MAX_FILE_BYTES, destination=None):
    row = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
    if row is None: raise ValueError('Unknown queue ID')
    item = json.loads(row['metadata'])
    if item.get('watch_search'):
        raise ValueError('This book is waiting for an automatic source search; it has no file to download yet.')
    item = validate_item(item)
    explicit_rights_path = item.get('rights_evidence_path')
    rights_path = explicit_rights_path or (Path(root) / 'rights-evidence.json')
    if explicit_rights_path and not Path(rights_path).is_file():
        raise ValueError(f'Local rights evidence file is missing: {rights_path}')
    if Path(rights_path).is_file():
        from .rights_evidence import load_rights_evidence, source_download_rights, source_key_for_item
        document = load_rights_evidence(rights_path)
        work_key = str(item.get('rights_work_key') or '')
        work_record = (document.get('works') or {}).get(work_key) if work_key else None
        if isinstance(work_record, dict):
            approved = work_record.get('approved_for_download') is True
        else:
            approved = source_download_rights(document, source_key_for_item(item)) is not None
        if not approved:
            source = source_key_for_item(item)
            raise ValueError(f'Download not approved: set {source}.approved_for_download to true in {rights_path}')
    if row['state'] in ('downloaded', 'imported') and row['file_path'] and Path(row['file_path']).exists() \
            and urllib.parse.urlsplit(item['url']).scheme not in ('fanfic+http', 'fanfic+https'):
        return _auto_deliver_to_kindle(db, item_id, root)
    db.execute("UPDATE items SET state='downloading', error=NULL, progress_bytes=0, progress_total=NULL, progress_message='Preparing download', updated_at=CURRENT_TIMESTAMP WHERE id=?", (item_id,))
    db.commit()
    if urllib.parse.urlsplit(item['url']).scheme in ('fanfic+http', 'fanfic+https'):
        try:
            from .fanfiction import download_fanfiction
            result = download_fanfiction(db, row, root, max_bytes, destination)
            update_download_progress(db, item_id, None, None, None)
            return _auto_deliver_to_kindle(db, item_id, root) or result
        except Exception as exc:
            db.execute("UPDATE items SET state='failed', error=?, progress_bytes=NULL, progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc), item_id))
            db.commit()
            raise
    if urllib.parse.urlsplit(item['url']).scheme == 'irc+xdcc':
        try:
            from .irc import download_irc
            result = download_irc(db, row, root, max_bytes, destination)
            update_download_progress(db, item_id, None, None, None)
            return _auto_deliver_to_kindle(db, item_id, root) or result
        except Exception as exc:
            db.execute("UPDATE items SET state='failed', error=?, progress_bytes=NULL, progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc), item_id))
            db.commit()
            raise
    if urllib.parse.urlsplit(item['url']).scheme in ('serial+http', 'serial+https'):
        try:
            if item.get('webcomic_job'):
                from .webcomics.downloads import download_entry as download_serial
            else:
                from .serial import download_serial
            result = download_serial(db, row, root, max_bytes, destination)
            update_download_progress(db, item_id, None, None, None)
            return _auto_deliver_to_kindle(db, item_id, root) or result
        except Exception as exc:
            db.execute("UPDATE items SET state='failed', error=?, progress_bytes=NULL, progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc), item_id))
            db.commit()
            raise
    folder = Path(destination) if destination else Path(root) / 'downloads'
    digest, total = hashlib.sha256(), 0
    tmp = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.partial-', dir=folder)
        with os.fdopen(fd, 'wb') as out:
            req = urllib.request.Request(item['url'], headers={'User-Agent': 'MediaIntakePrototype/0.1 (personal library intake)'})
            with open_url(req, timeout=60) as response:
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                if content_type in ('text/html', 'application/xhtml+xml'):
                    raise ValueError('URL returned an HTML page, not a media file')
                try:
                    expected = int(response.headers.get('Content-Length') or '')
                    if expected <= 0:
                        expected = None
                except ValueError:
                    expected = None
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    total += len(chunk)
                    if total > max_bytes: raise ValueError('File exceeds size limit')
                    digest.update(chunk)
                    out.write(chunk)
                    update_download_progress(db, item_id, total, expected, 'Downloading file')
        if total:
            update_download_progress(db, item_id, total, expected, 'Validating downloaded file')
        if not total: raise ValueError('Empty download')
        if expected is not None and total != expected:
            raise ValueError('Download truncated: byte count differs from Content-Length')
        if item.get('expected_size') is not None and total != int(item['expected_size']):
            raise ValueError('Downloaded size differs from host metadata')
        if item.get('expected_sha256') and digest.hexdigest().casefold() != str(item['expected_sha256']).casefold():
            raise ValueError('Downloaded checksum differs from host metadata')
        check_download(tmp, item['format'])
        base = folder / file_name(item)
        dest = base
        if dest.exists():
            existing = hashlib.sha256(dest.read_bytes()).hexdigest()
            if existing == digest.hexdigest():
                os.unlink(tmp)
                dest = base
            else:
                dest = base.with_name(base.stem + f" [{digest.hexdigest()[:8]}]" + base.suffix)
                os.replace(tmp, dest)
        else:
            os.replace(tmp, dest)
        db.execute("UPDATE items SET state='downloaded', file_path=?, sha256=?, error=NULL, progress_bytes=NULL, progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(dest.resolve()), digest.hexdigest(), item_id))
        db.commit()
    except Exception as exc:
        if tmp and os.path.exists(tmp): os.unlink(tmp)
        db.execute("UPDATE items SET state='failed', error=?, progress_bytes=NULL, progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc), item_id))
        db.commit()
        raise
    return _auto_deliver_to_kindle(db, item_id, root)


def import_calibre(db, item_id, library=None, executable='calibredb'):
    row = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
    if row is None or row['state'] not in ('downloaded', 'imported') or not row['file_path'] or not Path(row['file_path']).is_file():
        raise ValueError('Download the item before import')
    if row['state'] == 'imported': return dict(row)
    item = json.loads(row['metadata'])
    cmd = [executable, 'add']
    if library: cmd += ['--with-library', str(library)]
    cmd += ['--title', item['title'], '--authors', item.get('author', 'Unknown')]
    if item.get('series'): cmd += ['--series', item['series']]
    if item.get('series_index') is not None: cmd += ['--series-index', str(item['series_index'])]
    cmd.append(row['file_path'])
    subprocess.run(cmd, check=True)
    db.execute("UPDATE items SET state='imported', updated_at=CURRENT_TIMESTAMP WHERE id=?", (item_id,))
    db.commit()
    return dict(db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())
