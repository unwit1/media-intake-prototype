"""Best-effort local conversion of supported book files to EPUB."""
import datetime as dt
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import zipfile

logger = logging.getLogger(__name__)


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_metadata(row, item):
    title = str(item.get('title') or Path(row['file_path']).stem).strip() or 'Untitled'
    author = str(item.get('author') or 'Unknown').strip() or 'Unknown'
    return title, author


def _write_epub(destination, title, author, paragraphs):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    uid = 'urn:uuid:' + str(uuid.uuid4())
    body = ''.join(f'<p>{html.escape(paragraph, quote=False)}</p>'
                   for paragraph in paragraphs if paragraph.strip())
    if not body:
        raise ValueError('No readable text was found to put in the EPUB.')
    content = ('<?xml version="1.0" encoding="utf-8"?>'
               '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" '
               'xmlns:epub="http://www.idpf.org/2007/ops" lang="en">'
               f'<head><title>{html.escape(title)}</title>'
               '<meta charset="utf-8"/></head><body>'
               f'<h1>{html.escape(title)}</h1>{body}</body></html>')
    nav = ('<?xml version="1.0" encoding="utf-8"?>'
           '<!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:epub="http://www.idpf.org/2007/ops" lang="en">'
           f'<head><title>{html.escape(title)}</title><meta charset="utf-8"/></head>'
           '<body><nav epub:type="toc" id="toc"><h1>Contents</h1><ol>'
           f'<li><a href="content.xhtml">{html.escape(title)}</a></li>'
           '</ol></nav></body></html>')
    package = ('<?xml version="1.0" encoding="utf-8"?>'
               '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
               'unique-identifier="book-id" xml:lang="en">'
               '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:identifier id="book-id">{uid}</dc:identifier>'
               f'<dc:title>{html.escape(title)}</dc:title>'
               f'<dc:creator>{html.escape(author)}</dc:creator>'
               '<dc:language>en</dc:language>'
               f'<meta property="dcterms:modified">{dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>'
               '</metadata><manifest>'
               '<item id="content" href="content.xhtml" media-type="application/xhtml+xml"/>'
               '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
               '</manifest><spine><itemref idref="content"/></spine></package>')
    temporary = destination.with_name(f'.{destination.stem}.{uuid.uuid4().hex}.partial')
    try:
        with zipfile.ZipFile(temporary, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip',
                             compress_type=zipfile.ZIP_STORED)
            archive.writestr('META-INF/container.xml',
                             '<?xml version="1.0" encoding="UTF-8"?>'
                             '<container version="1.0" '
                             'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                             '<rootfiles><rootfile full-path="EPUB/package.opf" '
                             'media-type="application/oebps-package+xml"/>'
                             '</rootfiles></container>', compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr('EPUB/content.xhtml', content, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr('EPUB/nav.xhtml', nav, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr('EPUB/package.opf', package, compress_type=zipfile.ZIP_DEFLATED)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or archive.read('mimetype') != b'application/epub+zip':
                raise ValueError('EPUB validation failed.')
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def _convert_text(source, destination, title, author):
    raw = Path(source).read_bytes()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            text = raw.decode('cp1252')
        except UnicodeDecodeError as exc:
            raise ValueError('Text encoding could not be read as UTF-8 or Windows-1252.') from exc
    paragraphs = [line.strip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    return _write_epub(destination, title, author, paragraphs)


def _convert_pdf(source, destination, title, author):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError('PDF to EPUB needs the bundled pypdf package. Rebuild the portable app.') from exc
    reader = PdfReader(str(source))
    paragraphs = []
    for page in reader.pages:
        text = page.extract_text() or ''
        paragraphs.extend(line.strip() for line in text.splitlines())
    if sum(len(value) for value in paragraphs) < 50:
        raise ValueError('This PDF has too little extractable text; scanned pages need OCR, which is not bundled.')
    return _write_epub(destination, title, author, paragraphs)


def _convert_with_calibre(source, destination):
    executable = shutil.which('ebook-convert') or shutil.which('ebook-convert.exe')
    if not executable:
        raise ValueError('No local converter is available for this format. Install Calibre to convert MOBI/AZW3 files.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='media-intake-epub-', dir=str(destination.parent)) as temp:
        output = Path(temp) / 'converted.epub'
        completed = subprocess.run([executable, str(source), str(output)],
                                   capture_output=True, text=True, timeout=600, check=False)
        if completed.returncode != 0 or not output.is_file():
            reason = (completed.stderr or completed.stdout or 'Calibre conversion failed').strip()
            raise RuntimeError(reason[-1000:])
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None or archive.read('mimetype') != b'application/epub+zip':
                raise ValueError('Calibre output was not a valid EPUB archive.')
        shutil.copyfile(output, destination)
    return destination


def _destination_for(source, item_id, source_digest):
    primary = Path(source).with_suffix('.epub')
    if not primary.exists():
        return primary
    stamped = primary.with_name(f'{primary.stem} [{int(item_id)}].epub')
    if not stamped.exists():
        return stamped
    if _sha256(stamped) == source_digest:
        return stamped
    return stamped.with_name(f'{stamped.stem} [{source_digest[:12]}].epub')


def ensure_book_epub(db, item_id):
    """Create and register an EPUB sidecar for a completed book; keep its source file."""
    from .kindle import _book_for_delivery

    raw_row = db.execute('SELECT * FROM items WHERE id=?', (int(item_id),)).fetchone()
    if not raw_row:
        return None
    row = dict(raw_row)
    if row.get('state') not in {'downloaded', 'imported'} or not row.get('file_path'):
        return row
    try:
        item = json.loads(row.get('metadata') or '{}')
    except (TypeError, ValueError):
        item = {}
    if not _book_for_delivery(db, row, item):
        return row
    source = Path(row['file_path']).expanduser()
    if source.suffix.casefold() == '.epub':
        return row
    if not source.is_file():
        return _save_conversion_error(db, row, item, f'Original book file is missing: {source}')

    source_digest = _sha256(source)
    previous = item.get('epub_conversion') or {}
    if previous.get('source_sha256') == source_digest and previous.get('status') == 'converted':
        converted = Path(previous.get('epub_path') or '')
        if converted.is_file():
            db.execute('UPDATE items SET file_path=?,sha256=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (str(converted), _sha256(converted), int(item_id)))
            db.commit()
            return dict(db.execute('SELECT * FROM items WHERE id=?', (int(item_id),)).fetchone())

    title, author = _safe_metadata(row, item)
    destination = _destination_for(source, item_id, source_digest)
    try:
        extension = source.suffix.casefold()
        if extension == '.txt':
            _convert_text(source, destination, title, author)
        elif extension == '.pdf':
            _convert_pdf(source, destination, title, author)
        else:
            _convert_with_calibre(source, destination)
        converted_hash = _sha256(destination)
        item['original_file_path'] = str(source)
        item['original_format'] = extension.lstrip('.').upper()
        item['format'] = '.epub'
        item['epub_conversion'] = {
            'status': 'converted', 'source_path': str(source),
            'source_sha256': source_digest, 'epub_path': str(destination),
            'converted_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        }
        db.execute('UPDATE items SET file_path=?,sha256=?,metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (str(destination), converted_hash,
                    json.dumps(item, ensure_ascii=False), int(item_id)))
        watch_id = item.get('watch_request_id') or item.get('watch_id')
        if watch_id:
            db.execute('UPDATE watched_items SET file_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (str(destination), int(watch_id)))
        db.commit()
    except Exception as exc:
        _save_conversion_error(db, row, item, str(exc))
    return dict(db.execute('SELECT * FROM items WHERE id=?', (int(item_id),)).fetchone())


def _save_conversion_error(db, row, item, error):
    logger.warning('EPUB conversion failed for library item %s: %s', row.get('id'), error)
    item['epub_conversion'] = {
        'status': 'failed', 'error': str(error)[:1200],
        'attempted_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
    }
    db.execute('UPDATE items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
               (json.dumps(item, ensure_ascii=False), row['id']))
    db.commit()
    return dict(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())


def convert_library_books_to_epub(db):
    """Convert each completed book item once and return per-item results."""
    rows = db.execute("SELECT id FROM items WHERE state IN ('downloaded','imported')").fetchall()
    results = []
    for row in rows:
        converted = ensure_book_epub(db, int(row['id']))
        if converted:
            try:
                metadata = json.loads(converted.get('metadata') or '{}')
            except (TypeError, ValueError):
                metadata = {}
            outcome = metadata.get('epub_conversion') or {}
            results.append({'id': int(row['id']), 'status': outcome.get('status',
                             'already_epub' if Path(converted.get('file_path') or '').suffix.casefold() == '.epub'
                             else 'unsupported'), 'error': outcome.get('error', '')})
    return results
