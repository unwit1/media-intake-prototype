"""Per-source health: recent download and search outcomes, speed and the latest error.

Every queue download and every watched-item source search records one event.
summaries() turns the recent history into a status per source:

- failing:  the last FAILING_STREAK events all failed, or the last FAILING_STREAK
            downloads did (a source that searches fine but cannot deliver files)
- degraded: under half of recent attempts succeeded (with enough attempts to judge)
- healthy:  otherwise, once the source has succeeded at least once
- untested: no successful event yet and not failing

History is capped per source so the table stays small.
"""
import statistics

KEEP_PER_SOURCE = 200
RECENT = 20
FAILING_STREAK = 3
DEGRADED_MIN_ATTEMPTS = 4


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS source_events (
        id INTEGER PRIMARY KEY, source_key TEXT NOT NULL, kind TEXT NOT NULL,
        ok INTEGER NOT NULL, bytes INTEGER, seconds REAL, detail TEXT,
        at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('CREATE INDEX IF NOT EXISTS source_events_key ON source_events(source_key, id)')


def record(db, source_key, kind, ok, *, bytes=None, seconds=None, detail=None):
    """Store one outcome; never raises (health tracking must not break a download)."""
    source_key = str(source_key or '').strip().casefold()
    if not source_key:
        return
    try:
        db.execute('INSERT INTO source_events(source_key, kind, ok, bytes, seconds, detail) VALUES(?,?,?,?,?,?)',
                   (source_key, kind, 1 if ok else 0, bytes, seconds, str(detail)[:500] if detail else None))
        db.execute('''DELETE FROM source_events WHERE source_key=? AND id NOT IN (
            SELECT id FROM source_events WHERE source_key=? ORDER BY id DESC LIMIT ?)''',
                   (source_key, source_key, KEEP_PER_SOURCE))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _streak_failed(events):
    return len(events) >= FAILING_STREAK and not any(event['ok'] for event in events[:FAILING_STREAK])


def _status(recent, recent_downloads):
    if _streak_failed(recent) or _streak_failed(recent_downloads):
        return 'failing'
    attempts = len(recent)
    successes = sum(event['ok'] for event in recent)
    if attempts >= DEGRADED_MIN_ATTEMPTS and successes / attempts < 0.5:
        return 'degraded'
    return 'healthy' if successes else 'untested'


def summary(db, source_key):
    rows = [dict(row) for row in db.execute(
        'SELECT * FROM source_events WHERE source_key=? ORDER BY id DESC LIMIT ?',
        (source_key, KEEP_PER_SOURCE))]
    recent = rows[:RECENT]
    downloads = [row for row in recent if row['kind'] == 'download']
    all_downloads = [row for row in rows if row['kind'] == 'download']
    speeds = [row['bytes'] / row['seconds'] for row in downloads
              if row['ok'] and row['bytes'] and row['seconds'] and row['seconds'] > 0]
    last_ok = next((row for row in rows if row['ok']), None)
    last_error = next((row for row in rows if not row['ok']), None)
    streak = 0
    for row in rows:
        if row['ok']:
            break
        streak += 1
    return {
        'source_key': source_key, 'status': _status(recent, all_downloads),
        'recent_attempts': len(recent), 'recent_successes': sum(row['ok'] for row in recent),
        'downloads_ok': sum(row['ok'] for row in downloads),
        'downloads_failed': sum(not row['ok'] for row in downloads),
        'searches_ok': sum(row['ok'] for row in recent if row['kind'] == 'search'),
        'searches_failed': sum(not row['ok'] for row in recent if row['kind'] == 'search'),
        'median_speed': statistics.median(speeds) if speeds else None,
        'last_ok_at': last_ok['at'] if last_ok else None,
        'last_error': last_error['detail'] if last_error else None,
        'last_error_at': last_error['at'] if last_error else None,
        'failure_streak': streak,
    }


def summaries(db):
    """One summary per source with any history, worst first."""
    keys = [row[0] for row in db.execute('SELECT DISTINCT source_key FROM source_events')]
    order = {'failing': 0, 'degraded': 1, 'untested': 2, 'healthy': 3}
    return sorted((summary(db, key) for key in keys),
                  key=lambda item: (order[item['status']], item['source_key']))


def clear(db, source_key=None):
    if source_key:
        db.execute('DELETE FROM source_events WHERE source_key=?', (str(source_key).casefold(),))
    else:
        db.execute('DELETE FROM source_events')
    db.commit()
