"""Parser health for webcomic sources: metrics history, early warnings and metadata reprocessing.

Each successful check appends a small metrics sample (entry count, images per
entry). Warnings are computed before the next check so a layout change is
visible early: a collapsing entry count, fewer images per entry, repeated
failures, layout-change or rejected-image events. When an adapter improves,
``reprocess_metadata`` refreshes entry metadata in place without downloading
anything again.
"""
import json
import time

HISTORY = 30


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(webcomic_sources)')}
    if 'metrics_history' not in columns:
        db.execute("ALTER TABLE webcomic_sources ADD COLUMN metrics_history TEXT NOT NULL DEFAULT '[]'")


def _images_per_entry(db, source_id):
    row = db.execute("""SELECT COUNT(*), SUM(json_array_length(json_extract(metadata, '$.assets')))
        FROM webcomic_entries WHERE source_id=? AND removed=0 AND json_array_length(json_extract(metadata, '$.assets')) > 0""",
                     (source_id,)).fetchone()
    return round(row[1] / row[0], 2) if row and row[0] else None


def record_sample(db, source_id, snapshot_metrics):
    """Append one metrics sample after a successful check."""
    row = db.execute('SELECT metrics_history FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()
    history = json.loads(row[0] or '[]') if row else []
    entries = db.execute('SELECT COUNT(*) FROM webcomic_entries WHERE source_id=? AND removed=0', (source_id,)).fetchone()[0]
    history.append({'at': time.time(), 'entries': entries, 'snapshot_entries': snapshot_metrics.get('entry_count'),
                    'images_per_entry': _images_per_entry(db, source_id)})
    db.execute('UPDATE webcomic_sources SET metrics_history=? WHERE id=?', (json.dumps(history[-HISTORY:]), source_id))


def warnings(db, source_id):
    """Human-readable warnings for a source, worst first."""
    source = db.execute('SELECT * FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()
    history = json.loads(source['metrics_history'] or '[]')
    found = []
    if source['failures'] and source['failures'] >= 2:
        found.append(f"{source['failures']} checks in a row failed ({source['health']})")
    seen = [sample['snapshot_entries'] for sample in history if sample.get('snapshot_entries')]
    if len(seen) >= 2 and seen[-1] < max(seen) * 0.7:
        found.append(f'The site now lists {seen[-1]} entries, down from {max(seen)}; its layout may have changed')
    ratios = [sample['images_per_entry'] for sample in history if sample.get('images_per_entry')]
    if len(ratios) >= 3 and ratios[-1] < (sum(ratios[:-1]) / len(ratios[:-1])) * 0.6:
        found.append(f'Fewer images per entry than usual ({ratios[-1]} vs {sum(ratios[:-1]) / len(ratios[:-1]):.1f})')
    week = time.time() - 7 * 86400
    counts = dict(db.execute("""SELECT event_type, COUNT(*) FROM webcomic_events WHERE source_id=?
        AND created_at >= datetime(?, 'unixepoch') GROUP BY event_type""", (source_id, week)).fetchall())
    for kind, label in (('PARSER_SUSPECTED_BROKEN', 'parser problems'), ('SOURCE_LAYOUT_CHANGED', 'layout changes'),
                        ('ASSET_REJECTED', 'rejected images'), ('ENTRY_GAVE_UP', 'entries that gave up'),
                        ('SOURCE_MOVED', 'address changes')):
        if counts.get(kind):
            found.append(f'{counts[kind]} {label} this week')
    return found


def summary(db, source_id):
    source = db.execute('SELECT * FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()
    events = [dict(row) for row in db.execute(
        'SELECT event_type, entry_key, detail, created_at FROM webcomic_events WHERE source_id=? ORDER BY id DESC LIMIT 40',
        (source_id,))]
    return {'health': source['health'], 'failures': source['failures'], 'last_success': source['last_success'],
            'last_failure': source['last_failure'], 'adapter': source['adapter'], 'adapter_version': source['adapter_version'],
            'history': json.loads(source['metrics_history'] or '[]'), 'warnings': warnings(db, source_id), 'events': events}


def reprocess_metadata(db, source_id, client=None):
    """Re-read the source with the current adapter and refresh entry metadata, without any downloads."""
    from .http import HttpClient
    from .models import record
    from .resolver import registry_for
    from .storage import event
    from .watcher import entry_signature
    source = db.execute('SELECT * FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()
    config = json.loads(source['config'])
    adapter = registry_for(config).select(source['url'], adapter_id=source['adapter'])
    client = client or HttpClient(db)
    snapshot = adapter.resolve_series(source['url'], client, json.loads(source['cursor']), config)
    updated = 0
    for entry in snapshot.entries:
        row = db.execute('SELECT id, metadata, fingerprint FROM webcomic_entries WHERE source_id=? AND entry_key=?',
                         (source_id, entry.source_id)).fetchone()
        if row is None:
            continue
        old = json.loads(row['metadata'])
        entry.ordinal = old.get('ordinal')
        signature = entry_signature(entry)  # what the next ordinary check will compute
        data = record(entry)
        if not entry.assets:
            data['assets'] = old.get('assets') or []  # listing pages carry no images; keep the archived ones
        if data == old and signature == row['fingerprint']:
            continue
        # New fingerprint, unchanged state: nothing is marked for download, and archived image
        # records follow the entry to its new fingerprint.
        db.execute('UPDATE webcomic_entries SET metadata=?, fingerprint=? WHERE id=?',
                   (json.dumps(data, ensure_ascii=False), signature, row['id']))
        db.execute('UPDATE OR IGNORE webcomic_assets SET revision=? WHERE entry_id=? AND revision=?',
                   (signature, row['id'], row['fingerprint']))
        updated += 1
    db.execute('UPDATE webcomic_sources SET adapter_version=? WHERE id=?', (adapter.version, source_id))
    event(db, source_id, 'METADATA_REPROCESSED', detail={'entries': updated, 'adapter_version': adapter.version})
    db.commit()
    return updated


def estimate_next_release(db, source_id, recent=12):
    """An inferred next-release date from the median gap between recent publication dates.

    Returns {'estimated_next', 'cadence_days', 'based_on'} or None; always labelled as an estimate.
    """
    import datetime as dt
    import statistics
    dates = []
    for (value,) in db.execute("""SELECT published_at FROM webcomic_entries WHERE source_id=? AND removed=0
            AND published_at IS NOT NULL ORDER BY published_at DESC LIMIT ?""", (source_id, recent + 1)):
        try:
            dates.append(dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')[:19]))
        except ValueError:
            continue
    if len(dates) < 4:
        return None
    gaps = [(newer - older).total_seconds() / 86400 for newer, older in zip(dates, dates[1:]) if newer > older]
    if len(gaps) < 3:
        return None
    cadence = statistics.median(gaps)
    if cadence <= 0 or cadence > 120:
        return None
    return {'estimated_next': (dates[0] + dt.timedelta(days=cadence)).date().isoformat(),
            'cadence_days': round(cadence, 1), 'based_on': len(gaps), 'label': 'estimate'}
