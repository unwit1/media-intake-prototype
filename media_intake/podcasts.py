"""Follow podcasts and download their episodes automatically.

- Shows are found through Apple's public podcast directory (iTunes Search API,
  no key) or added by feed URL.
- Following a show records every episode and downloads the chosen back
  catalogue: none, the latest N, or everything.
- Checks are conditional (ETag/Last-Modified) and run on each show's schedule;
  new episodes queue automatically for the normal download engine (resume,
  retries, speed limit) into ``Podcasts/<Show>/`` in the library.
- Optionally keep only the latest N downloaded episode files per show.

Episode identity is the RSS GUID (or the enclosure URL), so re-published or
reordered feeds never download an episode twice.
"""
import datetime as dt
import email.utils
import json
from pathlib import Path
import re
import urllib.parse
import xml.etree.ElementTree as ET

SOURCE_KEY = 'podcast_rss'
ITUNES_SEARCH = 'https://itunes.apple.com/search'
DEFAULT_CHECK_HOURS = 2
MIME_FORMATS = {'audio/mpeg': '.mp3', 'audio/mp3': '.mp3', 'audio/x-m4a': '.m4a', 'audio/mp4': '.m4a',
                'audio/m4a': '.m4a', 'audio/aac': '.aac', 'audio/ogg': '.ogg', 'audio/opus': '.opus',
                'audio/flac': '.flac', 'audio/wav': '.wav', 'audio/x-wav': '.wav',
                'video/mp4': '.mp4', 'video/x-m4v': '.m4v', 'video/quicktime': '.mov', 'video/webm': '.webm'}
ITUNES_NS = '{http://www.itunes.com/dtds/podcast-1.0.dtd}'
PODCAST_NS = '{https://podcastindex.org/namespace/1.0}'


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS podcast_shows (
        id INTEGER PRIMARY KEY, feed_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
        author TEXT, image_url TEXT, settings TEXT NOT NULL DEFAULT '{}',
        etag TEXT, last_modified TEXT, last_checked TEXT, next_check TEXT, last_error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    db.execute('''CREATE TABLE IF NOT EXISTS podcast_episodes (
        id INTEGER PRIMARY KEY, show_id INTEGER NOT NULL, guid TEXT NOT NULL, title TEXT NOT NULL,
        published TEXT, enclosure_url TEXT NOT NULL, mime_type TEXT, length INTEGER, duration TEXT,
        season INTEGER, episode INTEGER, state TEXT NOT NULL DEFAULT 'new', item_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(show_id, guid), FOREIGN KEY(show_id) REFERENCES podcast_shows(id))''')
    db.execute('CREATE INDEX IF NOT EXISTS podcast_episodes_show ON podcast_episodes(show_id, published)')


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _iso(value):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _text(node, name):
    found = node.find(name)
    return (found.text or '').strip() if found is not None and found.text else ''


def _int(value):
    try:
        number = int(str(value).strip())
        return number if number >= 0 else None
    except (TypeError, ValueError):
        return None


def _safe_url(value, base=''):
    url = urllib.parse.urljoin(base, str(value or '').strip())
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password:
        return None
    return url


def episode_format(url, mime_type=''):
    from .core import AUDIO_FORMATS, VIDEO_FORMATS
    suffix = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).suffix.casefold()
    if suffix in AUDIO_FORMATS | VIDEO_FORMATS:
        return suffix
    return MIME_FORMATS.get(str(mime_type or '').split(';')[0].strip().casefold(), '.mp3')


def parse_podcast_feed(body, feed_url):
    """Return {'title', 'author', 'image_url', 'episodes': [...]} from podcast RSS."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError('The podcast feed is not valid RSS') from exc
    channel = root.find('channel')
    if channel is None:
        raise ValueError('The podcast feed has no RSS channel')
    image = channel.find(f'{ITUNES_NS}image')
    rss_image = channel.find('image')
    image_url = (image.get('href') if image is not None else
                 rss_image.findtext('url') if rss_image is not None else None)
    show = {'title': _text(channel, 'title') or feed_url, 'author': _text(channel, f'{ITUNES_NS}author'),
            'image_url': _safe_url(image_url, feed_url) if image_url else None, 'episodes': []}
    seen = set()
    for item in channel.findall('item'):
        enclosure = item.find('enclosure')
        url = _safe_url(enclosure.get('url'), feed_url) if enclosure is not None else None
        if not url:
            continue  # text-only posts are not episodes
        guid = _text(item, 'guid') or url
        if guid in seen:
            continue
        seen.add(guid)
        show['episodes'].append({
            'guid': guid[:500], 'title': _text(item, 'title') or 'Untitled episode',
            'published': _iso(_text(item, 'pubDate')), 'enclosure_url': url,
            'mime_type': (enclosure.get('type') or '')[:100], 'length': _int(enclosure.get('length')),
            'duration': _text(item, f'{ITUNES_NS}duration')[:40] or None,
            'season': _int(_text(item, f'{ITUNES_NS}season') or _text(item, f'{PODCAST_NS}season')),
            'episode': _int(_text(item, f'{ITUNES_NS}episode') or _text(item, f'{PODCAST_NS}episode')),
        })
    return show


def search_directory(query, limit=25, fetch=None):
    """Search Apple's public podcast directory; returns shows with their feed URLs."""
    from .core import http_get
    params = urllib.parse.urlencode({'media': 'podcast', 'entity': 'podcast', 'term': query, 'limit': limit})
    body, _url = (fetch or http_get)(f'{ITUNES_SEARCH}?{params}')
    data = json.loads(body)
    shows = []
    for result in data.get('results') or []:
        feed = _safe_url(result.get('feedUrl'))
        if not feed:
            continue
        shows.append({'title': result.get('collectionName') or result.get('trackName') or feed,
                      'author': result.get('artistName') or '', 'feed_url': feed,
                      'episodes': result.get('trackCount'), 'genre': result.get('primaryGenreName') or '',
                      'image_url': result.get('artworkUrl600') or result.get('artworkUrl100')})
    return shows


def _settings(row):
    settings = {'auto_download': True, 'keep_latest': 0, 'check_hours': DEFAULT_CHECK_HOURS}
    try:
        settings.update(json.loads(row['settings'] or '{}'))
    except (TypeError, ValueError):
        pass
    return settings


def _fetch_feed(db, show, client=None):
    from .webcomics.http import HttpClient
    client = client or HttpClient(db, user_agent='MediaArchiveImporter/0.3 (+podcast follower)', respect_robots=False)
    return client.get(show['feed_url'], max_bytes=20_000_000)


def _upsert_episodes(db, show_id, episodes):
    new = []
    for episode in episodes:
        cursor = db.execute('''INSERT OR IGNORE INTO podcast_episodes
            (show_id, guid, title, published, enclosure_url, mime_type, length, duration, season, episode)
            VALUES(?,?,?,?,?,?,?,?,?,?)''', (show_id, episode['guid'], episode['title'], episode['published'],
                                            episode['enclosure_url'], episode['mime_type'], episode['length'],
                                            episode['duration'], episode['season'], episode['episode']))
        if cursor.rowcount:
            new.append(cursor.lastrowid)
        else:
            # Keep the latest title and file URL (feeds re-host episodes).
            db.execute('''UPDATE podcast_episodes SET title=?, enclosure_url=?, mime_type=? WHERE show_id=? AND guid=?
                AND state IN ('new','skipped')''', (episode['title'], episode['enclosure_url'], episode['mime_type'],
                                                    show_id, episode['guid']))
    return new


def queue_episode(db, episode_id):
    """Queue one episode for the download engine; returns the queue item ID."""
    from .core import enqueue
    row = db.execute('''SELECT e.*, s.title AS show_title, s.feed_url FROM podcast_episodes e
        JOIN podcast_shows s ON s.id=e.show_id WHERE e.id=?''', (episode_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown podcast episode')
    if row['item_id'] and row['state'] in ('queued', 'downloaded'):
        return row['item_id']
    date = (row['published'] or '')[:10]
    title = ' - '.join(part for part in (date, row['title']) if part)
    item = enqueue(db, {
        'title': title[:300], 'author': row['show_title'][:300], 'url': row['enclosure_url'],
        'format': episode_format(row['enclosure_url'], row['mime_type']), 'source': row['feed_url'],
        'source_key': SOURCE_KEY, 'provider': 'Podcast', 'kind': 'podcast_episode',
        'library_subfolder': str(Path('Podcasts') / re.sub(r'[\\/:*?"<>|]+', '_', row['show_title']).strip(' .')[:120]),
        'podcast_episode_id': episode_id, 'expected_size': None,
        **({'series': row['show_title'], 'series_index': row['episode']} if row['episode'] else {})})
    db.execute("UPDATE podcast_episodes SET state='queued', item_id=? WHERE id=?", (item['id'], episode_id))
    db.commit()
    return item['id']


def follow(db, feed_url, backlog='latest', latest=3, settings=None, client=None):
    """Follow a show. backlog: 'none', 'latest' (newest `latest` episodes) or 'all'."""
    feed_url = _safe_url(feed_url)
    if not feed_url:
        raise ValueError('Enter a podcast feed URL (http or https)')
    existing = db.execute('SELECT id FROM podcast_shows WHERE feed_url=?', (feed_url,)).fetchone()
    if existing:
        return existing['id']
    response = _fetch_feed(db, {'feed_url': feed_url}, client)
    show = parse_podcast_feed(response.body, response.url)
    stored = {'auto_download': True, 'keep_latest': 0, 'check_hours': DEFAULT_CHECK_HOURS, **(settings or {})}
    cursor = db.execute('''INSERT INTO podcast_shows(feed_url, title, author, image_url, settings, etag, last_modified,
        last_checked, next_check) VALUES(?,?,?,?,?,?,?,?,?)''',
        (feed_url, show['title'], show['author'], show['image_url'], json.dumps(stored),
         response.headers.get('etag'), response.headers.get('last-modified'), _now(),
         (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=stored['check_hours'])).replace(microsecond=0).isoformat()))
    show_id = cursor.lastrowid
    _upsert_episodes(db, show_id, show['episodes'])
    newest_first = [row['id'] for row in db.execute(
        'SELECT id FROM podcast_episodes WHERE show_id=? ORDER BY COALESCE(published, "") DESC, id DESC', (show_id,))]
    chosen = newest_first if backlog == 'all' else newest_first[:max(0, int(latest))] if backlog == 'latest' else []
    keep = set(chosen)
    db.executemany("UPDATE podcast_episodes SET state='skipped' WHERE id=?",
                   [(episode_id,) for episode_id in newest_first if episode_id not in keep])
    db.commit()
    for episode_id in reversed(chosen):  # oldest first, so the queue reads in order
        queue_episode(db, episode_id)
    return show_id


def check_show(db, show_id, client=None, force=False):
    """Refresh one show; returns the number of new episodes found."""
    show = db.execute('SELECT * FROM podcast_shows WHERE id=?', (show_id,)).fetchone()
    if show is None:
        raise ValueError('Unknown podcast')
    settings = _settings(show)
    next_check = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=float(settings['check_hours']))).replace(
        microsecond=0).isoformat()
    try:
        response = _fetch_feed(db, show, client)
        parsed = parse_podcast_feed(response.body, response.url)
    except Exception as exc:
        db.execute('UPDATE podcast_shows SET last_checked=?, next_check=?, last_error=? WHERE id=?',
                   (_now(), next_check, str(exc)[:500], show_id))
        db.commit()
        raise
    new_ids = [] if response.unchanged and not force else _upsert_episodes(db, show_id, parsed['episodes'])
    db.execute('''UPDATE podcast_shows SET title=?, author=?, image_url=COALESCE(?, image_url), etag=?, last_modified=?,
        last_checked=?, next_check=?, last_error=NULL WHERE id=?''',
               (parsed['title'], parsed['author'], parsed['image_url'], response.headers.get('etag'),
                response.headers.get('last-modified'), _now(), next_check, show_id))
    db.commit()
    if settings.get('auto_download', True):
        for episode_id in sorted(new_ids):
            queue_episode(db, episode_id)
    return len(new_ids)


def due_shows(db, now=None):
    now = now or _now()
    return [row['id'] for row in db.execute(
        'SELECT id FROM podcast_shows WHERE next_check IS NULL OR next_check <= ? ORDER BY next_check', (now,))]


def check_due(db, client=None):
    """Check every show that is due; returns {show_id: new episodes or error text}."""
    outcomes = {}
    for show_id in due_shows(db):
        try:
            outcomes[show_id] = check_show(db, show_id, client)
        except Exception as exc:
            outcomes[show_id] = str(exc)
    return outcomes


def sync_downloads(db):
    """Mark episodes whose queue items finished; apply each show's keep-latest limit."""
    for row in db.execute('''SELECT e.id, i.state AS item_state FROM podcast_episodes e
            JOIN items i ON i.id=e.item_id WHERE e.state='queued' ''').fetchall():
        if row['item_state'] in ('downloaded', 'imported'):
            db.execute("UPDATE podcast_episodes SET state='downloaded' WHERE id=?", (row['id'],))
    removed = []
    for show in db.execute('SELECT * FROM podcast_shows').fetchall():
        keep = int(_settings(show).get('keep_latest') or 0)
        if keep <= 0:
            continue
        rows = db.execute('''SELECT e.id, i.id AS item_id, i.file_path FROM podcast_episodes e JOIN items i ON i.id=e.item_id
            WHERE e.show_id=? AND e.state='downloaded' ORDER BY COALESCE(e.published,'') DESC, e.id DESC''',
                          (show['id'],)).fetchall()
        for row in rows[keep:]:
            if row['file_path']:
                Path(row['file_path']).unlink(missing_ok=True)
                removed.append(row['file_path'])
            db.execute("UPDATE podcast_episodes SET state='removed' WHERE id=?", (row['id'],))
            db.execute("UPDATE items SET state='removed', file_path=NULL WHERE id=?", (row['item_id'],))
    db.commit()
    return removed


def update_settings(db, show_id, **changes):
    show = db.execute('SELECT settings FROM podcast_shows WHERE id=?', (show_id,)).fetchone()
    settings = _settings(show)
    settings.update({key: value for key, value in changes.items() if value is not None})
    db.execute('UPDATE podcast_shows SET settings=? WHERE id=?', (json.dumps(settings), show_id))
    db.commit()
    return settings


def unfollow(db, show_id):
    """Stop following a show. Downloaded episode files are kept."""
    db.execute('DELETE FROM podcast_episodes WHERE show_id=?', (show_id,))
    db.execute('DELETE FROM podcast_shows WHERE id=?', (show_id,))
    db.commit()


# OPML: the subscription list format every podcast app imports and exports ----

def export_opml(db, title='Media Intake podcasts'):
    """Return the followed shows as an OPML 2.0 document (bytes)."""
    root = ET.Element('opml', version='2.0')
    head = ET.SubElement(root, 'head')
    ET.SubElement(head, 'title').text = title
    ET.SubElement(head, 'dateCreated').text = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    body = ET.SubElement(root, 'body')
    for row in db.execute('SELECT title, feed_url FROM podcast_shows ORDER BY title COLLATE NOCASE'):
        ET.SubElement(body, 'outline', type='rss', text=row['title'], title=row['title'], xmlUrl=row['feed_url'])
    ET.indent(root)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def parse_opml(body):
    """Return ``[(title, feed_url)]`` for every feed outline, including nested folders."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f'Not an OPML file: {exc}') from None
    if root.tag != 'opml':
        raise ValueError('Not an OPML file')
    feeds, seen = [], set()
    for node in root.iter('outline'):
        url = _safe_url(node.get('xmlUrl') or node.get('xmlurl') or '')
        if url and url not in seen:
            seen.add(url)
            feeds.append((node.get('title') or node.get('text') or url, url))
    return feeds


def import_opml(db, body, backlog='none', latest=0, client=None):
    """Follow every feed in an OPML file; already-followed feeds are left alone.

    Importing defaults to no back catalogue, so moving a subscription list from
    another app does not queue years of old episodes. Returns a summary dict.
    """
    added, existing, failed = [], [], []
    for title, url in parse_opml(body):
        if db.execute('SELECT 1 FROM podcast_shows WHERE feed_url=?', (url,)).fetchone():
            existing.append(title)
            continue
        try:
            follow(db, url, backlog=backlog, latest=latest, client=client)
            added.append(title)
        except Exception as exc:  # one dead feed should not stop the rest
            failed.append(f'{title}: {exc}')
    return {'added': added, 'existing': existing, 'failed': failed}
