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
        url = urllib.parse.urlsplit(chapter.get('url', ''))
        if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password:
            raise ValueError('Chapter URLs must be HTTP(S) without credentials')
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
    """Turn an RSS/Atom chapter feed into an editable manifest."""
    if kind not in KINDS or not 1 <= limit <= 500: raise ValueError('Invalid kind or limit')
    data, final = http_get(feed_url)
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
        else:
            link = entry.findtext('link')
            chapter_title = entry.findtext('title', default='Chapter')
        if link:
            chapter_url = urllib.parse.urljoin(final, link.strip())
            if urllib.parse.urlsplit(chapter_url).scheme in ('http', 'https'):
                chapters.append({'title': chapter_title.strip(), 'url': chapter_url})
    # Feeds conventionally show newest first; package in reading order.
    chapters.reverse()
    return {'title': title.strip(), 'author': author.strip(), 'kind': kind,
            'source': final, 'selector': 'article,main', 'chapters': chapters}


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
    magic = ('.png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
             '.jpg' if data.startswith(b'\xff\xd8\xff') else
             '.gif' if data.startswith((b'GIF87a', b'GIF89a')) else
             '.webp' if data.startswith(b'RIFF') and data[8:12] == b'WEBP' else None)
    if not magic or (media and not media.startswith('image/')):
        raise ValueError('Chapter image is not a supported image')
    return data, magic


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


def download_serial(db, row, root, max_bytes=MAX_FILE_BYTES):
    item = json.loads(row['metadata'])
    validate_manifest(item)
    folder = Path(root) / 'downloads'
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.partial-', dir=folder)
    try:
        chapters, spent = [], 0
        with os.fdopen(fd, 'wb') as out:
            archive = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) if item['kind'] in IMAGE_KINDS else None
            try:
                for index, chapter in enumerate(item['chapters'], 1):
                    raw, final = http_get(chapter['url'], max_bytes=2_000_000)
                    parser = ChapterParser(item['selector'], final)
                    parser.feed(raw.decode('utf-8-sig', 'replace'))
                    if archive is not None:
                        if not parser.images: raise ValueError(f'No images in chapter {index}')
                        for number, image_url in enumerate(parser.images, 1):
                            data, ext = _fetch_image(image_url, final, max_bytes - spent)
                            spent += len(data)
                            if spent > max_bytes: raise ValueError('Series exceeds size limit')
                            archive.writestr(f'{index:04d}-{number:04d}{ext}', data)
                    else:
                        if not parser.paragraphs: raise ValueError(f'No text in chapter {index}')
                        spent += len(raw)
                        if spent > min(max_bytes, 50_000_000): raise ValueError('Series text exceeds size limit')
                        chapters.append((chapter.get('title') or f'Chapter {index}', parser.paragraphs))
            finally:
                if archive is not None: archive.close()
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
        db.execute("UPDATE items SET state='downloaded', file_path=?, sha256=?, error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(dest.resolve()), digest, row['id']))
        db.commit()
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return dict(db.execute('SELECT * FROM items WHERE id=?', (row['id'],)).fetchone())
