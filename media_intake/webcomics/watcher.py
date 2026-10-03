"""Incremental engine run by the existing watched_items scheduler, never a new timer."""
import datetime as dt
import json
import time
import threading
from .chapters import resolve_chapter
from .http import ExtractionError, HttpClient, SourceUnavailable
from .models import canonicalize_url, fingerprint, identity, record
from .resolver import registry_for, resolve
from .storage import event

_LOCKS_GUARD = threading.Lock()
_LOCKS = {}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def entry_signature(entry):
    data = record(entry)
    data.pop('ordinal', None)
    # Field evidence is durable but should not create content revisions itself.
    data.pop('provenance', None)
    for asset in data['assets']:
        for key in ('retrieved_at', 'checksum', 'archive_path', 'provenance'):
            asset.pop(key, None)
    return fingerprint(data)


def add_series(db, url, title=None, config=None, archive_existing=False, client=None):
    from ..watched import add_watch
    client = client or HttpClient(db)
    adapter, snapshot, config = resolve(url, client, config)
    source_url = snapshot.series.canonical_source_url
    existing = db.execute('SELECT * FROM webcomic_sources WHERE url=?', (source_url,)).fetchone()
    if existing is None:
        alias = db.execute('''SELECT s.* FROM webcomic_source_aliases a JOIN webcomic_sources s ON s.id=a.source_id
            WHERE a.alias_url IN (?, ?)''', (source_url, canonicalize_url(url))).fetchone()
        existing = alias
    if existing:
        saved_watch = db.execute('SELECT id FROM watched_items WHERE id=?', (existing['watch_id'],)).fetchone()
        if saved_watch:
            return int(saved_watch['id'])
        # Removing a watch keeps archived content. Re-adding reconnects the
        # durable source/cursor instead of losing its history or returning a dead ID.
        restored_id = add_watch(db, 'webcomic', snapshot.series.title,
            source_url=source_url, metadata={'webcomics': {'adapter': adapter.id},
            'library_profile_ids': config.get('library_profile_ids') or []})
        db.execute('UPDATE webcomic_sources SET watch_id=? WHERE id=?', (restored_id, existing['id']))
        db.commit()
        return restored_id
    if title:
        snapshot.series.title = title.strip()
        config['series_title'] = title.strip()
    creators = ', '.join(dict.fromkeys(item['name'] for item in snapshot.series.creators)) or 'Unknown'
    watch_id = add_watch(db, 'webcomic', snapshot.series.title, creators,
                         source_url=source_url, metadata={'webcomics': {'adapter': adapter.id},
                         'library_profile_ids': config.get('library_profile_ids') or []})
    config['archive_existing'] = bool(archive_existing)
    config.setdefault('auto_archive', True)
    config.setdefault('batch_size', 10)
    config.setdefault('poll_hours', 24)
    series_id = snapshot.series.canonical_id
    db.execute('INSERT OR IGNORE INTO webcomic_series(id,metadata) VALUES(?,?)',
               (series_id, json.dumps(record(snapshot.series), ensure_ascii=False)))
    cursor = db.execute('''INSERT INTO webcomic_sources(series_id,watch_id,url,adapter,adapter_version,config)
        VALUES(?,?,?,?,?,?)''', (series_id, watch_id, source_url, adapter.id, adapter.version, json.dumps(config)))
    source_id = cursor.lastrowid
    apply_snapshot(db, source_id, snapshot, baseline=not archive_existing)
    db.commit()
    return watch_id


def apply_snapshot(db, source_id, snapshot, baseline=False):
    source = dict(db.execute('SELECT * FROM webcomic_sources WHERE id=?', (source_id,)).fetchone())
    previous = json.loads(source['metrics'])
    count = len(snapshot.entries)
    if not snapshot.unchanged and (not count and not snapshot.metrics.get('incremental') or
            snapshot.complete and previous.get('entry_count', 0) >= 10 and count < previous['entry_count'] * 0.5):
        raise ExtractionError('PARSER_SUSPECTED_BROKEN: archive count collapsed; retained all stored entries')
    # Incremental APIs may legitimately return no entries. An unchanged index
    # does not imply that pending downloads are completed.
    old_series = db.execute('SELECT metadata FROM webcomic_series WHERE id=?', (source['series_id'],)).fetchone()
    config = json.loads(source['config'])
    if config.get('series_title'):
        snapshot.series.title = config['series_title']
    moved_to = snapshot.series.canonical_source_url
    if moved_to and moved_to != source['url']:
        if db.execute('INSERT OR IGNORE INTO webcomic_source_aliases(alias_url, source_id) VALUES(?,?)',
                      (moved_to, source_id)).rowcount:
            event(db, source_id, 'SOURCE_MOVED', detail={'from': source['url'], 'to': moved_to})
    # Accepted catalog matches and user locks survive every refresh.
    from .metadata_bridge import apply_enrichment
    apply_enrichment(db, source['series_id'], snapshot.series)
    series_data = record(snapshot.series)
    if old_series:
        old_data = json.loads(old_series['metadata'])
        for field in ('last_checked_at', 'last_successful_update'):
            series_data[field] = old_data.get(field)
        # Canonical identity survives redirect/mirror changes.
        series_data['canonical_id'] = source['series_id']
        if {k: v for k, v in old_data.items() if not k.startswith('last_')} != {
                k: v for k, v in series_data.items() if not k.startswith('last_')}:
            event(db, source_id, 'METADATA_CHANGED', detail={'before': old_data, 'after': series_data})
    series_data['last_checked_at'] = now()
    overrides = {(row['entry_key'], row['field']): json.loads(row['value']) for row in
                 db.execute('SELECT * FROM webcomic_overrides WHERE source_id=?', (source_id,))}
    chapters = {chapter.source_id: chapter for chapter in snapshot.chapters}
    max_ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0) FROM webcomic_entries WHERE source_id=?', (source_id,)).fetchone()[0]
    # Entries from older archive pages found after newer ones go before everything
    # already stored; the source is renumbered afterwards to keep reading order.
    existing_keys = {row[0] for row in db.execute('SELECT entry_key FROM webcomic_entries WHERE source_id=?', (source_id,))}
    backfill = [entry.source_id for entry in snapshot.entries
                if entry.provenance.get('backfill') and entry.source_id not in existing_keys]
    prepend = {}
    if backfill and existing_keys:
        min_ordinal = db.execute('SELECT COALESCE(MIN(ordinal),1) FROM webcomic_entries WHERE source_id=?', (source_id,)).fetchone()[0]
        prepend = {key: min_ordinal - len(backfill) + index - 1 for index, key in enumerate(backfill)}
    backfill_state = 'pending' if config.get('archive_existing') else 'known'
    known = set()
    for entry in snapshot.entries:
        known.add(entry.source_id)
        for field in ('title', 'content_type'):
            if (entry.source_id, field) in overrides:
                setattr(entry, field, overrides[entry.source_id, field])
        if (entry.source_id, 'chapter') in overrides:
            chapter = resolve_chapter(entry.title, entry.source_url, override=overrides[entry.source_id, 'chapter'])
            entry.chapter_id = chapter.source_id if chapter else None
            if chapter:
                chapters[chapter.source_id] = chapter
        previous_entry = db.execute('SELECT * FROM webcomic_entries WHERE source_id=? AND entry_key=?',
                                    (source_id, entry.source_id)).fetchone()
        if previous_entry:
            entry.ordinal = previous_entry['ordinal']
        elif entry.source_id in prepend:
            entry.ordinal = prepend[entry.source_id]
        else:
            max_ordinal += 1
            entry.ordinal = max_ordinal
        signature = entry_signature(entry)
        state = 'known' if baseline else 'pending'
        if not previous_entry and not baseline and (entry.provenance.get('backfill') or
                                                    entry.provenance.get('archive_continuation')):
            # Older entries follow the source's "archive existing" choice.
            state = backfill_state
        if previous_entry:
            state = previous_entry['state']
            if signature != previous_entry['fingerprint']:
                old = json.loads(previous_entry['metadata'])
                if entry.title != old['title']:
                    event(db, source_id, 'RENAMED_ENTRY', entry.source_id, {'title': entry.title})
                if entry.chapter_id != previous_entry['chapter_key']:
                    event(db, source_id, 'CHAPTER_REASSIGNED', entry.source_id, {'chapter_id': entry.chapter_id})
                event(db, source_id, 'UPDATED_ENTRY', entry.source_id, {'revision': signature})
                state = 'pending' if not baseline else state
            else:
                # Keep richer downloaded metadata; the index may contain only a title.
                if previous_entry['removed']:
                    db.execute('UPDATE webcomic_entries SET removed=0 WHERE id=?', (previous_entry['id'],))
                    event(db, source_id, 'RESTORED_ENTRY', entry.source_id)
                continue
        else:
            event(db, source_id, 'BASELINE_ENTRY' if baseline else 'NEW_ENTRY', entry.source_id)
            series_data['last_successful_update'] = now()
        db.execute('''INSERT INTO webcomic_entries(source_id,entry_key,url,ordinal,published_at,chapter_key,metadata,fingerprint,state)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,entry_key) DO UPDATE SET url=excluded.url,
            published_at=excluded.published_at,chapter_key=excluded.chapter_key,metadata=excluded.metadata,
            fingerprint=excluded.fingerprint,state=excluded.state,removed=0''',
            (source_id, entry.source_id, entry.source_url, entry.ordinal, entry.published_at, entry.chapter_id,
             json.dumps(record(entry), ensure_ascii=False), signature, state))
    for chapter in chapters.values():
        exists = db.execute('SELECT 1 FROM webcomic_chapters WHERE source_id=? AND chapter_key=?',
                            (source_id, chapter.source_id)).fetchone()
        if not exists:
            event(db, source_id, 'CHAPTER_ADDED', detail=record(chapter))
        db.execute('INSERT INTO webcomic_chapters VALUES(?,?,?) ON CONFLICT(source_id,chapter_key) DO UPDATE SET metadata=excluded.metadata',
                   (source_id, chapter.source_id, json.dumps(record(chapter))))
    for volume in snapshot.volumes:
        db.execute('INSERT INTO webcomic_volumes VALUES(?,?,?) ON CONFLICT(source_id,volume_key) DO UPDATE SET metadata=excluded.metadata',
                   (source_id, volume.source_id, json.dumps(record(volume))))
    if prepend:
        rows = db.execute('SELECT id FROM webcomic_entries WHERE source_id=? ORDER BY ordinal, id', (source_id,)).fetchall()
        db.executemany('UPDATE webcomic_entries SET ordinal=? WHERE id=?',
                       [(index, row['id']) for index, row in enumerate(rows, 1)])
        event(db, source_id, 'ARCHIVE_BACKFILLED', detail={'entries': len(prepend)})
    if snapshot.complete and not snapshot.unchanged:
        for row in db.execute('SELECT entry_key FROM webcomic_entries WHERE source_id=? AND removed=0', (source_id,)):
            if row['entry_key'] not in known:
                event(db, source_id, 'REMOVED_ENTRY', row['entry_key'], {'review_required': True})
                db.execute('UPDATE webcomic_entries SET removed=1 WHERE source_id=? AND entry_key=?', (source_id, row['entry_key']))
    cursor = {**json.loads(source['cursor']), **snapshot.cursor, 'last_successful_poll': now(),
              'source_adapter_version': source['adapter_version']}
    if snapshot.entries:
        latest = max(snapshot.entries, key=lambda entry: entry.ordinal or 0)
        cursor.update({'latest_source_entry_id': latest.source_id, 'latest_canonical_ordinal': latest.ordinal,
                       'latest_publication_timestamp': latest.published_at,
                       'archive_fingerprint': fingerprint(sorted(known))})
    db.execute('UPDATE webcomic_series SET metadata=? WHERE id=?', (json.dumps(series_data), source['series_id']))
    db.execute('UPDATE watched_items SET title=?,author=? WHERE id=?',
               (series_data['title'], ', '.join(dict.fromkeys(item['name'] for item in series_data['creators'])) or 'Unknown', source['watch_id']))
    db.execute('UPDATE webcomic_sources SET cursor=?,metrics=?,health=?,last_success=?,failures=0 WHERE id=?',
               (json.dumps(cursor), json.dumps(snapshot.metrics), 'OK', now(), source_id))


def queue_pending(db, source, limit=10, entry_ids=None):
    from ..core import enqueue
    from ..serial import validate_manifest
    watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (source['watch_id'],)).fetchone())
    config = json.loads(source['config'])
    queued = []
    rows = db.execute("SELECT * FROM webcomic_entries WHERE source_id=? AND state='pending' AND removed=0 ORDER BY ordinal,id LIMIT ?",
                      (source['id'], max(1, min(limit, 100)) if entry_ids is None else 100000)).fetchall()
    if entry_ids is not None:
        wanted = set(entry_ids)
        rows = [row for row in rows if row['id'] in wanted][:max(1, min(limit, 100))]
    for row in rows:
        entry = json.loads(row['metadata'])
        chapter = {'title': entry['title'], 'url': entry['source_url']}
        if entry['assets']:
            chapter['images'] = [asset['source_url'] for asset in entry['assets']]
        manifest = validate_manifest({'kind': 'webcomic', 'title': watch['title'] + ' - ' + entry['title'],
            'author': watch['author'], 'series': watch['title'], 'source': source['url'],
            'chapters': [chapter], 'series_index': row['ordinal'], 'selector': '#comic,.comic,article,main'})
        manifest.update({'url': 'serial+https://webcomic/' + identity(f"{source['id']}:{row['entry_key']}:{row['fingerprint']}"),
            'webcomic_job': {'source_id': source['id'], 'entry_id': row['id'],
                            'revision': row['fingerprint'], 'entry': entry}, 'watch_id': source['watch_id'],
            'source_key': 'comic_rss_atom'})
        if config.get('rights_evidence_path'):
            manifest['rights_evidence_path'] = config['rights_evidence_path']
        from ..rights_evidence import watch_rights_key
        manifest['rights_work_key'] = watch_rights_key(watch)
        job = enqueue(db, manifest)
        db.execute("UPDATE webcomic_entries SET queue_id=?,state='queued' WHERE id=?", (job['id'], row['id']))
        queued.append(job['id'])
    db.commit()
    return queued


def poll_source(db, source_id, client=None):
    source = dict(db.execute('SELECT * FROM webcomic_sources WHERE id=?', (source_id,)).fetchone())
    config = json.loads(source['config'])
    registry = registry_for(config)
    adapter = registry.select(source['url'], adapter_id=source['adapter'])
    client = client or HttpClient(db, interval=config.get('request_interval', 1),
                                  user_agent=config.get('user_agent', 'MediaArchiveImporter/0.3'))
    try:
        snapshot = adapter.get_updates_since(source['url'], client, json.loads(source['cursor']), config)
        if adapter.version != source['adapter_version']:
            event(db, source_id, 'SOURCE_LAYOUT_CHANGED', detail={'previous': source['adapter_version'], 'current': adapter.version})
            db.execute('UPDATE webcomic_sources SET adapter_version=? WHERE id=?', (adapter.version, source_id))
        apply_snapshot(db, source_id, snapshot)
        db.commit()
        client.prune() if hasattr(client, 'prune') else None
        if config.get('auto_archive', True):
            return queue_pending(db, source, int(config.get('batch_size', 10)))
        # Automation off: only a requested one-time backlog job is queued.
        from .controls import backlog_entries
        batch = int(config.get('batch_size', 10))
        return queue_pending(db, source, batch, entry_ids=backlog_entries(db, source_id, batch))
    except Exception as exc:
        db.rollback()
        health = 'PARSER_SUSPECTED_BROKEN' if isinstance(exc, (ExtractionError, ValueError, KeyError, TypeError)) else 'SOURCE_UNREACHABLE'
        event(db, source_id, health, detail={'error': str(exc)})
        if health == 'PARSER_SUSPECTED_BROKEN':
            event(db, source_id, 'SOURCE_LAYOUT_CHANGED', detail={'error': str(exc), 'review_required': True})
        db.execute('UPDATE webcomic_sources SET health=?,last_failure=?,failures=failures+1 WHERE id=?', (health, now(), source_id))
        db.commit()
        raise


def process_watch(db, watch, library_folder, source_options=None):
    from ..core import download
    source = db.execute('SELECT * FROM webcomic_sources WHERE watch_id=?', (watch['id'],)).fetchone()
    if not source:
        raise ValueError('Webcomic source state is missing')
    source = dict(source)
    config = json.loads(source['config'])
    if config.get('paused'):
        return  # paused sources are never checked
    database_file = db.execute('PRAGMA database_list').fetchone()['file']
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault((database_file, source['id']), threading.Lock())
    if not lock.acquire(blocking=False):
        return
    if not claim_source(db, source['id']):
        lock.release()
        return  # another app window is checking this source right now
    hours = max(1, min(168, float(config.get('poll_hours', 24))))
    error = None
    try:
        from .downloads import reconcile_archives
        reconcile_archives(db, source['id'])
        poll_source(db, source['id'])
        backlog_until = config.get('backlog_until_ordinal')
        if config.get('auto_archive', True) or backlog_until:
            # Automation off: download only the one-time backlog (entries known when it was requested).
            ceiling = float('inf') if config.get('auto_archive', True) else float(backlog_until)
            pending = db.execute('''SELECT e.id, e.queue_id, e.attempts FROM webcomic_entries e JOIN items i ON i.id=e.queue_id
                WHERE e.source_id=? AND e.state='queued' AND i.state IN ('queued','failed','downloaded')
                AND (e.retry_after IS NULL OR e.retry_after <= ?) AND e.ordinal <= ?
                ORDER BY e.ordinal,e.id LIMIT ?''', (source['id'], time.time(), ceiling,
                                                     int(config.get('batch_size', 10)))).fetchall()
            download_errors = []
            for row in pending:
                try:
                    download(db, row['queue_id'], database_file and __import__('pathlib').Path(database_file).parent or library_folder,
                             destination=library_folder)
                    db.execute('UPDATE webcomic_entries SET attempts=0, retry_after=NULL WHERE id=?', (row['id'],))
                except Exception as exc:
                    download_errors.append(f"Queue {row['queue_id']}: {exc}")
                    record_entry_failure(db, source['id'], row['id'], exc)
            if download_errors:
                error = '; '.join(download_errors[:3])
        run_scheduled_audit(db, source['id'], config)
    except Exception as exc:
        error = str(exc)
        failures = db.execute('SELECT failures FROM webcomic_sources WHERE id=?', (source['id'],)).fetchone()[0]
        hours = min(168, hours * 2 ** min(failures, 3))
        if isinstance(exc, SourceUnavailable) and exc.retry_after:
            hours = max(hours, exc.retry_after / 3600)
    finally:
        timestamp = dt.datetime.now()
        db.execute("UPDATE watched_items SET state='watching',last_checked_at=?,next_check_at=?,last_error=? WHERE id=?",
                   (timestamp.isoformat(), (timestamp + dt.timedelta(hours=hours)).isoformat(), error, watch['id']))
        db.execute('UPDATE webcomic_sources SET lease_until=NULL WHERE id=?', (source['id'],))
        db.commit()
        lock.release()


SOURCE_LEASE_SECONDS = 1800
MAX_ENTRY_ATTEMPTS = 6


def claim_source(db, source_id):
    """Atomically take the source for this process; False while another holds a live lease."""
    now = time.time()
    claimed = db.execute('UPDATE webcomic_sources SET lease_until=? WHERE id=? AND (lease_until IS NULL OR lease_until < ?)',
                         (now + SOURCE_LEASE_SECONDS, source_id, now)).rowcount
    db.commit()
    return bool(claimed)


def record_entry_failure(db, source_id, entry_id, exc):
    """Back off a failing entry (10 min doubling, capped at a day); give up after MAX_ENTRY_ATTEMPTS."""
    row = db.execute('SELECT entry_key, attempts FROM webcomic_entries WHERE id=?', (entry_id,)).fetchone()
    attempts = int(row['attempts'] or 0) + 1
    if attempts >= MAX_ENTRY_ATTEMPTS:
        db.execute("UPDATE webcomic_entries SET attempts=?, retry_after=NULL, state='failed' WHERE id=?", (attempts, entry_id))
        event(db, source_id, 'ENTRY_GAVE_UP', row['entry_key'], {'attempts': attempts, 'error': str(exc)[:300],
                                                                  'review_required': True})
    else:
        delay = min(86400, 600 * 2 ** (attempts - 1))
        db.execute('UPDATE webcomic_entries SET attempts=?, retry_after=? WHERE id=?', (attempts, time.time() + delay, entry_id))
    db.commit()


def retry_failed_entries(db, source_id):
    """Put entries that gave up back in line (the user asked to try again)."""
    changed = db.execute("""UPDATE webcomic_entries SET state='queued', attempts=0, retry_after=NULL
        WHERE source_id=? AND state='failed' AND queue_id IS NOT NULL""", (source_id,)).rowcount
    db.commit()
    return changed


AUDIT_EVERY_DAYS = 7


def run_scheduled_audit(db, source_id, config, client=None):
    """Revalidate a few archived entries about once a week (audit_sample 0 turns it off)."""
    sample = int(config.get('audit_sample', 3))
    every = float(config.get('audit_every_days', AUDIT_EVERY_DAYS)) * 86400
    last = float(config.get('last_audit_at') or 0)
    if sample <= 0 or time.time() - last < every:
        return None
    from .audit import audit_source
    client = client or HttpClient(db, interval=config.get('request_interval', 1),
                                  user_agent=config.get('user_agent', 'MediaArchiveImporter/0.3'))
    try:
        outcome = audit_source(db, source_id, client, sample)
    except Exception as exc:
        event(db, source_id, 'AUDIT_FAILED', detail={'error': str(exc)[:300]})
        outcome = None
    stored = json.loads(db.execute('SELECT config FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()[0])
    stored['last_audit_at'] = time.time()
    db.execute('UPDATE webcomic_sources SET config=? WHERE id=?', (json.dumps(stored), source_id))
    db.commit()
    return outcome
