"""Export archived webcomics as folders, chapter/series CBZ files or image EPUBs, plus an OPDS 2.0 catalog.

Every exported image is read from the archived CBZ and checked against its
recorded SHA-256, so original bytes survive unchanged. Order follows the stored
entry order (and each entry's page order); chapters follow the source's chapter
grouping. Folder and file names suit Komga/Kavita libraries
(``<Series>/<Series> - <Chapter>.cbz`` with ComicInfo.xml).
"""
import datetime as dt
import hashlib
import html
import json
from pathlib import Path
import re
import uuid
import zipfile

from .downloads import comic_info
from .models import WebcomicAsset, WebcomicEntry

MODES = ('folders', 'chapter_cbz', 'series_cbz', 'epub')


def _safe(name, fallback='Untitled'):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', str(name or '')).strip(' .')[:120] or fallback


def _archived(db, source_id):
    """Entries with their verified image bytes, in reading order, grouped by chapter."""
    chapters = {row['chapter_key']: json.loads(row['metadata']).get('title') for row in db.execute(
        'SELECT chapter_key, metadata FROM webcomic_chapters WHERE source_id=?', (source_id,))}
    entries = []
    for entry in db.execute("""SELECT * FROM webcomic_entries WHERE source_id=? AND state='archived' AND removed=0
            ORDER BY ordinal, id""", (source_id,)).fetchall():
        assets = [json.loads(row['metadata']) for row in db.execute(
            'SELECT metadata FROM webcomic_assets WHERE entry_id=? AND revision=? ORDER BY ordinal',
            (entry['id'], entry['fingerprint']))]
        if not assets:
            continue
        pages = []
        with zipfile.ZipFile(assets[0]['local_path']) as archive:
            for asset in assets:
                data = archive.read(asset['archive_path'])
                if hashlib.sha256(data).hexdigest() != asset['checksum']:
                    raise ValueError(f"{asset['archive_path']} in {assets[0]['local_path']} no longer matches its checksum")
                pages.append((asset, data))
        metadata = json.loads(entry['metadata'])
        entries.append({'row': entry, 'metadata': metadata, 'pages': pages,
                        'chapter': chapters.get(entry['chapter_key']) or None})
    return entries


def _entry_object(metadata):
    fields = WebcomicEntry.__dataclass_fields__
    data = {key: value for key, value in metadata.items() if key in fields and key != 'assets'}
    return WebcomicEntry(**{'source_id': data.pop('source_id', ''), 'source_url': data.pop('source_url', ''),
                            'title': data.pop('title', ''), **data})


def _asset_objects(pages):
    fields = WebcomicAsset.__dataclass_fields__
    return [WebcomicAsset(**{key: value for key, value in asset.items() if key in fields}) for asset, _data in pages]


def _write_cbz(path, series, title, pages, chapter=None, entry=None):
    entry = entry or WebcomicEntry(title, '', title)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
        for index, (asset, data) in enumerate(pages, 1):
            archive.writestr(f'{index:04d}{Path(asset["archive_path"]).suffix}', data)
        archive.writestr('ComicInfo.xml', comic_info(series, entry, chapter, _asset_objects(pages)).encode('utf-8'))


def _write_epub(path, series, entries):
    pages = [(asset, data) for entry in entries for asset, data in entry['pages']]
    book_id = str(uuid.uuid5(uuid.NAMESPACE_URL, series.get('canonical_source_url') or series.get('title') or 'webcomic'))
    manifest, spine = [], []
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" '
                         'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile '
                         'full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        for index, (asset, data) in enumerate(pages, 1):
            suffix = Path(asset['archive_path']).suffix
            image, page = f'images/{index:04d}{suffix}', f'page{index:04d}.xhtml'
            media = asset.get('media_type') or {'.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'}.get(suffix, 'image/jpeg')
            width, height = asset.get('width') or 800, asset.get('height') or 1200
            archive.writestr(f'OEBPS/{image}', data)
            archive.writestr(f'OEBPS/{page}', (
                '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
                f'<meta name="viewport" content="width={width}, height={height}"/><title>Page {index}</title></head>'
                f'<body style="margin:0"><img src="{image}" alt="Page {index}" style="width:100%"/></body></html>'))
            manifest += [f'<item id="img{index}" href="{image}" media-type="{media}"/>',
                         f'<item id="p{index}" href="{page}" media-type="application/xhtml+xml"/>']
            spine.append(f'<itemref idref="p{index}"/>')
        archive.writestr('OEBPS/nav.xhtml', '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
                         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body>'
                         '<nav epub:type="toc"><ol><li><a href="page0001.xhtml">Start</a></li></ol></nav></body></html>')
        creators = ''.join(f'<dc:creator>{html.escape(c["name"])}</dc:creator>' for c in series.get('creators') or [])
        archive.writestr('OEBPS/content.opf', (
            '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="id">urn:uuid:{book_id}</dc:identifier><dc:title>{html.escape(series.get("title") or "Webcomic")}</dc:title>'
            f'{creators}<dc:language>{html.escape(series.get("language") or "en")}</dc:language>'
            f'<meta property="dcterms:modified">{dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</meta>'
            '<meta property="rendition:layout">pre-paginated</meta></metadata>'
            f'<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{"".join(manifest)}</manifest>'
            f'<spine>{"".join(spine)}</spine></package>'))


def export_source(db, source_id, destination, mode='chapter_cbz'):
    """Export one source; returns the list of files or folders written."""
    if mode not in MODES:
        raise ValueError(f'Choose one of {", ".join(MODES)}')
    source = db.execute('SELECT * FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()
    series = json.loads(db.execute('SELECT metadata FROM webcomic_series WHERE id=?', (source['series_id'],)).fetchone()[0])
    entries = _archived(db, source_id)
    if not entries:
        raise ValueError('Nothing archived to export yet')
    folder = Path(destination) / _safe(series.get('title'))
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    if mode == 'folders':
        for entry in entries:
            target = folder / _safe(entry['chapter'] or 'Ungrouped') / f"{int(entry['row']['ordinal']):05d} - {_safe(entry['metadata'].get('title'))}"
            target.mkdir(parents=True, exist_ok=True)
            for index, (asset, data) in enumerate(entry['pages'], 1):
                (target / f'{index:04d}{Path(asset["archive_path"]).suffix}').write_bytes(data)
            (target / 'ComicInfo.xml').write_text(comic_info(series, _entry_object(entry['metadata']),
                                                             {'title': entry['chapter']}, _asset_objects(entry['pages'])),
                                                  encoding='utf-8')
            written.append(target)
    elif mode == 'chapter_cbz':
        groups = {}
        for entry in entries:
            groups.setdefault(entry['chapter'] or 'Ungrouped', []).append(entry)
        for number, (chapter, members) in enumerate(groups.items(), 1):
            path = folder / f"{_safe(series.get('title'))} - Ch. {number:03d} - {_safe(chapter)}.cbz"
            pages = [page for member in members for page in member['pages']]
            _write_cbz(path, series, chapter, pages, {'title': chapter}, WebcomicEntry(str(number), '', chapter, number=str(number)))
            written.append(path)
    elif mode == 'series_cbz':
        path = folder / f"{_safe(series.get('title'))}.cbz"
        _write_cbz(path, series, series.get('title'), [page for entry in entries for page in entry['pages']])
        written.append(path)
    else:
        path = folder / f"{_safe(series.get('title'))}.epub"
        _write_epub(path, series, entries)
        written.append(path)
    write_opds(Path(destination))
    return written


def write_opds(root):
    """A static OPDS 2.0 catalog (catalog.json) of every exported file under `root`."""
    root = Path(root)
    publications = []
    for path in sorted(root.rglob('*')):
        if path.suffix.casefold() not in ('.cbz', '.epub') or not path.is_file():
            continue
        media = 'application/epub+zip' if path.suffix.casefold() == '.epub' else 'application/vnd.comicbook+zip'
        publications.append({'metadata': {'title': path.stem, '@type': 'http://schema.org/Book'},
                             'links': [{'rel': 'http://opds-spec.org/acquisition', 'type': media,
                                        'href': path.relative_to(root).as_posix()}]})
    catalog = {'metadata': {'title': 'Media Intake webcomics'},
               'links': [{'rel': 'self', 'href': 'catalog.json', 'type': 'application/opds+json'}],
               'publications': publications}
    (root / 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    return root / 'catalog.json'
