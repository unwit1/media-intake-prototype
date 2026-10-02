"""Bulk watched-series intake using the existing catalog and graph import path.

Batches are persisted so an interrupted 100-series add resumes after a restart,
can be paused, cancelled, or retried, and keeps each series' outcome visible.
"""
import datetime as dt
import json

from .watched import add_from_goodreads_metadata

BATCH_LIMIT = 100
# A claimed series that has not finished within this window is assumed to belong
# to a crashed process and may be claimed again.
CLAIM_LEASE_SECONDS = 15 * 60
ACTIVE_BATCH_STATES = ('pending', 'running', 'paused')


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS book_batches (
        id INTEGER PRIMARY KEY, state TEXT NOT NULL DEFAULT 'pending',
        profile_id TEXT, sources TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS book_batch_items (
        id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL, position INTEGER NOT NULL,
        title TEXT NOT NULL, record TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        watch_id INTEGER, books_added INTEGER NOT NULL DEFAULT 0, error TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, claimed_at TEXT,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(batch_id, position), FOREIGN KEY(batch_id) REFERENCES book_batches(id))''')
    db.execute('CREATE INDEX IF NOT EXISTS book_batch_items_state ON book_batch_items(batch_id,state)')


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def _batch_state(db, batch_id):
    row = db.execute('SELECT state FROM book_batches WHERE id=?', (batch_id,)).fetchone()
    if not row:
        raise ValueError(f'Unknown book batch {batch_id}')
    return row['state']


def _set_batch_state(db, batch_id, state, finished=False):
    db.execute('UPDATE book_batches SET state=?, updated_at=?, finished_at=? WHERE id=?',
               (state, _now(), _now() if finished else None, batch_id))
    db.commit()


def create_book_batch(db, records, sources=None, profile_id=None):
    records = list(records)
    if not 1 <= len(records) <= BATCH_LIMIT:
        raise ValueError(f'Choose between 1 and {BATCH_LIMIT} book series')
    cursor = db.execute('INSERT INTO book_batches(profile_id, sources) VALUES(?,?)',
                        (profile_id or None, json.dumps(sources or {}, ensure_ascii=False)))
    batch_id = cursor.lastrowid
    db.executemany('INSERT INTO book_batch_items(batch_id, position, title, record) VALUES(?,?,?,?)', [
        (batch_id, index, str(record.get('title') or '').strip(), json.dumps(record, ensure_ascii=False))
        for index, record in enumerate((dict(raw.get('series_bundle') or raw) for raw in records), 1)])
    db.commit()
    return batch_id


def _claim_next(db, batch_id):
    stale = (dt.datetime.now(dt.timezone.utc) -
             dt.timedelta(seconds=CLAIM_LEASE_SECONDS)).strftime('%Y-%m-%d %H:%M:%S')
    while True:
        row = db.execute('''SELECT * FROM book_batch_items WHERE batch_id=? AND
            (state='pending' OR (state='processing' AND claimed_at < ?))
            ORDER BY position LIMIT 1''', (batch_id, stale)).fetchone()
        if not row:
            return None
        # The conditional update makes the claim atomic across processes.
        claimed = db.execute('''UPDATE book_batch_items SET state='processing', claimed_at=?,
            attempts=attempts+1, updated_at=? WHERE id=? AND state=? AND claimed_at IS ?''',
            (_now(), _now(), row['id'], row['state'], row['claimed_at']))
        db.commit()
        if claimed.rowcount:
            return row


def _add_series(db, record, sources, profile_id):
    if record.get('kind') != 'series':
        raise ValueError('Choose a book series result')
    url = record.get('source_url') or record.get('url')
    if not url:
        raise ValueError('Series has no catalog URL')
    existing = db.execute('SELECT id FROM watched_items WHERE source_url=?', (url,)).fetchone()
    before = db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='book'").fetchone()[0]
    watch_id, _ = add_from_goodreads_metadata(db, record, url, kind='series', sources=sources)
    if profile_id:
        from .watched import assign_watch_profile
        assign_watch_profile(db, watch_id, profile_id)
        for child in db.execute('SELECT id FROM watched_items WHERE parent_id=?', (watch_id,)).fetchall():
            assign_watch_profile(db, child['id'], profile_id)
    after = db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='book'").fetchone()[0]
    return watch_id, bool(existing), after - before


def run_book_batch(db, batch_id, progress=None, should_stop=None):
    """Process unfinished series until done, paused, cancelled, or asked to stop.

    Pause and cancel are read from the database before each series, so another
    connection (the GUI thread or a second process) can control a running batch.
    """
    state = _batch_state(db, batch_id)
    if state in ('cancelled', 'completed'):
        return book_batch_summary(db, batch_id)
    _set_batch_state(db, batch_id, 'running')
    batch = db.execute('SELECT * FROM book_batches WHERE id=?', (batch_id,)).fetchone()
    sources = json.loads(batch['sources'] or '{}') or None
    total = db.execute('SELECT COUNT(*) FROM book_batch_items WHERE batch_id=?', (batch_id,)).fetchone()[0]
    while True:
        if _batch_state(db, batch_id) != 'running' or (should_stop and should_stop()):
            break
        row = _claim_next(db, batch_id)
        if not row:
            break
        record = json.loads(row['record'])
        try:
            watch_id, existed, books_added = _add_series(db, record, sources, batch['profile_id'])
            # A series this batch had already started before a crash is still its addition.
            outcome = 'existing' if existed and row['state'] == 'pending' else 'added'
            db.execute('''UPDATE book_batch_items SET state=?, watch_id=?, books_added=?, error=NULL,
                claimed_at=NULL, updated_at=? WHERE id=?''', (outcome, watch_id, books_added, _now(), row['id']))
        except Exception as exc:
            db.rollback()
            db.execute('''UPDATE book_batch_items SET state='failed', error=?, claimed_at=NULL,
                updated_at=? WHERE id=?''', (str(exc), _now(), row['id']))
        db.commit()
        if progress:
            result = dict(db.execute('SELECT * FROM book_batch_items WHERE id=?', (row['id'],)).fetchone())
            progress(row['position'], total, _item_result(result))
    remaining = db.execute('''SELECT COUNT(*) FROM book_batch_items WHERE batch_id=?
        AND state IN ('pending','processing')''', (batch_id,)).fetchone()[0]
    state = _batch_state(db, batch_id)
    if state == 'running':
        if not remaining:
            _set_batch_state(db, batch_id, 'completed', finished=True)
        elif should_stop and should_stop():
            # Stopped by the caller (for example, application shutdown): resumable.
            _set_batch_state(db, batch_id, 'paused')
    return book_batch_summary(db, batch_id)


def pause_book_batch(db, batch_id):
    if _batch_state(db, batch_id) in ('pending', 'running'):
        _set_batch_state(db, batch_id, 'paused')


def cancel_book_batch(db, batch_id):
    """Stop the batch. Series already added stay in Watched; unstarted ones are cancelled."""
    if _batch_state(db, batch_id) in ('completed', 'cancelled'):
        return
    db.execute('''UPDATE book_batch_items SET state='cancelled', updated_at=?
        WHERE batch_id=? AND state='pending' ''', (_now(), batch_id))
    _set_batch_state(db, batch_id, 'cancelled', finished=True)


def retry_book_batch(db, batch_id, include_cancelled=False):
    """Return failed (and optionally cancelled) series to pending; returns the count."""
    states = ('failed', 'cancelled') if include_cancelled else ('failed',)
    changed = db.execute(f'''UPDATE book_batch_items SET state='pending', error=NULL, updated_at=?
        WHERE batch_id=? AND state IN ({','.join('?' * len(states))})''',
        (_now(), batch_id, *states)).rowcount
    if changed:
        _set_batch_state(db, batch_id, 'pending')
    else:
        db.commit()
    return changed


def resumable_book_batches(db):
    return [row['id'] for row in db.execute(f'''SELECT DISTINCT b.id FROM book_batches b
        JOIN book_batch_items i ON i.batch_id=b.id
        WHERE b.state IN ('pending','running') AND i.state IN ('pending','processing')
        ORDER BY b.id''')]


def list_book_batches(db, limit=20):
    ids = [row['id'] for row in db.execute(
        'SELECT id FROM book_batches ORDER BY id DESC LIMIT ?', (limit,))]
    return [book_batch_summary(db, batch_id) for batch_id in ids]


def _item_result(row):
    result = {'title': row['title'], 'status': row['state']}
    if row.get('watch_id') is not None:
        result['watch_id'] = row['watch_id']
    if row['state'] in ('added', 'existing'):
        result['books_added'] = row.get('books_added') or 0
    if row.get('error'):
        result['error'] = row['error']
    return result


def series_progress(db, watch_id):
    """Aggregate child book states so one series row shows its overall progress."""
    counts = {'books': 0, 'downloaded': 0, 'upcoming': 0, 'failed': 0, 'pending': 0}
    for row in db.execute('''SELECT state, last_error FROM watched_items
            WHERE parent_id=? AND kind='book' ''', (watch_id,)):
        counts['books'] += 1
        if row['state'] == 'downloaded':
            counts['downloaded'] += 1
        elif row['last_error']:
            counts['failed'] += 1
        elif row['state'] == 'upcoming':
            counts['upcoming'] += 1
        else:
            counts['pending'] += 1
    return counts


def book_batch_summary(db, batch_id):
    batch = db.execute('SELECT * FROM book_batches WHERE id=?', (batch_id,)).fetchone()
    if not batch:
        raise ValueError(f'Unknown book batch {batch_id}')
    results, totals = [], {'books': 0, 'downloaded': 0, 'upcoming': 0, 'failed': 0, 'pending': 0}
    for row in db.execute('SELECT * FROM book_batch_items WHERE batch_id=? ORDER BY position', (batch_id,)):
        result = _item_result(dict(row))
        if row['watch_id'] is not None and row['state'] in ('added', 'existing'):
            result['progress'] = series_progress(db, row['watch_id'])
            for key, value in result['progress'].items():
                totals[key] += value
        results.append(result)
    count = lambda state: sum(row['status'] == state for row in results)
    return {'batch_id': batch_id, 'state': batch['state'], 'created_at': batch['created_at'],
            'finished_at': batch['finished_at'], 'total': len(results),
            'added': count('added'), 'existing': count('existing'), 'failed': count('failed'),
            'cancelled': count('cancelled'),
            'remaining': count('pending') + count('processing'),
            'books_added': sum(row.get('books_added', 0) for row in results),
            'books': totals, 'results': results}


def add_book_series_batch(db, records, sources=None, progress=None, profile_id=None, should_stop=None):
    batch_id = create_book_batch(db, records, sources, profile_id)
    return run_book_batch(db, batch_id, progress=progress, should_stop=should_stop)
