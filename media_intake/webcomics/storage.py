"""Additive versioned SQLite migration, separate source identities and durable jobs."""
import json

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS webcomic_schema(version INTEGER PRIMARY KEY)''',
    '''CREATE TABLE IF NOT EXISTS webcomic_series(
        id TEXT PRIMARY KEY, metadata TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS webcomic_sources(
        id INTEGER PRIMARY KEY, series_id TEXT NOT NULL, watch_id INTEGER UNIQUE,
        url TEXT NOT NULL UNIQUE, adapter TEXT NOT NULL, adapter_version TEXT NOT NULL,
        config TEXT NOT NULL DEFAULT '{}', cursor TEXT NOT NULL DEFAULT '{}',
        health TEXT NOT NULL DEFAULT 'OK', last_success TEXT, last_failure TEXT,
        failures INTEGER NOT NULL DEFAULT 0, metrics TEXT NOT NULL DEFAULT '{}')''',
    '''CREATE TABLE IF NOT EXISTS webcomic_entries(
        id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, entry_key TEXT NOT NULL,
        url TEXT NOT NULL, ordinal REAL, published_at TEXT, chapter_key TEXT,
        metadata TEXT NOT NULL, fingerprint TEXT NOT NULL, queue_id INTEGER,
        state TEXT NOT NULL DEFAULT 'known', removed INTEGER NOT NULL DEFAULT 0,
        UNIQUE(source_id,entry_key))''',
    '''CREATE INDEX IF NOT EXISTS webcomic_entry_pending ON webcomic_entries(source_id,state,ordinal)''',
    '''CREATE TABLE IF NOT EXISTS webcomic_chapters(
        source_id INTEGER NOT NULL, chapter_key TEXT NOT NULL, metadata TEXT NOT NULL,
        PRIMARY KEY(source_id,chapter_key))''',
    '''CREATE TABLE IF NOT EXISTS webcomic_volumes(
        source_id INTEGER NOT NULL, volume_key TEXT NOT NULL, metadata TEXT NOT NULL,
        PRIMARY KEY(source_id,volume_key))''',
    '''CREATE TABLE IF NOT EXISTS webcomic_assets(
        entry_id INTEGER NOT NULL, revision TEXT NOT NULL, ordinal INTEGER NOT NULL,
        metadata TEXT NOT NULL, PRIMARY KEY(entry_id,revision,ordinal))''',
    '''CREATE TABLE IF NOT EXISTS webcomic_events(
        id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, entry_key TEXT,
        event_type TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE INDEX IF NOT EXISTS webcomic_events_source ON webcomic_events(source_id,id)''',
    '''CREATE TABLE IF NOT EXISTS webcomic_overrides(
        source_id INTEGER NOT NULL, entry_key TEXT NOT NULL, field TEXT NOT NULL,
        value TEXT NOT NULL, PRIMARY KEY(source_id,entry_key,field))''',
    '''CREATE TABLE IF NOT EXISTS webcomic_http_cache(
        url TEXT PRIMARY KEY, final_url TEXT NOT NULL, body BLOB NOT NULL,
        headers TEXT NOT NULL, fetched_at TEXT NOT NULL)''',
)


def migrate(db):
    for sql in SCHEMA:
        db.execute(sql)
    db.execute('INSERT OR IGNORE INTO webcomic_schema(version) VALUES(1)')


def event(db, source_id, kind, key=None, detail=None):
    db.execute('INSERT INTO webcomic_events(source_id,event_type,entry_key,detail) VALUES(?,?,?,?)',
               (source_id, kind, key, json.dumps(detail or {}, ensure_ascii=False)))


def set_override(db, source_id, entry_key, field, value):
    if field not in {'chapter', 'title', 'content_type'}:
        raise ValueError('Supported overrides: chapter, title, content_type')
    db.execute('INSERT INTO webcomic_overrides VALUES(?,?,?,?) ON CONFLICT(source_id,entry_key,field) DO UPDATE SET value=excluded.value',
               (source_id, entry_key, field, json.dumps(value)))
    row = db.execute('SELECT * FROM webcomic_entries WHERE source_id=? AND entry_key=?', (source_id, entry_key)).fetchone()
    if row:
        entry = json.loads(row['metadata'])
        if field == 'chapter':
            from .chapters import resolve_chapter
            from .models import record
            chapter = resolve_chapter(entry['title'], entry['source_url'], override=value)
            entry['chapter_id'] = chapter.source_id if chapter else None
            if chapter:
                existing = db.execute('SELECT 1 FROM webcomic_chapters WHERE source_id=? AND chapter_key=?',
                                      (source_id, chapter.source_id)).fetchone()
                if not existing:
                    event(db, source_id, 'CHAPTER_ADDED', detail=record(chapter))
                db.execute('INSERT OR REPLACE INTO webcomic_chapters VALUES(?,?,?)',
                           (source_id, chapter.source_id, json.dumps(record(chapter))))
            db.execute('UPDATE webcomic_entries SET chapter_key=? WHERE id=?', (entry['chapter_id'], row['id']))
            event(db, source_id, 'CHAPTER_REASSIGNED', entry_key, {'chapter_id': entry['chapter_id'], 'manual': True})
        else:
            entry[field] = value
        db.execute('UPDATE webcomic_entries SET metadata=? WHERE id=?', (json.dumps(entry), row['id']))
    db.commit()


def library(db):
    return [dict(row) for row in db.execute('''SELECT s.*, w.title,w.author,w.state AS watch_state,
        w.last_checked_at,w.last_error,
        (SELECT COUNT(*) FROM webcomic_entries e WHERE e.source_id=s.id) AS entries,
        (SELECT COUNT(*) FROM webcomic_entries e WHERE e.source_id=s.id AND e.state='archived') AS archived,
        (SELECT COUNT(*) FROM webcomic_assets a JOIN webcomic_entries e ON a.entry_id=e.id
            WHERE e.source_id=s.id) AS assets,
        (SELECT json_extract(e.metadata,'$.title') FROM webcomic_entries e
            WHERE e.source_id=s.id ORDER BY e.ordinal DESC,e.id DESC LIMIT 1) AS latest_release,
        (SELECT json_extract(e.metadata,'$.title') FROM webcomic_entries e
            WHERE e.source_id=s.id AND e.state='archived' ORDER BY e.ordinal DESC,e.id DESC LIMIT 1) AS latest_archived
        FROM webcomic_sources s JOIN watched_items w ON w.id=s.watch_id ORDER BY w.title''')]
