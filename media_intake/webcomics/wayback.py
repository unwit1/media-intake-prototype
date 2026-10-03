"""Historical captures for webcomic pages via the Internet Archive's Wayback CDX API.

A completeness report looks up a bounded sample of a source's entry pages (no
mass crawl; the polite shared HTTP client paces requests) and records, per
entry, the newest successful capture: its capture date (kept separate from the
entry's publication date), the replay URL and the original URL. Entries without
any capture are reported as gaps.
"""
import datetime as dt
import json
import time
import urllib.parse

CDX = 'https://web.archive.org/cdx/search/cdx'
REPLAY = 'https://web.archive.org/web/{timestamp}/{original}'
DEFAULT_SAMPLE = 50


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS webcomic_wayback(
        entry_id INTEGER PRIMARY KEY, capture_ts TEXT, capture_url TEXT, original_url TEXT NOT NULL,
        checked_at REAL NOT NULL)''')


def captured_at(timestamp):
    """CDX timestamps (YYYYMMDDhhmmss) as ISO dates, in UTC."""
    try:
        return dt.datetime.strptime(timestamp[:14], '%Y%m%d%H%M%S').replace(tzinfo=dt.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def newest_capture(client, url):
    """The newest 200 capture of a URL as {'timestamp', 'captured_at', 'replay_url', 'original'}, or None."""
    params = urllib.parse.urlencode({'url': url, 'output': 'json', 'fl': 'timestamp,original,statuscode',
                                     'filter': 'statuscode:200', 'limit': '-1'})
    response = client.get(f'{CDX}?{params}', conditional=False, max_bytes=500_000)
    rows = json.loads(response.body.decode('utf-8') or '[]')
    if len(rows) < 2:
        return None
    timestamp, original = rows[-1][0], rows[-1][1]
    return {'timestamp': timestamp, 'captured_at': captured_at(timestamp), 'original': original,
            'replay_url': REPLAY.format(timestamp=timestamp, original=original)}


def completeness_report(db, source_id, client, sample=DEFAULT_SAMPLE, refresh_days=30):
    """Check up to `sample` entries not looked up recently; returns {'captured', 'gaps', 'checked'}."""
    stale = time.time() - refresh_days * 86400
    rows = db.execute('''SELECT e.id, e.url, e.published_at FROM webcomic_entries e
        LEFT JOIN webcomic_wayback w ON w.entry_id=e.id
        WHERE e.source_id=? AND (w.entry_id IS NULL OR w.checked_at < ?) ORDER BY e.ordinal LIMIT ?''',
                      (source_id, stale, max(1, int(sample)))).fetchall()
    for row in rows:
        capture = newest_capture(client, row['url'])
        db.execute('''INSERT INTO webcomic_wayback VALUES(?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET
            capture_ts=excluded.capture_ts, capture_url=excluded.capture_url, checked_at=excluded.checked_at''',
                   (row['id'], capture and capture['timestamp'], capture and capture['replay_url'], row['url'], time.time()))
    db.commit()
    report = {'checked': len(rows), 'captured': [], 'gaps': []}
    for row in db.execute('''SELECT e.entry_key, e.url, e.published_at, w.capture_ts, w.capture_url FROM webcomic_entries e
            JOIN webcomic_wayback w ON w.entry_id=e.id WHERE e.source_id=? ORDER BY e.ordinal''', (source_id,)):
        item = {'entry': row['entry_key'], 'original_url': row['url'], 'published_at': row['published_at']}
        if row['capture_ts']:
            report['captured'].append({**item, 'captured_at': captured_at(row['capture_ts']), 'capture_url': row['capture_url']})
        else:
            report['gaps'].append({**item, 'evidence': 'No successful Wayback capture of this page'})
    return report
