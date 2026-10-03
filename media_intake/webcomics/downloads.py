"""Shared queue archival worker: original bytes, ComicInfo and provenance manifest."""
import datetime as dt
import errno
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile
from ..core import check_download, file_name, update_download_progress
from ..serial import validate_manifest
from .images import CorruptImage, inspect_image
from .http import ExtractionError, HttpClient
from .models import WebcomicAsset, WebcomicEntry, record
from .resolver import registry_for
from .storage import event


def download_entry(db, row, root, max_bytes, destination=None, client=None):
    item = json.loads(row['metadata'])
    validate_manifest(item)
    job = item['webcomic_job']
    source = dict(db.execute('SELECT * FROM webcomic_sources WHERE id=?', (job['source_id'],)).fetchone())
    config = json.loads(source['config'])
    client = client or HttpClient(db, interval=config.get('request_interval', 1),
                                  user_agent=config.get('user_agent', 'MediaArchiveImporter/0.3'))
    adapter = registry_for(config).select(source['url'], adapter_id=source['adapter'])
    entry_data = dict(job['entry'])
    entry_data['assets'] = [WebcomicAsset(**asset) for asset in entry_data['assets']]
    entry = adapter.resolve_assets(WebcomicEntry(**entry_data), client, config)
    if not 1 <= len(entry.assets) <= 1000:
        raise ExtractionError('Entry must have 1 to 1000 image assets')
    folder = Path(destination) if destination else Path(root) / 'downloads'
    folder.mkdir(parents=True, exist_ok=True)
    check_disk_budget(db, folder)
    fd, temp = tempfile.mkstemp(prefix='.webcomic-', dir=folder)
    manifest = {'schema': 'media-intake/webcomic-archive/v1',
                'series': json.loads(db.execute('SELECT metadata FROM webcomic_series WHERE id=?',
                                               (source['series_id'],)).fetchone()[0]),
                'source': {'url': source['url'], 'adapter': adapter.id, 'version': adapter.version},
                'entry': record(entry), 'revision': job['revision'], 'assets': []}
    spent = 0
    try:
        with os.fdopen(fd, 'wb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_STORED) as archive:
            for ordinal, asset in enumerate(entry.assets, 1):
                if spent >= max_bytes:
                    raise ValueError('Entry exceeds archive byte budget')
                response = client.get(asset.source_url, max_bytes=min(25_000_000, max_bytes - spent),
                                      conditional=False, headers={'Referer': entry.source_url})
                media_type = response.headers.get('content-type', '').split(';', 1)[0].lower()
                try:
                    ext, width, height = inspect_image(response.body, media_type)
                except CorruptImage as exc:
                    # Usually a truncated transfer: fail this attempt (retryable), keep the source healthy.
                    event(db, source['id'], 'ASSET_REJECTED', entry.source_id,
                          {'url': asset.source_url, 'reason': str(exc), 'bytes': len(response.body)})
                    db.commit()
                    raise ValueError(f'Image {ordinal} rejected: {exc}') from exc
                spent += len(response.body)
                path = f'{ordinal:04d}{ext}'
                archive.writestr(path, response.body)
                asset.ordinal, asset.archive_path = ordinal, path
                asset.checksum = hashlib.sha256(response.body).hexdigest()
                asset.retrieved_at = dt.datetime.now(dt.timezone.utc).isoformat()
                asset.media_type = media_type or {'.jpg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'}[ext]
                asset.provenance.update({'requested_url': asset.source_url, 'final_url': response.url,
                                         'provider': adapter.id, 'adapter_version': adapter.version})
                asset.width, asset.height = width, height
                manifest['assets'].append(record(asset))
                update_download_progress(db, row['id'], spent, None, f'Archiving image {ordinal} of {len(entry.assets)}')
            manifest['entry'] = record(entry)
            chapter = db.execute('SELECT metadata FROM webcomic_chapters WHERE source_id=? AND chapter_key=?',
                                 (source['id'], entry.chapter_id)).fetchone()
            manifest['chapter'] = json.loads(chapter[0]) if chapter else None
            info = comic_info(manifest['series'], entry, manifest['chapter'], entry.assets)
            archive.writestr('ComicInfo.xml', info.encode('utf-8'))
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        if Path(temp).stat().st_size > max_bytes:
            raise ValueError('Entry exceeds archive byte budget')
        check_download(temp, '.cbz')
        with open(temp, 'rb') as stream:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
            digest = hasher.hexdigest()
        # Stable per-entry/revision path avoids collision across mirrors, renames,
        # numbered title duplicates, and interrupted retries.
        dest = folder / file_name({**item, 'title': item['title'] + f" [{job['entry_id']}-{job['revision'][:8]}]"})
        # Record the destination first: a crash after the move can then be reconciled.
        db.execute('UPDATE items SET file_path=? WHERE id=?', (str(dest.resolve()), row['id']))
        db.commit()
        os.replace(temp, dest)
        record_disk_usage(db, dest.stat().st_size)
        db.execute("UPDATE items SET state='downloaded',file_path=?,sha256=?,error=NULL WHERE id=?",
                   (str(dest.resolve()), digest, row['id']))
        for asset in entry.assets:
            db.execute('INSERT OR REPLACE INTO webcomic_assets VALUES(?,?,?,?)',
                       (job['entry_id'], job['revision'], asset.ordinal, json.dumps({**record(asset), 'local_path': str(dest.resolve())})))
        current = db.execute('SELECT fingerprint FROM webcomic_entries WHERE id=?', (job['entry_id'],)).fetchone()
        if current and current[0] == job['revision']:
            db.execute("UPDATE webcomic_entries SET state='archived',metadata=? WHERE id=?",
                       (json.dumps(record(entry), ensure_ascii=False), job['entry_id']))
        event(db, source['id'], 'ARCHIVED_ENTRY', entry.source_id,
              {'queue_id': row['id'], 'revision': job['revision'], 'sha256': digest})
        db.commit()
        return dict(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)):
            raise ValueError('The disk is full; free some space and retry (the partial archive was removed)') from exc
        raise
    except ExtractionError as exc:
        event(db, source['id'], 'PARSER_SUSPECTED_BROKEN', entry.source_id, {'error': str(exc)})
        db.execute("UPDATE webcomic_sources SET health='PARSER_SUSPECTED_BROKEN',last_failure=?,failures=failures+1 WHERE id=?",
                   (dt.datetime.now(dt.timezone.utc).isoformat(), source['id']))
        db.commit()
        raise
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


# ComicInfo.xml (Anansi Project v2) elements must follow the schema's sequence order.
COMIC_INFO_ORDER = ('Title', 'Series', 'Number', 'Summary', 'Notes', 'Year', 'Month', 'Day', 'Writer', 'Penciller',
                    'Inker', 'Colorist', 'Letterer', 'CoverArtist', 'Publisher', 'Genre', 'Web', 'PageCount',
                    'LanguageISO', 'ScanInformation', 'StoryArc')
ROLE_ELEMENTS = {'author': 'Writer', 'writer': 'Writer', 'creator': 'Writer', 'story': 'Writer',
                 'artist': 'Penciller', 'illustrator': 'Penciller', 'penciller': 'Penciller', 'art': 'Penciller',
                 'inker': 'Inker', 'colorist': 'Colorist', 'letterer': 'Letterer', 'cover': 'CoverArtist'}


def comic_info(series, entry, chapter, assets):
    """A schema-ordered ComicInfo.xml for one webcomic entry, with credits and page sizes."""
    series = series or {}
    credits = {}
    for creator in series.get('creators') or []:
        element = ROLE_ELEMENTS.get(str(creator.get('role') or '').casefold(), 'Writer')
        names = credits.setdefault(element, [])
        if creator.get('name') and creator['name'] not in names:
            names.append(creator['name'])
    published = str(entry.published_at or '')
    number = entry.number or (f'{entry.ordinal:g}' if isinstance(entry.ordinal, float) else entry.ordinal)
    values = {
        'Title': entry.title, 'Series': series.get('title'), 'Number': str(number) if number is not None else None,
        'Summary': entry.notes, 'Notes': f'Archived by Media Intake from {entry.source_url}',
        'Year': published[:4] if published[:4].isdigit() else None,
        'Month': str(int(published[5:7])) if published[5:7].isdigit() else None,
        'Day': str(int(published[8:10])) if published[8:10].isdigit() else None,
        **{element: ', '.join(names) for element, names in credits.items()},
        'Genre': ', '.join(series.get('tags') or []) or None, 'Web': entry.source_url,
        'PageCount': str(len(assets)), 'LanguageISO': series.get('language'),
        'StoryArc': (chapter or {}).get('title'),
    }
    body = ''.join(f'<{key}>{html.escape(str(values[key]))}</{key}>' for key in COMIC_INFO_ORDER if values.get(key))
    pages = ''.join(
        f'<Page Image="{index}"' + (' Type="FrontCover"' if index == 0 else '') +
        (f' ImageWidth="{asset.width}" ImageHeight="{asset.height}"' if asset.width and asset.height else '') +
        (f' ImageSize="{asset.size}"' if getattr(asset, 'size', None) else '') + '/>'
        for index, asset in enumerate(assets))
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<ComicInfo xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">' + body + f'<Pages>{pages}</Pages></ComicInfo>')


def _setting(db, key, default=0):
    row = db.execute('SELECT value FROM gui_settings WHERE key=?', (key,)).fetchone()
    try:
        return int(row['value']) if row else default
    except (TypeError, ValueError):
        return default


def check_disk_budget(db, folder, reserve=50 * 1024 * 1024):
    """Refuse new archives past the webcomic disk budget (MB; 0 = none) or when the disk is nearly full."""
    budget = _setting(db, 'webcomics_disk_budget_mb') * 1024 * 1024
    used = _setting(db, 'webcomics_bytes_archived')
    if budget and used >= budget:
        raise ValueError(f'Webcomic disk budget reached ({used // 1048576} of {budget // 1048576} MB); '
                         'raise it in Webcomics settings or free space')
    if shutil.disk_usage(folder).free < reserve:
        raise ValueError('The disk is nearly full; webcomic archiving is paused')


def record_disk_usage(db, size):
    used = _setting(db, 'webcomics_bytes_archived') + int(size)
    db.execute('INSERT INTO gui_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               ('webcomics_bytes_archived', str(used)))



def _archive_matches(path, revision):
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            return manifest.get('revision') == revision and archive.testzip() is None
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return False


def reconcile_archives(db, source_id=None):
    """Finish bookkeeping for archives written just before a crash; returns how many were reconciled."""
    reconciled = 0
    rows = db.execute("""SELECT i.id, i.file_path, i.metadata FROM items i
        WHERE i.state IN ('queued','downloading','failed') AND i.file_path IS NOT NULL
        AND json_extract(i.metadata, '$.webcomic_job') IS NOT NULL""").fetchall()
    for row in rows:
        job = json.loads(row['metadata'])['webcomic_job']
        if source_id is not None and job['source_id'] != source_id:
            continue
        path = Path(row['file_path'])
        if not path.is_file() or not _archive_matches(path, job['revision']):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        db.execute("UPDATE items SET state='downloaded', sha256=?, error=NULL, lease_until=NULL WHERE id=?",
                   (digest, row['id']))
        db.execute("UPDATE webcomic_entries SET state='archived', attempts=0, retry_after=NULL WHERE id=? AND fingerprint=?",
                   (job['entry_id'], job['revision']))
        event(db, job['source_id'], 'ARCHIVE_RECONCILED', job['entry']['source_id'], {'queue_id': row['id']})
        reconciled += 1
    db.commit()
    return reconciled


def repair_manifest(db, entry_id):
    """Rebuild a missing or damaged manifest.json inside an archive, keeping the original image bytes."""
    assets = [json.loads(row['metadata']) for row in db.execute(
        'SELECT metadata FROM webcomic_assets WHERE entry_id=? ORDER BY ordinal', (entry_id,))]
    if not assets:
        raise ValueError('No archived assets are recorded for this entry')
    path = Path(assets[0]['local_path'])
    entry = db.execute('SELECT * FROM webcomic_entries WHERE id=?', (entry_id,)).fetchone()
    source = db.execute('SELECT * FROM webcomic_sources WHERE id=?', (entry['source_id'],)).fetchone()
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        try:
            json.loads(archive.read('manifest.json'))
            return False  # already intact
        except (KeyError, ValueError):
            pass
        members = {name: archive.read(name) for name in names if name != 'manifest.json'}
    for asset in assets:
        data = members.get(asset.get('archive_path') or '')
        if data is None or hashlib.sha256(data).hexdigest() != asset.get('checksum'):
            raise ValueError(f"Image {asset.get('archive_path')} is missing or changed; the archive needs a fresh download")
    series = db.execute('SELECT metadata FROM webcomic_series WHERE id=?', (source['series_id'],)).fetchone()
    manifest = {'schema': 'media-intake/webcomic-archive/v1', 'repaired': True,
                'series': json.loads(series[0]) if series else {},
                'source': {'url': source['url'], 'adapter': source['adapter'], 'version': source['adapter_version']},
                'entry': json.loads(entry['metadata']), 'revision': entry['fingerprint'],
                'assets': [{key: value for key, value in asset.items() if key != 'local_path'} for asset in assets]}
    fd, temp = tempfile.mkstemp(prefix='.webcomic-repair-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_STORED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    event(db, source['id'], 'MANIFEST_REPAIRED', entry['entry_key'])
    db.commit()
    return True
