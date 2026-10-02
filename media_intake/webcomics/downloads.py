"""Shared queue archival worker: original bytes, ComicInfo and provenance manifest."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile
from ..core import check_download, file_name, update_download_progress
from ..serial import _comic_info, _image_extension, validate_manifest
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
                ext = _image_extension(response.body)
                media_type = response.headers.get('content-type', '').split(';', 1)[0].lower()
                if not ext or media_type and not media_type.startswith('image/'):
                    raise ExtractionError('Source returned an unsupported image')
                spent += len(response.body)
                path = f'{ordinal:04d}{ext}'
                archive.writestr(path, response.body)
                asset.ordinal, asset.archive_path = ordinal, path
                asset.checksum = hashlib.sha256(response.body).hexdigest()
                asset.retrieved_at = dt.datetime.now(dt.timezone.utc).isoformat()
                asset.media_type = media_type or {'.jpg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp'}[ext]
                asset.provenance.update({'requested_url': asset.source_url, 'final_url': response.url,
                                         'provider': adapter.id, 'adapter_version': adapter.version})
                if ext == '.png' and len(response.body) >= 24:
                    asset.width = int.from_bytes(response.body[16:20], 'big')
                    asset.height = int.from_bytes(response.body[20:24], 'big')
                manifest['assets'].append(record(asset))
                update_download_progress(db, row['id'], spent, None, f'Archiving image {ordinal} of {len(entry.assets)}')
            manifest['entry'] = record(entry)
            chapter = db.execute('SELECT metadata FROM webcomic_chapters WHERE source_id=? AND chapter_key=?',
                                 (source['id'], entry.chapter_id)).fetchone()
            manifest['chapter'] = json.loads(chapter[0]) if chapter else None
            comic_item = {**item, 'title': entry.title, 'source': entry.source_url}
            info = _comic_info(comic_item)
            if info:
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
        os.replace(temp, dest)
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
    except ExtractionError as exc:
        event(db, source['id'], 'PARSER_SUSPECTED_BROKEN', entry.source_id, {'error': str(exc)})
        db.execute("UPDATE webcomic_sources SET health='PARSER_SUSPECTED_BROKEN',last_failure=?,failures=failures+1 WHERE id=?",
                   (dt.datetime.now(dt.timezone.utc).isoformat(), source['id']))
        db.commit()
        raise
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
