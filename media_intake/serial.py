"""Site-independent chapter intake for illustrated and prose serials."""
import hashlib
import html
import io
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
import email.utils
import datetime as dt
from html.parser import HTMLParser
from pathlib import Path

from .core import ATOM, MAX_FILE_BYTES, check_download, enqueue, file_name, http_get, open_url

KINDS = {'comic', 'manga', 'webcomic', 'webtoon', 'webfiction', 'fanfiction'}
IMAGE_KINDS = {'comic', 'manga', 'webcomic', 'webtoon'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
YOUTUBE = '{http://www.youtube.com/xml/schemas/2015}'
PODCAST_PROVIDER = 'podcast_rss'
VOID_TAGS = {'img', 'br', 'hr', 'meta', 'link', 'input', 'source', 'wbr'}


def validate_manifest(manifest):
    item = dict(manifest)
    kind = item.get('kind')
    if kind not in KINDS: raise ValueError('Unknown serial kind')
    if not item.get('title'): raise ValueError('Missing series title')
    chapters = item.get('chapters')
    if not isinstance(chapters, list) or not 1 <= len(chapters) <= 500:
        raise ValueError('Serial needs 1 to 500 chapters')
    seen = set()
    for chapter in chapters:
        mangadex_id = chapter.get('mangadex_id')
        chapter_url = str(chapter.get('url') or '')
        url = urllib.parse.urlsplit(chapter_url)
        images = chapter.get('images')
        if images is not None:
            if not isinstance(images, list) or not 1 <= len(images) <= 1000:
                raise ValueError('A direct-image chapter needs 1 to 1000 images')
            for image_url in images:
                image = urllib.parse.urlsplit(str(image_url or ''))
                if (image.scheme not in ('http', 'https') or not image.netloc or
                        image.username or image.password):
                    raise ValueError('Chapter image URLs must be HTTP(S) without credentials')
        if mangadex_id:
            if not re.fullmatch(r'[0-9a-f-]{36}', str(mangadex_id), flags=re.I):
                raise ValueError('Invalid MangaDex chapter ID')
            chapter['url'] = f'https://mangadex.org/chapter/{mangadex_id}'
        elif chapter_url and (url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password):
            raise ValueError('Chapter URLs must be HTTP(S) without credentials')
        elif not chapter_url and not images:
            raise ValueError('Chapter needs a public URL or direct images')
        if not chapter_url:
            chapter['url'] = images[0]
        if chapter['url'] in seen: raise ValueError('Duplicate chapter URL')
        seen.add(chapter['url'])
    selector = item.get('selector', 'article,main')
    if not isinstance(selector, str) or not re.fullmatch(r'[\w#.,\- ]{1,120}', selector):
        raise ValueError('Selector supports comma-separated tags, .classes and #ids')
    item['selector'] = selector
    item['format'] = '.cbz' if kind in IMAGE_KINDS else '.epub'
    item.setdefault('author', 'Unknown')
    item.setdefault('source', chapters[0]['url'])
    # A stable queue key independent of a site's search engine or feed order.
    identity = json.dumps([kind, item['title'], [c['url'] for c in chapters]], ensure_ascii=False)
    item['url'] = 'serial+https://item/' + hashlib.sha256(identity.encode()).hexdigest()
    return item


def enqueue_serial(db, manifest):
    return enqueue(db, validate_manifest(manifest))


class _FeedAutodiscoveryParser(HTMLParser):
    _TYPES = {
        'application/rss+xml', 'application/atom+xml', 'application/feed+json',
        'application/json', 'text/calendar', 'application/ics',
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() != 'link':
            return
        values = {str(key).casefold(): str(value or '').strip()
                  for key, value in attrs if key}
        rel = {value.casefold() for value in values.get('rel', '').split()}
        media_type = values.get('type', '').split(';', 1)[0].strip().casefold()
        href = values.get('href', '')
        if 'alternate' in rel and media_type in self._TYPES and href:
            self.links.append({'href': href, 'type': media_type,
                               'title': values.get('title', '')})


def _looks_like_html(data, xml_root=None):
    if xml_root is not None and xml_root.tag.rsplit('}', 1)[-1].casefold() == 'html':
        return True
    prefix = data[:2048].lstrip(b'\xef\xbb\xbf\r\n \t').lower()
    return (prefix.startswith((b'<!doctype html', b'<html', b'<head')) or
            b'<html' in prefix[:512])


def _discover_alternate_feed(page_data, page_url, kind, limit, visited):
    parser = _FeedAutodiscoveryParser()
    parser.feed(page_data.decode('utf-8-sig', errors='replace'))
    candidates = []
    for link in parser.links:
        candidate_url = urllib.parse.urljoin(page_url, link['href'])
        parsed = urllib.parse.urlsplit(candidate_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or
                parsed.username or parsed.password):
            continue
        key = urllib.parse.urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(),
                                       parsed.path, parsed.query, ''))
        if key in visited:
            continue
        candidates.append((link, candidate_url))
    # Prefer episode-like or comic-like feeds when a creator page advertises
    # several alternatives, then try the remaining standards-based feeds.
    def rank(candidate):
        link, _ = candidate
        title = link['title'].casefold()
        preferred = any(word in title for word in ('episode', 'comic', 'chapter', 'video'))
        return (not preferred, link['type'] not in {
            'application/rss+xml', 'application/atom+xml', 'application/feed+json'},
            title)
    candidates.sort(key=rank)
    errors = []
    for _, candidate_url in candidates[:8]:
        try:
            manifest = discover_feed(candidate_url, kind, limit, _visited=visited)
            manifest['discovered_feed_from'] = page_url
            return manifest
        except Exception as exc:
            errors.append(f'{candidate_url}: {exc}')
    if errors:
        detail = '; '.join(errors[:3])
        raise ValueError('The page advertised feeds, but none could be read: ' + detail)
    raise ValueError('The page has no supported RSS, Atom, JSON Feed, or iCalendar alternate link')


def discover_feed(feed_url, kind, limit=100, _visited=None):
    """Build a manifest from a feed URL or a page advertising a supported feed."""
    if kind not in KINDS or not 1 <= limit <= 500: raise ValueError('Invalid kind or limit')
    parsed_url = urllib.parse.urlsplit(str(feed_url or ''))
    if (parsed_url.scheme not in ('http', 'https') or not parsed_url.netloc or
            parsed_url.username or parsed_url.password):
        raise ValueError('Feed URL must be public HTTP(S) without credentials')
    visited = set(_visited or ())
    current_key = urllib.parse.urlunsplit((parsed_url.scheme.casefold(),
                                           parsed_url.netloc.casefold(),
                                           parsed_url.path, parsed_url.query, ''))
    if current_key in visited or len(visited) >= 9:
        raise ValueError('Feed autodiscovery exceeded its redirect or link limit')
    visited.add(current_key)
    data, final = http_get(feed_url)
    final_parts = urllib.parse.urlsplit(final)
    final_key = urllib.parse.urlunsplit((final_parts.scheme.casefold(),
                                         final_parts.netloc.casefold(),
                                         final_parts.path, final_parts.query, ''))
    if final_key in visited and final_key != current_key:
        raise ValueError('Feed autodiscovery response redirected to a previously visited page')
    visited.add(final_key)
    return parse_feed(data, final, kind, limit, visited)


def parse_feed(data, final, kind, limit=100, visited=None):
    """Parse a fetched feed without network I/O; reused by conditional watchers."""
    if kind not in KINDS or not 1 <= limit <= 500:
        raise ValueError('Invalid kind or limit')
    visited = set(visited or ())
    try:
        json_feed = json.loads(data.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        json_feed = None
    if isinstance(json_feed, dict) and json_feed.get('@type') == 'ComicSeries':
        if kind not in IMAGE_KINDS:
            raise ValueError('SSS ComicSeries feeds can only be used for comic watches')
        chapters = []
        now = dt.datetime.now(dt.timezone.utc)
        series_exclusive = json_feed.get('scopesForExclusiveContent') or []
        for issue in (json_feed.get('issues') or [])[:limit]:
            if not isinstance(issue, dict):
                continue
            exclusive = issue.get('scopesForExclusiveContent') or series_exclusive
            free_at = _feed_timestamp(issue.get('dateExclusiveContentIsAvailable'))
            if exclusive:
                if not free_at:
                    continue
                try:
                    free_date = dt.datetime.fromisoformat(free_at.replace('Z', '+00:00'))
                    if free_date.tzinfo is None:
                        free_date = free_date.replace(tzinfo=dt.timezone.utc)
                    if free_date > now:
                        continue
                except ValueError:
                    continue
            images = []
            for story in issue.get('stories') or []:
                details = story.get('storyImage') or {}
                if isinstance(details, str):
                    image_url = details
                elif isinstance(details, dict):
                    image_url = urllib.parse.urljoin(
                        str(details.get('base_url') or details.get('baseUrl') or ''),
                        str(details.get('story') or details.get('url') or ''))
                else:
                    image_url = ''
                image = urllib.parse.urlsplit(image_url)
                if image.scheme in ('http', 'https') and image.netloc and not image.username and not image.password:
                    images.append(image_url)
            if not images:
                continue
            issue_id = str(issue.get('identifier') or '')
            issue_url = final + ('#issue-' + urllib.parse.quote(issue_id, safe='') if issue_id else '')
            chapter = {'title': str(issue.get('name') or 'Comic issue').strip(),
                       'url': issue_url, 'images': images}
            published = _feed_timestamp(issue.get('datePublished'))
            if published:
                chapter['published'] = published
            chapters.append(chapter)
        chapters.reverse()
        creators = json_feed.get('creators') or []
        names = [str(item.get('name') or '').strip() if isinstance(item, dict) else str(item).strip()
                 for item in creators]
        author = ', '.join(name for name in names if name) or str(json_feed.get('feedOwnerName') or 'Unknown')
        return {'title': str(json_feed.get('name') or 'Untitled comic').strip(),
                'author': author, 'kind': kind, 'source': final,
                'selector': 'article,main', 'chapters': chapters,
                'sss_metadata': {key: json_feed.get(key) for key in
                                 ('identifier', 'description', 'status', 'seriesType', 'seriesLayout',
                                 'genres', 'tags', 'contentRating', 'copyrightNotice') if key in json_feed}}
    if (isinstance(json_feed, dict) and
            str(json_feed.get('version') or '').startswith('https://jsonfeed.org/version/') and
            isinstance(json_feed.get('items'), list)):
        return _discover_json_feed(json_feed, final, kind, limit)
    if (isinstance(json_feed, dict) and
            (json_feed.get('@type') == 'MediaSchedule' or
             json_feed.get('schedule_type') == 'media_schedule') and
            isinstance(json_feed.get('events'), list)):
        return _discover_json_schedule_feed(json_feed, final, kind, limit)
    if isinstance(json_feed, (dict, list)) and _jsonld_schedule_events(json_feed):
        return _discover_jsonld_schedule(json_feed, final, kind, limit)
    if json_feed is not None:
        raise ValueError('Expected RSS, Atom, JSON Feed, JSON-LD events, iCalendar, or SSS ComicSeries')
    if data.lstrip(b'\xef\xbb\xbf\r\n \t').startswith(b'BEGIN:VCALENDAR'):
        return _discover_ical_events(data, final, kind, limit)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        if _looks_like_html(data):
            return _discover_alternate_feed(data, final, kind, limit, visited)
        raise ValueError('Feed response is not valid RSS, Atom, JSON, or iCalendar') from exc
    if _looks_like_html(data, root):
        return _discover_alternate_feed(data, final, kind, limit, visited)
    atom = root.tag == ATOM + 'feed'
    if atom:
        title = root.findtext(ATOM + 'title', default='Untitled serial')
        author = root.findtext(ATOM + 'author/' + ATOM + 'name', default='Unknown')
        entries = root.findall(ATOM + 'entry')
        feed_metadata = {'provider': 'atom'}
    elif root.tag.lower() == 'rss':
        channel = root.find('channel')
        if channel is None: raise ValueError('RSS has no channel')
        title = channel.findtext('title', default='Untitled serial')
        author = (_feed_text(channel, 'author') or _feed_text(channel, 'managingEditor') or
                  _feed_text(channel, 'webMaster') or 'Unknown')
        entries = channel.findall('item')
        feed_metadata = _rss_feed_metadata(channel)
    else:
        raise ValueError('Expected RSS or Atom feed')
    chapters = []
    for entry in entries[:limit]:
        categories = []
        for category in entry.iter():
            if category.tag.rsplit('}', 1)[-1].casefold() != 'category':
                continue
            value = (category.get('term') or category.text or '').strip()
            if value and len(value) <= 120 and value not in categories:
                categories.append(value)
            if len(categories) >= 20:
                break
        if atom:
            link = next((l.get('href') for l in entry.findall(ATOM + 'link') if l.get('rel', 'alternate') == 'alternate'), None)
            chapter_title = entry.findtext(ATOM + 'title', default='Chapter')
            published = entry.findtext(ATOM + 'published') or entry.findtext(ATOM + 'updated')
        else:
            link = entry.findtext('link')
            chapter_title = entry.findtext('title', default='Chapter')
            published = entry.findtext('pubDate')
        if link:
            chapter_url = urllib.parse.urljoin(final, link.strip())
            if urllib.parse.urlsplit(chapter_url).scheme in ('http', 'https'):
                chapter = {'title': chapter_title.strip(), 'url': chapter_url}
                if categories:
                    chapter['categories'] = categories
                if atom:
                    video_id = entry.findtext(YOUTUBE + 'videoId')
                    channel_id = entry.findtext(YOUTUBE + 'channelId')
                    native = {'provider': 'youtube'}
                    if video_id:
                        native['video_id'] = video_id.strip()
                    if channel_id:
                        native['channel_id'] = channel_id.strip()
                    if video_id:
                        chapter['provider_metadata'] = native
                native_media = _rss_episode_metadata(entry, final, atom=atom)
                if native_media:
                    if 'provider_metadata' in chapter:
                        chapter['provider_metadata'].update(native_media)
                    else:
                        chapter['provider_metadata'] = native_media
                timestamp = _feed_timestamp(published)
                if timestamp:
                    chapter['published'] = timestamp
                chapters.append(chapter)
    # Feeds conventionally show newest first; package in reading order.
    chapters.reverse()
    result = {'title': title.strip(), 'author': author.strip() or 'Unknown', 'kind': kind,
              'source': final, 'selector': 'article,main', 'chapters': chapters}
    if atom:
        playlist_id = root.findtext(YOUTUBE + 'playlistId')
        channel_id = root.findtext(YOUTUBE + 'channelId')
        if playlist_id or channel_id:
            result['provider_metadata'] = {
                'provider': 'youtube', 'playlist_id': playlist_id,
                'channel_id': channel_id,
            }
    elif feed_metadata:
        result['provider_metadata'] = feed_metadata
    return result


def _discover_json_feed(feed, feed_url, kind, limit):
    """Adapt standard JSON Feed entries to the shared watched-feed manifest."""
    def author_names(record):
        if not isinstance(record, dict):
            return []
        values = record.get('authors')
        if values is None and record.get('author') is not None:
            values = [record['author']]
        if not isinstance(values, list):
            values = [values] if values else []
        result = []
        for value in values:
            name = value.get('name') if isinstance(value, dict) else value
            name = str(name or '').strip()
            if name and name not in result:
                result.append(name[:200])
        return result

    feed_author = ', '.join(author_names(feed)) or 'Unknown'
    chapters = []
    items = list(feed.get('items') or [])[:limit]
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        title = str(item.get('title') or '').strip() or 'Feed entry'
        raw_url = item.get('url') or item.get('external_url')
        item_url = urllib.parse.urljoin(feed_url, str(raw_url or '').strip()) if raw_url else ''
        parsed_item_url = urllib.parse.urlsplit(item_url)
        if (parsed_item_url.scheme not in {'http', 'https'} or not parsed_item_url.netloc or
                parsed_item_url.username or parsed_item_url.password):
            identity = str(item.get('id') or '\n'.join((title, str(item.get('date_published') or ''))))
            digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
            item_url = urllib.parse.urlsplit(feed_url)._replace(
                fragment=f'item-{digest}').geturl()
        tags = item.get('tags') or []
        categories = list(dict.fromkeys(
            str(value).strip()[:120] for value in tags
            if isinstance(value, str) and value.strip()))[:20]
        summary = (item.get('summary') or item.get('content_text') or
                   item.get('content_html') or '')
        summary = re.sub(r'<[^>]*>', ' ', html.unescape(str(summary)))
        summary = re.sub(r'\s+', ' ', summary).strip()[:4000]
        identity = str(item.get('id') or item_url).strip()[:500]
        metadata = {'provider': 'json_feed', 'entry_id': identity}
        if summary:
            metadata['summary'] = summary
        episode_type = str(item.get('episode_type') or item.get('item_type') or '').strip()
        if episode_type and len(episode_type) <= 40:
            metadata['episode_type'] = episode_type.casefold()
        for source_key, target_key, minimum, maximum in (
                ('season_number', 'season_number', 0, 999),
                ('episode_number', 'episode_number', 1, 9999)):
            try:
                value = int(item.get(source_key))
            except (TypeError, ValueError, OverflowError):
                continue
            if minimum <= value <= maximum:
                metadata[target_key] = value
        enclosures = []
        for attachment in (item.get('attachments') or [])[:20]:
            if not isinstance(attachment, dict):
                continue
            url = urllib.parse.urljoin(feed_url, str(attachment.get('url') or '').strip())
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or
                    parsed.username or parsed.password):
                continue
            enclosure = {'url': url}
            mime = str(attachment.get('mime_type') or '').strip()[:120]
            if mime:
                enclosure['mime_type'] = mime
            try:
                size = int(attachment.get('size_in_bytes'))
            except (TypeError, ValueError, OverflowError):
                size = None
            if size is not None and size >= 0:
                enclosure['length'] = size
            enclosures.append(enclosure)
        if enclosures:
            metadata['enclosures'] = enclosures
        chapter = {'title': title[:500], 'url': item_url,
                   'author': ', '.join(author_names(item)) or feed_author,
                   'provider_metadata': metadata}
        if categories:
            chapter['categories'] = categories
        published = _feed_timestamp(item.get('date_published') or item.get('date_modified'))
        if published:
            chapter['published'] = published
        chapters.append(chapter)
    return {'title': str(feed.get('title') or 'Untitled feed').strip()[:200],
            'author': feed_author, 'kind': kind, 'source': feed_url,
            'selector': 'article,main', 'chapters': chapters,
            'provider_metadata': {'provider': 'json_feed'}}


def _discover_json_schedule_feed(feed, feed_url, kind, limit):
    """Adapt a small explicit JSON media-schedule envelope to reviewable events."""
    title = str(feed.get('name') or feed.get('title') or 'Media schedule').strip()[:200]
    events = []
    for index, event in enumerate((feed.get('events') or [])[:limit]):
        if not isinstance(event, dict):
            continue
        event_title = str(event.get('name') or event.get('title') or 'Scheduled media event').strip()
        raw_url = event.get('url') or event.get('external_url')
        event_url = urllib.parse.urljoin(feed_url, str(raw_url or '').strip()) if raw_url else ''
        parsed_url = urllib.parse.urlsplit(event_url)
        identity = str(event.get('id') or event.get('uid') or f'{index}:{event_title}').strip()[:500]
        if (parsed_url.scheme not in {'http', 'https'} or not parsed_url.netloc or
                parsed_url.username or parsed_url.password):
            digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
            event_url = urllib.parse.urlsplit(feed_url)._replace(
                fragment=f'event-{digest}').geturl()
        start = event.get('start_date') or event.get('startDate') or event.get('release_date')
        if isinstance(start, dict):
            start = start.get('@value') or start.get('name')
        start_text = str(start or '').strip()
        schedule_date = None
        if re.match(r'^\d{4}-\d{2}-\d{2}', start_text):
            try:
                schedule_date = dt.date.fromisoformat(start_text[:10]).isoformat()
            except ValueError:
                pass
        raw_categories = event.get('categories') or event.get('tags') or []
        if isinstance(raw_categories, str):
            raw_categories = [raw_categories]
        categories = list(dict.fromkeys(
            str(value).strip()[:120] for value in raw_categories
            if isinstance(value, str) and value.strip()))[:20]
        summary = str(event.get('description') or event.get('summary') or '').strip()
        summary = re.sub(r'<[^>]*>', ' ', html.unescape(summary))
        summary = re.sub(r'\s+', ' ', summary).strip()[:4000]
        event_type = str(event.get('event_type') or event.get('eventType') or
                         event.get('category') or '').strip()[:80]
        provider_metadata = {'provider': 'json_schedule', 'entry_id': identity}
        if schedule_date:
            provider_metadata['schedule_date'] = schedule_date
            provider_metadata['schedule_date_precision'] = 'day'
        if event_type:
            provider_metadata['event_type'] = event_type
        if summary:
            provider_metadata['summary'] = summary
        location = event.get('location')
        if isinstance(location, dict):
            location = location.get('name') or location.get('url')
        if location:
            provider_metadata['location'] = str(location).strip()[:200]
        item = {'title': event_title[:500], 'url': event_url,
                'provider_metadata': provider_metadata}
        if categories:
            item['categories'] = categories
        published = _feed_timestamp(event.get('date_published') or event.get('datePublished'))
        if published:
            item['published'] = published
        events.append(item)
    return {'title': title, 'author': str(feed.get('author') or 'Calendar').strip()[:200],
            'kind': kind, 'source': feed_url, 'selector': 'article,main',
            'chapters': events, 'provider_metadata': {'provider': 'json_schedule'}}


def _jsonld_type_names(value):
    values = value if isinstance(value, list) else [value]
    names = []
    for item in values:
        if not isinstance(item, str):
            continue
        name = re.split(r'[/#]', item.rstrip('/#'))[-1].casefold()
        if name:
            names.append(name)
    return names


def _walk_jsonld_nodes(payload):
    """Walk common JSON-LD graph and series-membership containers with a hard cap."""
    pending = list(payload) if isinstance(payload, list) else [payload]
    cursor = 0
    while cursor < len(pending) and cursor < 5000:
        node = pending[cursor]
        cursor += 1
        if not isinstance(node, dict):
            continue
        yield node
        for key in ('@graph', 'itemListElement', 'events', 'mainEntity', 'hasPart',
                    'containsSeason', 'episode', 'episodes', 'season'):
            remaining = max(0, 5000 - len(pending))
            if not remaining:
                break
            value = node.get(key)
            if isinstance(value, list):
                pending.extend(value[:min(1000, remaining)])
            elif isinstance(value, dict):
                pending.append(value)
        item = node.get('item')
        if isinstance(item, dict) and len(pending) < 5000:
            pending.append(item)


def _jsonld_schedule_events(payload):
    """Find explicit Schema.org events and dated video entries in JSON-LD."""
    found = []
    for node in _walk_jsonld_nodes(payload):
        type_names = _jsonld_type_names(node.get('@type'))
        is_event = any(name == 'event' or name.endswith('event') for name in type_names)
        is_video_entry = any(name in {'tvepisode', 'videoobject'} for name in type_names)
        has_start = node.get('startDate') or node.get('start_date')
        has_video_date = (is_video_entry and
                          (node.get('datePublished') or node.get('uploadDate')))
        if (is_event and has_start) or (is_video_entry and (has_start or has_video_date)):
            found.append(node)
    return found


def _jsonld_schedule_value(value):
    if isinstance(value, dict):
        return value.get('name') or value.get('value') or value.get('@id') or ''
    if isinstance(value, list):
        values = []
        for item in value:
            normalized = str(_jsonld_schedule_value(item)).strip()
            if normalized:
                values.append(normalized)
        return ', '.join(values)
    return str(value or '').strip()


def _discover_jsonld_schedule(payload, feed_url, kind, limit):
    """Adapt Schema.org Event, BroadcastEvent, ScreeningEvent, and ItemList JSON-LD."""
    nodes = _jsonld_schedule_events(payload)[:limit]
    entries = []
    for index, event in enumerate(nodes):
        title = str(event.get('name') or event.get('headline') or
                    event.get('alternateName') or 'Scheduled media event').strip()
        raw_url = event.get('url') or event.get('@id')
        event_url = _feed_safe_url(raw_url, feed_url)
        event_identity = _jsonld_schedule_value(event.get('identifier')) or str(
            event.get('@id') or event_url or f'{index}:{title}')
        if not event_url:
            digest = hashlib.sha256(event_identity.encode('utf-8')).hexdigest()[:24]
            event_url = urllib.parse.urlsplit(feed_url)._replace(
                fragment=f'event-{digest}').geturl()

        start = event.get('startDate') or event.get('start_date')
        if isinstance(start, dict):
            start = start.get('@value') or start.get('value')
        start_text = str(start or '').strip()
        schedule_date = None
        if re.match(r'^\d{4}-\d{2}-\d{2}', start_text):
            try:
                schedule_date = dt.date.fromisoformat(start_text[:10]).isoformat()
            except ValueError:
                pass

        type_names = _jsonld_type_names(event.get('@type'))
        event_type = str(event.get('eventType') or event.get('event_type') or '').strip()
        if not event_type:
            if 'screeningevent' in type_names:
                event_type = 'screening'
            elif 'publicationevent' in type_names or 'broadcastevent' in type_names:
                event_type = 'availability'
        description = _jsonld_schedule_value(
            event.get('description') or event.get('disambiguatingDescription'))
        description = re.sub(r'<[^>]*>', ' ', html.unescape(description))
        description = re.sub(r'\s+', ' ', description).strip()[:4000]
        provider_metadata = {'provider': 'jsonld_schedule',
                             'entry_id': event_identity[:500],
                             'schema_types': type_names}
        if schedule_date:
            provider_metadata['schedule_date'] = schedule_date
            provider_metadata['schedule_date_precision'] = 'day'
        if event_type:
            provider_metadata['event_type'] = event_type[:80]
        status = _jsonld_schedule_value(event.get('eventStatus'))
        if status:
            provider_metadata['event_status'] = status[:120]
        if description:
            provider_metadata['summary'] = description
        location = _jsonld_schedule_value(event.get('location'))
        if location:
            provider_metadata['location'] = location[:200]
        categories = (event.get('keywords') or event.get('category') or
                      event.get('genre') or [])
        if isinstance(categories, str):
            categories = re.split(r'[,;]', categories)
        if not isinstance(categories, list):
            categories = [categories]
        normalized_categories = list(dict.fromkeys(
            str(_jsonld_schedule_value(value)).strip()[:120]
            for value in categories if _jsonld_schedule_value(value)))[:20]

        season = event.get('seasonNumber')
        episode = event.get('episodeNumber')
        season_record = event.get('partOfSeason')
        if isinstance(season_record, dict):
            season = season if season is not None else season_record.get('seasonNumber')
        for raw, key, minimum, maximum in (
                (season, 'season_number', 0, 999),
                (episode, 'episode_number', 1, 9999)):
            try:
                number = int(raw)
            except (TypeError, ValueError, OverflowError):
                continue
            if minimum <= number <= maximum:
                provider_metadata[key] = number
        if any(name == 'tvepisode' for name in type_names):
            provider_metadata['episode_type'] = 'episode'

        part_of_series = event.get('partOfSeries')
        if part_of_series:
            if isinstance(part_of_series, dict):
                series_name = _jsonld_schedule_value(part_of_series.get('name'))
                series_url = _feed_safe_url(part_of_series.get('url') or
                                            part_of_series.get('@id'), feed_url)
                if series_name:
                    provider_metadata['series_title'] = series_name[:200]
                if series_url:
                    provider_metadata['series_url'] = series_url
            else:
                provider_metadata['series_title'] = _jsonld_schedule_value(
                    part_of_series)[:200]

        video_object = event.get('video') or event.get('associatedMedia') or {}
        if isinstance(video_object, list):
            video_object = next((value for value in video_object
                                 if isinstance(value, dict)), {})
        if not isinstance(video_object, dict):
            video_object = {}
        raw_video_url = event.get('contentUrl') or video_object.get('contentUrl')
        video_url = _feed_safe_url(raw_video_url, feed_url) if raw_video_url else None
        if video_url:
            provider_metadata['content_url'] = video_url
        duration = _jsonld_schedule_value(event.get('duration') or
                                          video_object.get('duration'))
        if duration:
            provider_metadata['duration'] = duration[:80]
        thumbnail = event.get('thumbnailUrl') or video_object.get('thumbnailUrl')
        if isinstance(thumbnail, list):
            thumbnail = thumbnail[0] if thumbnail else None
        thumbnail_url = _feed_safe_url(thumbnail, feed_url) if thumbnail else None
        if thumbnail_url:
            provider_metadata['thumbnail_url'] = thumbnail_url

        entry = {'title': title[:500], 'url': event_url,
                 'provider_metadata': provider_metadata}
        creator = event.get('creator') or event.get('author')
        creator_name = _jsonld_schedule_value(creator)
        if creator_name:
            entry['author'] = creator_name[:200]
        if normalized_categories:
            entry['categories'] = normalized_categories
        published = event.get('datePublished') or event.get('uploadDate')
        if published:
            timestamp = _feed_timestamp(_jsonld_schedule_value(published))
            if timestamp:
                entry['published'] = timestamp
        entries.append(entry)

    payload_title = ((payload.get('name') or payload.get('title'))
                     if isinstance(payload, dict) else None)
    payload_author = (_jsonld_schedule_value(payload.get('creator') or
                                             payload.get('author'))
                      if isinstance(payload, dict) else '')
    if not payload_title or not payload_author:
        for node in _walk_jsonld_nodes(payload):
            types = _jsonld_type_names(node.get('@type'))
            if 'tvseries' not in types:
                continue
            payload_title = payload_title or node.get('name') or node.get('headline')
            payload_author = payload_author or _jsonld_schedule_value(
                node.get('creator') or node.get('author'))
            if payload_title and payload_author:
                break
    return {'title': str(payload_title or 'JSON-LD media schedule').strip()[:200],
            'author': str(payload_author or 'Schedule').strip()[:200],
            'kind': kind, 'source': feed_url,
            'selector': 'article,main', 'chapters': entries,
            'provider_metadata': {'provider': 'jsonld_schedule'}}


def _discover_ical_events(data, feed_url, kind, limit):
    """Read dated VEVENTs as feed entries while preserving event identity and text."""
    text = data.decode('utf-8-sig', errors='replace')
    unfolded = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        if line.startswith((' ', '\t')) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    calendar_name = 'Calendar events'
    events = []
    current = None
    for line in unfolded:
        if line == 'BEGIN:VEVENT':
            current = {}
            continue
        if line == 'END:VEVENT':
            if current:
                events.append(current)
            current = None
            continue
        if ':' not in line:
            continue
        name, raw_value = line.split(':', 1)
        property_name = name.split(';', 1)[0].upper()
        if current is None:
            value = _ical_unescape(raw_value)
            if property_name == 'X-WR-CALNAME' and value.strip():
                calendar_name = value.strip()[:200]
            continue
        if property_name == 'CATEGORIES':
            current.setdefault('categories', []).extend(
                _ical_unescape(item.strip()) for item in
                re.split(r'(?<!\\),', raw_value) if item.strip())
        elif property_name in {'UID', 'DTSTART', 'DTSTAMP', 'SUMMARY', 'DESCRIPTION',
                               'URL', 'STATUS', 'LOCATION'}:
            current[property_name.casefold()] = _ical_unescape(raw_value).strip()

    entries = []
    base_url = urllib.parse.urlsplit(feed_url)._replace(fragment='').geturl()
    for event in events:
        title = str(event.get('summary') or '').strip() or 'Calendar event'
        summary = str(event.get('description') or '').strip()
        categories = list(dict.fromkeys(event.get('categories') or []))[:20]
        uid = str(event.get('uid') or '').strip()
        identity = uid or '\n'.join((title, event.get('dtstart') or '', summary))
        event_key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        raw_url = str(event.get('url') or '').strip()
        event_url = urllib.parse.urljoin(feed_url, raw_url) if raw_url else ''
        parsed_event_url = urllib.parse.urlsplit(event_url)
        if (parsed_event_url.scheme not in {'http', 'https'} or not parsed_event_url.netloc or
                parsed_event_url.username or parsed_event_url.password):
            event_url = f'{base_url}#event-{event_key[:24]}'
        metadata = {'provider': 'ical', 'entry_id': uid or event_key,
                    'event_status': event.get('status'),
                    'schedule_date': _ical_schedule_date(event.get('dtstart')),
                    'schedule_date_precision': 'day' if _ical_schedule_date(
                        event.get('dtstart')) else None,
                    'location': event.get('location')}
        if summary:
            metadata['summary'] = summary[:4000]
        if metadata['schedule_date'] is None:
            metadata.pop('schedule_date_precision')
        metadata = {key: value for key, value in metadata.items() if value not in (None, '')}
        entry = {'title': title[:500], 'url': event_url,
                 'provider_metadata': metadata}
        if categories:
            entry['categories'] = categories
        published = _ical_timestamp(event.get('dtstamp'))
        if published:
            entry['published'] = published
        entries.append(entry)

    entries = entries[:limit]
    return {'title': calendar_name, 'author': 'Calendar', 'kind': kind,
            'source': feed_url, 'selector': 'article,main', 'chapters': entries,
            'provider_metadata': {'provider': 'ical'}}


def _ical_unescape(value):
    return re.sub(r'\\([nN,;\\])', lambda match: {
        'n': '\n', 'N': '\n', ',': ',', ';': ';', '\\': '\\'
    }[match.group(1)], str(value or ''))


def _ical_schedule_date(value):
    match = re.match(r'^(\d{4})(\d{2})(\d{2})(?:T\d{4}(?:\d{2})?(?:Z|[+-]\d{4})?)?$',
                     str(value or '').strip())
    if not match:
        return None
    try:
        return dt.date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return None


def _ical_timestamp(value):
    match = re.fullmatch(r'(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)',
                         str(value or '').strip())
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    try:
        parsed = dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    if match.group(7):
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.isoformat()


def _local_name(node):
    return str(node.tag).rsplit('}', 1)[-1].casefold()


def _feed_text(node, name):
    """Read a feed field by local XML name, regardless of its namespace."""
    for child in list(node):
        if _local_name(child) == name.casefold() and child.text:
            value = child.text.strip()
            if value:
                return value
    return None


def _feed_nodes(node, name):
    return [child for child in node.iter() if child is not node and _local_name(child) == name.casefold()]


def _feed_safe_url(value, base):
    url = urllib.parse.urljoin(base, str(value or '').strip())
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        return None
    return url


def _rss_feed_metadata(channel):
    podcast_type = _feed_text(channel, 'type')
    image_node = next(iter(_feed_nodes(channel, 'image')), None)
    image_url = (image_node.get('href') if image_node is not None else None)
    if not image_url:
        image_parent = next(iter(_feed_nodes(channel, 'image')), None)
        if image_parent is not None:
            image_url = _feed_text(image_parent, 'url')
    categories = []
    for node in _feed_nodes(channel, 'category'):
        value = str(node.get('text') or node.text or '').strip()
        if value and value not in categories:
            categories.append(value[:160])
        if len(categories) >= 50:
            break
    is_podcast = bool(podcast_type or _feed_nodes(channel, 'author') or
                      _feed_nodes(channel, 'episode') or _feed_nodes(channel, 'season'))
    metadata = {'provider': PODCAST_PROVIDER if is_podcast else 'rss'}
    if podcast_type:
        metadata['feed_type'] = podcast_type[:40]
    if image_url:
        metadata['image_url'] = image_url[:2000]
    if categories:
        metadata['categories'] = categories
    for key, local_name in (('language', 'language'), ('owner', 'name')):
        value = _feed_text(channel, local_name)
        if value and len(value) <= 300:
            metadata[key] = value
    return metadata


def _rss_episode_metadata(entry, base, atom=False):
    metadata = {}
    entry_id = (_feed_text(entry, 'id') if atom else
                _feed_text(entry, 'guid') or _feed_text(entry, 'id'))
    if entry_id and len(entry_id) <= 500:
        metadata['entry_id'] = entry_id
    season = _feed_text(entry, 'season')
    episode = _feed_text(entry, 'episode')
    try:
        season_number = int(season) if season is not None else None
        episode_number = int(episode) if episode is not None else None
    except (ValueError, TypeError, OverflowError):
        season_number = episode_number = None
    if season_number is not None and 0 <= season_number <= 999:
        metadata['season_number'] = season_number
    if episode_number is not None and 1 <= episode_number <= 9999:
        metadata['episode_number'] = episode_number
    episode_type = _feed_text(entry, 'episodetype')
    if episode_type and len(episode_type) <= 40:
        metadata['episode_type'] = episode_type.casefold()
    duration = _feed_text(entry, 'duration')
    if duration and len(duration) <= 40:
        metadata['duration'] = duration
    image_node = next(iter(_feed_nodes(entry, 'image')), None)
    if image_node is not None:
        image_url = _feed_safe_url(image_node.get('href') or image_node.get('url'), base)
        if image_url:
            metadata['image_url'] = image_url
    summary = _feed_text(entry, 'summary') or _feed_text(entry, 'description')
    if summary:
        summary = re.sub(r'<[^>]*>', ' ', html.unescape(summary))
        summary = re.sub(r'\s+', ' ', summary).strip()
        summary = re.sub(r'\s+([.,!?;:])', r'\1', summary)
        if summary:
            metadata['summary'] = summary[:4000]
    enclosures = []
    for node in entry.iter():
        local_name = _local_name(node)
        if local_name == 'link' and atom and str(node.get('rel') or '').casefold() != 'enclosure':
            continue
        if local_name not in {'enclosure', 'content', 'link'}:
            continue
        raw_url = node.get('url') or (node.get('href') if local_name == 'link' else None)
        if not raw_url:
            continue
        url = _feed_safe_url(raw_url, base)
        if not url:
            continue
        mime_type = str(node.get('type') or node.get('contentType') or '').strip()[:120]
        medium = str(node.get('medium') or '').strip()[:40]
        size = node.get('length') or node.get('fileSize') or node.get('size')
        item = {'url': url}
        if mime_type:
            item['mime_type'] = mime_type
        if medium:
            item['medium'] = medium
        try:
            size_number = int(size) if size is not None else None
            if size_number is not None and size_number >= 0:
                item['length'] = size_number
        except (ValueError, TypeError, OverflowError):
            pass
        if item not in enclosures:
            enclosures.append(item)
        if len(enclosures) >= 10:
            break
    if enclosures:
        metadata['enclosures'] = enclosures
    if not metadata:
        return None
    if enclosures or season_number is not None:
        metadata.setdefault('provider', PODCAST_PROVIDER)
    elif not atom:
        metadata.setdefault('provider', 'rss')
    return metadata


def _feed_timestamp(value):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    return parsed.isoformat()


class ChapterParser(HTMLParser):
    def __init__(self, selector, page_url):
        super().__init__(convert_charrefs=True)
        self.selectors = [s.strip() for s in selector.split(',')]
        self.page_url = page_url
        self.depth = 0
        self.skip = 0
        self.paragraph = 0
        self.buffer = []
        self.paragraphs = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth:
            self.depth += 1
        elif any((s == tag or s.startswith('.') and s[1:] in a.get('class', '').split()
                  or s.startswith('#') and s[1:] == a.get('id')) for s in self.selectors):
            self.depth = 1
        if not self.depth: return
        if tag in ('script', 'style', 'nav', 'aside'):
            self.skip += 1
        if self.skip: return
        if tag in ('p', 'h1', 'h2', 'h3', 'li', 'blockquote'):
            self.paragraph += 1
        if tag == 'img':
            candidate = next((a[k] for k in ('data-src', 'data-lazy-src', 'data-original', 'data-url', 'src') if a.get(k)), None)
            if not candidate and a.get('srcset'):
                candidate = a['srcset'].split(',', 1)[0].strip().split(' ', 1)[0]
            if candidate:
                url = urllib.parse.urljoin(self.page_url, candidate)
                if urllib.parse.urlsplit(url).scheme in ('http', 'https') and url not in self.images:
                    self.images.append(url)
        if tag in VOID_TAGS:
            self.depth -= 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS: self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.depth: return
        if self.skip and tag in ('script', 'style', 'nav', 'aside'):
            self.skip -= 1
        elif not self.skip and tag in ('p', 'h1', 'h2', 'h3', 'li', 'blockquote'):
            self.paragraph = max(0, self.paragraph - 1)
            value = re.sub(r'\s+', ' ', ''.join(self.buffer)).strip()
            if value: self.paragraphs.append(value)
            self.buffer.clear()
        self.depth -= 1

    def handle_data(self, data):
        if self.depth and self.paragraph and not self.skip:
            self.buffer.append(data)


def _fetch_image(url, referer, budget):
    req = urllib.request.Request(url, headers={'User-Agent': 'MediaIntakePrototype/0.1', 'Referer': referer})
    with open_url(req, timeout=30) as response:
        data = response.read(min(budget, 25_000_000) + 1)
        media = response.headers.get('Content-Type', '').split(';')[0].lower()
    if len(data) > min(budget, 25_000_000): raise ValueError('Image exceeds size limit')
    magic = _image_extension(data)
    if not magic or (media and not media.startswith('image/')):
        raise ValueError('Chapter image is not a supported image')
    return data, magic


def _image_extension(data):
    return ('.png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
            '.jpg' if data.startswith(b'\xff\xd8\xff') else
            '.gif' if data.startswith((b'GIF87a', b'GIF89a')) else
            '.webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None)


def _mangadex_image_urls(chapter):
    from .comics import mangadex_http_get
    chapter_id = chapter['mangadex_id']
    raw, _ = mangadex_http_get(f'https://api.mangadex.org/at-home/server/{chapter_id}', max_bytes=2_000_000)
    payload = json.loads(raw.decode('utf-8'))
    base_url = str(payload.get('baseUrl') or '')
    parsed = urllib.parse.urlsplit(base_url)
    host = (parsed.hostname or '').casefold()
    allowed_host = (host.endswith('.mangadex.org') or host.endswith('.mangadex.network'))
    if parsed.scheme != 'https' or not allowed_host:
        raise ValueError('MangaDex returned an unexpected image server')
    chapter_info = payload.get('chapter') or {}
    digest = str(chapter_info.get('hash') or '')
    filenames = chapter_info.get('data') or []
    if not digest or not isinstance(filenames, list) or not filenames:
        raise ValueError('MangaDex returned no chapter pages')
    output = []
    for filename in filenames:
        name = str(filename or '')
        if not name or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('MangaDex returned an invalid page name')
        page_url = (base_url.rstrip('/') + '/data/' + urllib.parse.quote(digest, safe='') + '/' +
                    urllib.parse.quote(name, safe=''))
        output.append(page_url)
    return output


def _comic_info(item):
    chapters = item.get('chapters') or []
    manga_chapter = next((chapter for chapter in chapters if chapter.get('mangadex_id')), None)
    if not chapters:
        return None
    comic_chapter = manga_chapter or chapters[0]
    source = comic_chapter.get('url') or item.get('source') or ''
    groups = (manga_chapter or {}).get('groups') or []
    notes = [('Source: MangaDex (' + source + ')') if manga_chapter else ('Source: ' + source)]
    if groups:
        notes.append('Scanlation group: ' + ', '.join(str(group) for group in groups))
    comic_meta = item.get('comic_metadata') or {}
    copyright_notice = str(comic_meta.get('copyrightNotice') or comic_meta.get('copyright') or '').strip()
    if copyright_notice:
        notes.append('Copyright: ' + copyright_notice)
    number = item.get('series_index')
    if isinstance(number, float):
        number = f'{number:g}'
    number = number or comic_chapter.get('number') or ''
    elements = {
        'Series': item.get('series') or item['title'],
        'Number': str(number),
        'Title': comic_chapter.get('title') or item['title'],
        'Writer': item.get('author') or 'Unknown',
        'Web': source,
        'ScanInformation': ', '.join(str(group) for group in groups),
        'Notes': '\n'.join(notes),
    }
    return ('<?xml version="1.0" encoding="utf-8"?>' +
            '<ComicInfo>' +
            ''.join(f'<{key}>{html.escape(value)}</{key}>' for key, value in elements.items() if value) +
            '</ComicInfo>')


def _make_epub(out, item, chapters):
    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, item['url']))
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        manifest, spine, nav = [], [], []
        for i, (title, paragraphs) in enumerate(chapters, 1):
            name = f'chapter-{i:04d}.xhtml'
            body = ''.join('<p>' + html.escape(p) + '</p>' for p in paragraphs)
            page = f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{html.escape(title)}</title></head><body><h1>{html.escape(title)}</h1>{body}</body></html>'
            archive.writestr('OEBPS/' + name, page, compress_type=zipfile.ZIP_DEFLATED)
            manifest.append(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            nav.append(f'<li><a href="{name}">{html.escape(title)}</a></li>')
        archive.writestr('OEBPS/nav.xhtml', '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol>' + ''.join(nav) + '</ol></nav></body></html>')
        opf = ('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:identifier id="id">urn:uuid:{uid}</dc:identifier><dc:title>{html.escape(item["title"])}</dc:title><dc:creator>{html.escape(item["author"])}</dc:creator><dc:language>en</dc:language></metadata><manifest>'
               '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' + ''.join(manifest) + '</manifest><spine>' + ''.join(spine) + '</spine></package>')
        archive.writestr('OEBPS/content.opf', opf)


def download_serial(db, row, root, max_bytes=MAX_FILE_BYTES, destination=None):
    item = json.loads(row['metadata'])
    validate_manifest(item)
    folder = Path(destination) if destination else Path(root) / 'downloads'
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.partial-', dir=folder)
    try:
        from .core import update_download_progress
        chapters, spent = [], 0
        with os.fdopen(fd, 'wb') as out:
            archive = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) if item['kind'] in IMAGE_KINDS else None
            try:
                for index, chapter in enumerate(item['chapters'], 1):
                    update_download_progress(db, row['id'], spent, None,
                                             f'Fetching chapter {index} of {len(item["chapters"])}')
                    if archive is not None:
                        if chapter.get('mangadex_id'):
                            image_urls = _mangadex_image_urls(chapter)
                            for number, image_url in enumerate(image_urls, 1):
                                from .comics import mangadex_http_get
                                data, final_image_url = mangadex_http_get(
                                    image_url, max_bytes=min(25_000_000, max_bytes - spent))
                                final_host = (urllib.parse.urlsplit(final_image_url).hostname or '').casefold()
                                if not (final_host.endswith('.mangadex.org') or
                                        final_host.endswith('.mangadex.network')):
                                    raise ValueError('MangaDex redirected an image outside its CDN')
                                ext = _image_extension(data)
                                if not ext:
                                    raise ValueError('MangaDex chapter contains an unsupported image')
                                spent += len(data)
                                if spent > max_bytes: raise ValueError('Series exceeds size limit')
                                archive.writestr(f'{index:04d}-{number:04d}{ext}', data)
                                update_download_progress(db, row['id'], spent, None,
                                                         f'Fetching chapter {index} of {len(item["chapters"])}')
                        elif chapter.get('images'):
                            referer = chapter.get('url') or item.get('source') or ''
                            for number, image_url in enumerate(chapter['images'], 1):
                                data, ext = _fetch_image(image_url, referer, max_bytes - spent)
                                spent += len(data)
                                if spent > max_bytes: raise ValueError('Series exceeds size limit')
                                archive.writestr(f'{index:04d}-{number:04d}{ext}', data)
                                update_download_progress(db, row['id'], spent, None,
                                                         f'Fetching chapter {index} of {len(item["chapters"])}')
                        else:
                            raw, final = http_get(chapter['url'], max_bytes=2_000_000)
                            parser = ChapterParser(item['selector'], final)
                            parser.feed(raw.decode('utf-8-sig', 'replace'))
                            if not parser.images: raise ValueError(f'No images in chapter {index}')
                            for number, image_url in enumerate(parser.images, 1):
                                data, ext = _fetch_image(image_url, final, max_bytes - spent)
                                spent += len(data)
                                if spent > max_bytes: raise ValueError('Series exceeds size limit')
                                archive.writestr(f'{index:04d}-{number:04d}{ext}', data)
                                update_download_progress(db, row['id'], spent, None,
                                                         f'Fetching chapter {index} of {len(item["chapters"])}')
                    else:
                        raw, final = http_get(chapter['url'], max_bytes=2_000_000)
                        parser = ChapterParser(item['selector'], final)
                        parser.feed(raw.decode('utf-8-sig', 'replace'))
                        if not parser.paragraphs: raise ValueError(f'No text in chapter {index}')
                        spent += len(raw)
                        if spent > min(max_bytes, 50_000_000): raise ValueError('Series text exceeds size limit')
                        chapters.append((chapter.get('title') or f'Chapter {index}', parser.paragraphs))
                        update_download_progress(db, row['id'], spent, None,
                                                 f'Fetching chapter {index} of {len(item["chapters"])}')
            finally:
                if archive is not None:
                    info = _comic_info(item)
                    if info:
                        archive.writestr('ComicInfo.xml', info.encode('utf-8'))
                    archive.close()
            if archive is None: _make_epub(out, item, chapters)
        if Path(tmp).stat().st_size > max_bytes: raise ValueError('Series exceeds size limit')
        check_download(tmp, item['format'])
        digest = hashlib.sha256(Path(tmp).read_bytes()).hexdigest()
        dest = folder / file_name(item)
        if dest.exists():
            if hashlib.sha256(dest.read_bytes()).hexdigest() == digest: os.unlink(tmp)
            else:
                dest = dest.with_name(dest.stem + ' [' + digest[:8] + ']' + dest.suffix)
                os.replace(tmp, dest)
        else: os.replace(tmp, dest)
        db.execute("UPDATE items SET state='downloaded', file_path=?, sha256=?, error=NULL, progress_bytes=NULL, progress_total=NULL, progress_message=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(dest.resolve()), digest, row['id']))
        db.commit()
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return dict(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())
