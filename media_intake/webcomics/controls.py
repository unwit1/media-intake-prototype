"""Per-source webcomic controls: pause/resume, one-time backlog archiving and single-source checks.

- A paused source is never polled or downloaded until resumed.
- "Archive backlog" is a one-time job: every entry known at that moment is
  queued and downloaded once, even with automatic archiving off. Entries
  discovered afterwards stay metadata-only unless automatic archiving is on.
- ``check_source`` runs one watched comic's check without touching any other
  watched media.
"""
import json


def _config(db, source_id):
    row = db.execute('SELECT config FROM webcomic_sources WHERE id=?', (source_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown webcomic source')
    return json.loads(row['config'] or '{}')


def _save(db, source_id, config):
    db.execute('UPDATE webcomic_sources SET config=? WHERE id=?', (json.dumps(config), source_id))
    db.commit()


def set_paused(db, source_id, paused):
    import datetime as dt
    config = _config(db, source_id)
    config['paused'] = bool(paused)
    _save(db, source_id, config)
    # A paused comic leaves the schedule entirely; resuming makes it due now.
    db.execute('''UPDATE watched_items SET next_check_at=? WHERE id=(SELECT watch_id FROM webcomic_sources WHERE id=?)''',
               ('9999-12-31T00:00:00' if paused else dt.datetime.now().isoformat(), source_id))
    from .storage import event
    event(db, source_id, 'SOURCE_PAUSED' if paused else 'SOURCE_RESUMED')
    db.commit()


def is_paused(db, source_id):
    return bool(_config(db, source_id).get('paused'))


def set_auto_archive(db, source_id, enabled):
    config = _config(db, source_id)
    config['auto_archive'] = bool(enabled)
    _save(db, source_id, config)


def archive_backlog(db, source_id):
    """Queue every entry known now (once); returns how many were scheduled."""
    newest = db.execute('SELECT COALESCE(MAX(ordinal), 0) FROM webcomic_entries WHERE source_id=?', (source_id,)).fetchone()[0]
    changed = db.execute("""UPDATE webcomic_entries SET state='pending' WHERE source_id=? AND removed=0
        AND state IN ('known','pending') AND queue_id IS NULL""", (source_id,)).rowcount
    config = _config(db, source_id)
    config['backlog_until_ordinal'] = max(float(config.get('backlog_until_ordinal') or 0), float(newest))
    _save(db, source_id, config)
    from .storage import event
    event(db, source_id, 'BACKLOG_REQUESTED', detail={'entries': changed, 'until_ordinal': newest})
    db.commit()
    return changed


def backlog_entries(db, source_id, limit):
    """Pending backlog entries within the one-time job's ordinal limit."""
    config = _config(db, source_id)
    until = config.get('backlog_until_ordinal')
    if not until:
        return []
    return [row['id'] for row in db.execute("""SELECT id FROM webcomic_entries WHERE source_id=? AND removed=0
        AND state='pending' AND ordinal <= ? ORDER BY ordinal, id LIMIT ?""", (source_id, until, limit))]


def check_source(db, watch_id, library_folder):
    """Check (and archive per its settings) a single watched comic now."""
    from .watcher import process_watch
    watch = db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()
    if watch is None:
        raise ValueError('Unknown watched comic')
    process_watch(db, dict(watch), library_folder)
    return dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
