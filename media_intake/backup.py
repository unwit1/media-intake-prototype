"""Back up the app database and export the watch list.

The database holds the queue, watch list, settings, podcast follows and webcomic
archives' records (not the downloaded files themselves). ``backup_database``
uses SQLite's online backup, so it is consistent even while downloads run.
Saved passwords and API keys are stored encrypted for the current Windows user,
so a backup restored under another account needs them re-entered.
"""
import csv
from pathlib import Path
import sqlite3

WATCH_COLUMNS = ('id', 'kind', 'title', 'author', 'series', 'release_date', 'state', 'last_checked_at',
                 'source_url', 'file_path')


def backup_database(db, target):
    """Copy the open database to ``target`` (a new .sqlite3 file) and return its path."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + '.partial')
    partial.unlink(missing_ok=True)
    destination = sqlite3.connect(partial)
    try:
        db.backup(destination)
        if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('The backup copy failed its integrity check')
    finally:
        destination.close()
    partial.replace(target)
    return target


def export_watch_list(db, target):
    """Write every watched item to a CSV file (UTF-8 with BOM, so Excel reads it); return the row count."""
    rows = db.execute('''SELECT w.id, w.kind, w.title, w.author, p.title AS series, w.release_date, w.state,
        w.last_checked_at, w.source_url, w.file_path FROM watched_items w
        LEFT JOIN watched_items p ON p.id = w.parent_id ORDER BY COALESCE(p.title, w.title) COLLATE NOCASE,
        w.release_date, w.title COLLATE NOCASE''').fetchall()
    with open(target, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(WATCH_COLUMNS)
        for row in rows:
            writer.writerow(['' if row[column] is None else row[column] for column in WATCH_COLUMNS])
    return len(rows)
