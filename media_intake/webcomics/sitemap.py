"""Sitemap discovery: archive candidates for comic sites without a feed or API.

- Sitemaps come from robots.txt ``Sitemap:`` lines, then the common locations
  (/sitemap.xml, /sitemap_index.xml, /wp-sitemap.xml).
- Sitemap indexes and URL sets (optionally gzip-compressed) are read within
  bounds: same host only, no revisits, 50 files, 50,000 URLs, 10 MB per file.
- Only URLs that look like comic pages are kept (an include pattern can be
  configured); tag, category, author, feed, search and media URLs are dropped.
- The site is adopted only with positive evidence: sampled candidate pages must
  contain comic images. Entries load their images lazily when downloaded.
- ``lastmod`` becomes an entry revision: a newer date marks the page as updated;
  it never removes entries (a sitemap is not proof that a page was deleted).
"""
import gzip
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .adapters import Adapter, MetadataCandidate, image_assets, merge_metadata, new_series
from .extraction import Document
from .http import ExtractionError
from .models import Snapshot, WebcomicEntry, canonicalize_url, natural_key

SITEMAP_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
COMMON_LOCATIONS = ('/sitemap.xml', '/sitemap_index.xml', '/wp-sitemap.xml')
MAX_FILES, MAX_URLS, MAX_BYTES = 50, 50_000, 10_000_000
COMIC_PATH = re.compile(r'/(comic|comics|strip|strips|page|pages|episode|episodes|chapter|chapters|webcomic|'
                        r'manga|issue|archive/\d)[s]?(/|-|$)', re.I)
EXCLUDED_PATH = re.compile(r'/(tag|tags|category|categories|author|authors|feed|search|wp-json|wp-content|'
                           r'wp-admin|cart|shop|store|product|about|contact|privacy|terms|login|page/\d+/?$)(/|$)', re.I)
MEDIA_SUFFIX = re.compile(r'\.(jpe?g|png|gif|webp|svg|css|js|pdf|zip|xml|txt)$', re.I)
IMAGE_SELECTOR = ('#comic img,.comic img,img.comic,#cc-comic,#comic-image img,.webcomic-image img,'
                  '.comic-page img,#comicimage,.toocheke-comic img,.mangapress-comic img')
SAMPLE_PAGES = 3


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or '').casefold().removeprefix('www.')


def _xml(body, url):
    if body[:2] == b'\x1f\x8b':
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise ExtractionError(f'Could not unpack the compressed sitemap {url}') from exc
    if len(body) > MAX_BYTES:
        raise ExtractionError(f'Sitemap {url} is larger than {MAX_BYTES // 1_000_000} MB')
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise ExtractionError(f'Sitemap {url} is not valid XML') from exc


def _children(node, name):
    return node.findall(f'{SITEMAP_NS}{name}') or node.findall(name)


def _loc(node):
    found = node.find(f'{SITEMAP_NS}loc')
    found = found if found is not None else node.find('loc')
    return (found.text or '').strip() if found is not None and found.text else ''


def _lastmod(node):
    found = node.find(f'{SITEMAP_NS}lastmod')
    found = found if found is not None else node.find('lastmod')
    return (found.text or '').strip()[:40] if found is not None and found.text else None


def sitemap_locations(client, site_url):
    """Sitemap URLs declared in robots.txt, otherwise the common locations."""
    parts = urllib.parse.urlsplit(site_url)
    origin = f'{parts.scheme}://{parts.netloc}'
    declared = []
    try:
        robots = client.get(origin + '/robots.txt', conditional=False)
        for line in robots.body.decode('utf-8', 'replace').splitlines():
            key, _, value = line.partition(':')
            if key.strip().casefold() == 'sitemap' and value.strip():
                declared.append(urllib.parse.urljoin(origin, value.strip()))
    except Exception:
        pass
    return declared or [origin + path for path in COMMON_LOCATIONS]


def is_comic_candidate(url, include=None):
    path = urllib.parse.urlsplit(url).path
    if MEDIA_SUFFIX.search(path) or EXCLUDED_PATH.search(path):
        return False
    if include:
        return bool(re.search(include, url))
    return bool(COMIC_PATH.search(path))


def collect_urls(client, site_url, include=None, locations=None):
    """Stream candidate (url, lastmod) pairs from the site's sitemaps, within bounds."""
    host = _host(site_url)
    pending = list(locations or sitemap_locations(client, site_url))
    visited, found, warnings, files = set(), {}, [], 0
    while pending and files < MAX_FILES:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if _host(url) != host:
            warnings.append(f'Skipped a sitemap on another site: {url}')
            continue
        try:
            response = client.get(url, max_bytes=MAX_BYTES, conditional=False)
        except Exception as exc:
            if locations is None and url.endswith(COMMON_LOCATIONS):
                continue  # common locations are guesses
            warnings.append(f'Could not read sitemap {url}: {exc}')
            continue
        files += 1
        root = _xml(response.body, url)
        tag = root.tag.rsplit('}', 1)[-1]
        if tag == 'sitemapindex':
            for child in _children(root, 'sitemap'):
                location = _loc(child)
                if location and location not in visited:
                    pending.append(urllib.parse.urljoin(url, location))
            continue
        for child in _children(root, 'url'):
            location = _loc(child)
            if not location:
                continue
            try:
                page = canonicalize_url(urllib.parse.urljoin(url, location))
            except ValueError:
                continue
            if _host(page) != host or not is_comic_candidate(page, include):
                continue
            lastmod = _lastmod(child)
            if page not in found or (lastmod or '') > (found[page] or ''):
                found[page] = lastmod
            if len(found) >= MAX_URLS:
                warnings.append(f'Stopped at {MAX_URLS:,} sitemap URLs')
                return found, warnings
    if pending:
        warnings.append(f'Stopped after {MAX_FILES} sitemap files')
    return found, warnings


def _title_from_url(url):
    slug = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1])
    slug = re.sub(r'\.(html?|php)$', '', slug)
    return re.sub(r'[-_]+', ' ', slug).strip().capitalize() or url


class SitemapAdapter(Adapter):
    id, version = 'sitemap', '1'

    def capabilities(self):
        return {'archive': True, 'incremental': 'lastmod', 'chapters': 'evidence', 'assets': True,
                'browser': False, 'authenticated': False}

    def validate(self, client, urls, selector=IMAGE_SELECTOR):
        """Positive evidence: most sampled candidate pages contain comic images."""
        sample = list(urls)[:SAMPLE_PAGES]
        hits = 0
        for url in sample:
            try:
                response = client.get(url, conditional=False)
            except Exception:
                continue
            if image_assets(Document(response.body), selector, response.url):
                hits += 1
        return bool(sample) and hits >= min(2, len(sample))

    def resolve_series(self, url, client, cursor=None, config=None):
        cursor, config = dict(cursor or {}), dict(config or {})
        include = config.get('sitemap_include')
        found, warnings = collect_urls(client, url, include, config.get('sitemap_urls'))
        if not found:
            raise ExtractionError('The site has no sitemap entries that look like comic pages')
        ordered = sorted(found, key=natural_key)
        state = dict(cursor.get('sitemap') or {})
        if not state.get('validated'):
            if not self.validate(client, ordered, config.get('image_selector') or IMAGE_SELECTOR):
                raise ExtractionError('Sitemap pages do not contain recognizable comic images; a recipe is required')
            state['validated'] = True
        entries = [WebcomicEntry(page, page, _title_from_url(page), ordinal=index + 1, revision=found[page],
                                 provenance={'provider': self.id, 'lastmod': found[page]})
                   for index, page in enumerate(ordered)]
        title = None
        try:
            home = client.get(url)
            nodes = Document(home.body).select('title')
            title = next((node.text() for node in nodes if node.text()), None)
        except Exception:
            pass
        series = new_series(url, title or _host(url), self.id)
        merge_metadata(series, [MetadataCandidate('title', series.title, self.id, url)])
        return Snapshot(series, entries, [], complete=False, warnings=warnings, cursor={'sitemap': state},
                        metrics={'entry_count': len(entries), 'sitemap_urls': len(found)})

    def resolve_assets(self, entry, client, config=None):
        response = client.get(entry.source_url)
        document = Document(response.body)
        entry.assets = image_assets(document, (config or {}).get('image_selector') or IMAGE_SELECTOR, response.url)
        if not entry.assets:
            raise ExtractionError('No comic images were found on this page')
        return entry
