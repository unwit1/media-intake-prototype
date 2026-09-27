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

from .core import ATOM, MAX_FILE_BYTES, check_download, enqueue, file_name, http_get

KINDS = {'comic', 'manga', 'webcomic', 'webtoon', 'webfiction', 'fanfiction'}
IMAGE_KINDS = {'comic', 'manga', 'webcomic', 'webtoon'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
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


def discover_feed(feed_url, kind, limit=100):
    """Turn RSS/Atom or an SSS comic feed into an editable manifest."""
    if kind not in KINDS or not 1 <= limit <= 500: raise ValueError('Invalid kind or limit')
    parsed_url = urllib.parse.urlsplit(str(feed_url or ''))
    if (parsed_url.scheme not in ('http', 'https') or not parsed_url.netloc or
            parsed_url.username or parsed_url.password):
        raise ValueError('Feed URL must be public HTTP(S) without credentials')
    data, final = http_get(feed_url)
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
    if json_feed is not None:
        raise ValueError('Expected an RSS, Atom, or SSS ComicSeries feed')
    root = ET.fromstring(data)
    atom = root.tag == ATOM + 'feed'
    if atom:
        title = root.findtext(ATOM + 'title', default='Untitled serial')
        author = root.findtext(ATOM + 'author/' + ATOM + 'name', default='Unknown')
        entries = root.findall(ATOM + 'entry')
    elif root.tag.lower() == 'rss':
        channel = root.find('channel')
        if channel is None: raise ValueError('RSS has no channel')
        title = channel.findtext('title', default='Untitled serial')
        author = channel.findtext('managingEditor', default='Unknown')
        entries = channel.findall('item')
    else:
        raise ValueError('Expected RSS or Atom feed')
    chapters = []
    for entry in entries[:limit]:
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
                timestamp = _feed_timestamp(published)
                if timestamp:
                    chapter['published'] = timestamp
                chapters.append(chapter)
    # Feeds conventionally show newest first; package in reading order.
    chapters.reverse()
    return {'title': title.strip(), 'author': author.strip() or 'Unknown', 'kind': kind,
            'source': final, 'selector': 'article,main', 'chapters': chapters}


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
    with urllib.request.urlopen(req, timeout=30) as response:
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
