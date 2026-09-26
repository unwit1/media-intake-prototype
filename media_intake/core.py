import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

EXTENSIONS = {'.epub', '.pdf', '.mobi', '.azw3', '.txt', '.cbz', '.mp3', '.m4b'}
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
    if url.scheme not in ('http', 'https', 'irc+xdcc', 'serial+https', 'serial+http') or not url.netloc or url.username or url.password:
        raise ValueError('Expected an HTTP(S), IRC, or serial URL without embedded credentials')
    item['format'] = ext
    item.setdefault('author', 'Unknown')
    return item


def connect(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'intake.sqlite3')
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', file_path TEXT, sha256 TEXT,
        error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
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
    db.commit()
    return db


def enqueue(db, item):
    item = validate_item(dict(item))
    db.execute("INSERT INTO items(url, metadata) VALUES (?, ?) ON CONFLICT(url) DO UPDATE SET metadata=excluded.metadata, updated_at=CURRENT_TIMESTAMP", (item['url'], json.dumps(item, ensure_ascii=False)))
    db.commit()
    return dict(db.execute('SELECT * FROM items WHERE url=?', (item['url'],)).fetchone())


def list_items(db, state=None):
    sql = 'SELECT * FROM items' + (' WHERE state=?' if state else '') + ' ORDER BY id'
    return [dict(x) for x in db.execute(sql, (state,) if state else ())]


def http_get(url, max_bytes=10_000_000):
    request = urllib.request.Request(url, headers={'User-Agent': 'MediaIntakePrototype/0.1 (personal library intake)'})
    with urllib.request.urlopen(request, timeout=30) as response:
        size = response.headers.get('Content-Length')
        if size and int(size) > max_bytes:
            raise ValueError('Response exceeds size limit')
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError('Response exceeds size limit')
        return data, response.geturl()


def check_download(path, ext):
    """Reject common error pages and broken archive formats before queuing import."""
    if ext in ('.epub', '.cbz'):
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


def download(db, item_id, root, max_bytes=MAX_FILE_BYTES, destination=None):
    row = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
    if row is None: raise ValueError('Unknown queue ID')
    if row['state'] in ('downloaded', 'imported') and row['file_path'] and Path(row['file_path']).exists():
        return dict(row)
    item = validate_item(json.loads(row['metadata']))
    if urllib.parse.urlsplit(item['url']).scheme == 'irc+xdcc':
        try:
            from .irc import download_irc
            return download_irc(db, row, root, max_bytes, destination)
        except Exception as exc:
            db.execute("UPDATE items SET state='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc), item_id))
            db.commit()
            raise
    if urllib.parse.urlsplit(item['url']).scheme in ('serial+http', 'serial+https'):
        try:
            from .serial import download_serial
            return download_serial(db, row, root, max_bytes)
        except Exception as exc:
            db.execute("UPDATE items SET state='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc), item_id))
            db.commit()
            raise
    folder = Path(destination) if destination else Path(root) / 'downloads'
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.partial-', dir=folder)
    digest, total = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, 'wb') as out:
            req = urllib.request.Request(item['url'], headers={'User-Agent': 'MediaIntakePrototype/0.1 (personal library intake)'})
            with urllib.request.urlopen(req, timeout=60) as response:
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                if content_type in ('text/html', 'application/xhtml+xml'):
                    raise ValueError('URL returned an HTML page, not a media file')
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk: break
                    total += len(chunk)
                    if total > max_bytes: raise ValueError('File exceeds size limit')
                    digest.update(chunk)
                    out.write(chunk)
        if not total: raise ValueError('Empty download')
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
        db.execute("UPDATE items SET state='downloaded', file_path=?, sha256=?, error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(dest.resolve()), digest.hexdigest(), item_id))
        db.commit()
    except Exception as exc:
        if os.path.exists(tmp): os.unlink(tmp)
        db.execute("UPDATE items SET state='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(exc), item_id))
        db.commit()
        raise
    return dict(db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone())


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
