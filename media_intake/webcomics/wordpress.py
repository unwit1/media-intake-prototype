"""Native WordPress REST API adapter for comic post types.

Many webcomics run on WordPress with a comic plugin (Comic Easel, Toocheke,
Webcomic, MangaPress) that registers a dedicated post type. Reading that post
type through the public REST API gives the complete archive with stable post
IDs, every image in order, publication dates and chapter terms, without
scraping. Ordinary blog posts are never treated as comics.

- The API root comes from the page's ``rel="https://api.w.org/"`` link or Link
  header, so subdirectory installs, custom REST prefixes and ``?rest_route=``
  sites all work.
- A post type is used only when its slug or name looks like a comic type and a
  sample of its posts carries images.
- The first checks walk the archive oldest-first in bounded pages, saving the
  position; later checks ask only for posts modified since the last check minus
  an overlap, so edits (for example a post moved to another chapter) are seen.
"""
import datetime as dt
import html
import json
import re
import urllib.parse

from .adapters import Adapter, MetadataCandidate, merge_metadata, new_series
from .http import ExtractionError
from .models import Snapshot, WebcomicAsset, WebcomicChapter, WebcomicEntry, canonicalize_url, identity

API_REL = 'https://api.w.org/'
COMIC_TYPE = re.compile(r'(?:^|[-_])(comic|comics|webcomic\d*|manga|strip|strips|ce_comic|mangapress_comic)(?:$|[-_])', re.I)
CHAPTER_TAXONOMIES = ('chapters', 'chapter', 'comic-chapter', 'comic_chapter', 'webcomic_storyline',
                      'storyline', 'storylines', 'series_chapter', 'mangapress_series')
PER_PAGE = 100
PAGES_PER_CHECK = 20
MODIFIED_OVERLAP = dt.timedelta(days=1)
_LINK_HEADER = re.compile(r'<([^>]+)>\s*;\s*rel="?https://api\.w\.org/"?', re.I)
_LINK_TAG = re.compile(r'<link[^>]+rel=["\']https://api\.w\.org/["\'][^>]*>', re.I)
_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_IMG = re.compile(r'<img\b[^>]*>', re.I)
_ATTR = re.compile(r'\b(data-src|data-lazy-src|data-original|src)=["\']([^"\']+)["\']', re.I)


def api_root(response):
    """The REST API root advertised by a WordPress page, if any."""
    header = (response.headers or {}).get('link') or ''
    match = _LINK_HEADER.search(header)
    if match:
        return urllib.parse.urljoin(response.url, match.group(1))
    head = response.body[:200_000].decode('utf-8', 'replace')
    tag = _LINK_TAG.search(head)
    if tag:
        href = _HREF.search(tag.group(0))
        if href:
            return urllib.parse.urljoin(response.url, html.unescape(href.group(1)))
    return None


def endpoint(root, route, params=None):
    """Build an API URL for `route` under `root` (pretty or ?rest_route= style)."""
    parts = urllib.parse.urlsplit(root)
    query = dict(urllib.parse.parse_qsl(parts.query))
    if 'rest_route' in query:
        query['rest_route'] = '/' + route.strip('/')
        query.update(params or {})
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    path = parts.path.rstrip('/') + '/' + route.strip('/')
    return urllib.parse.urlunsplit(parts._replace(path=path, query=urllib.parse.urlencode(params or {})))


def _text(value):
    if isinstance(value, dict):
        value = value.get('rendered', '')
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()


def _images(content_html, base):
    urls = []
    for tag in _IMG.findall(content_html or ''):
        attrs = {name.casefold(): value for name, value in _ATTR.findall(tag)}
        src = next((attrs[name] for name in ('data-src', 'data-lazy-src', 'data-original', 'src') if attrs.get(name)), None)
        if not src or src.startswith('data:'):
            continue
        try:
            url = canonicalize_url(urllib.parse.urljoin(base, html.unescape(src)))
        except ValueError:
            continue
        if url not in urls:
            urls.append(url)
    return urls


class WordPressAdapter(Adapter):
    id, version = 'wordpress_api', '1'

    def capabilities(self):
        return {'archive': True, 'incremental': 'modified_after', 'chapters': 'taxonomy',
                'assets': True, 'browser': False, 'authenticated': False}

    def _json(self, client, url):
        response = client.get(url, conditional=False)
        try:
            return json.loads(response.body.decode('utf-8-sig')), response
        except (UnicodeDecodeError, ValueError) as exc:
            raise ExtractionError(f'WordPress API returned invalid JSON at {url}') from exc

    def _posts(self, client, root, rest_base, params):
        data, response = self._json(client, endpoint(root, f'wp/v2/{rest_base}', params))
        if not isinstance(data, list):
            raise ExtractionError('WordPress API did not return a post list')
        try:
            total_pages = int((response.headers or {}).get('x-wp-totalpages') or 0)
        except ValueError:
            total_pages = 0
        return data, total_pages

    def detect(self, response, client):
        """Return {'api_root', 'rest_base', 'type'} when the site exposes a comic post type."""
        root = api_root(response)
        if not root:
            return None
        try:
            types, _response = self._json(client, endpoint(root, 'wp/v2/types'))
        except Exception:
            return None
        if not isinstance(types, dict):
            return None
        candidates = []
        for slug, info in types.items():
            if not isinstance(info, dict) or slug in ('post', 'page', 'attachment') or not info.get('rest_base'):
                continue
            if COMIC_TYPE.search(slug) or COMIC_TYPE.search(str(info.get('name') or '')):
                candidates.append((slug, info['rest_base']))
        for slug, rest_base in candidates:
            try:
                sample, _pages = self._posts(client, root, rest_base, {'per_page': 3, '_embed': 1})
            except Exception:
                continue
            # Positive evidence: the type's posts actually carry comic images.
            if sample and any(self._entry_images(post) for post in sample):
                return {'api_root': root, 'rest_base': rest_base, 'type': slug}
        return None

    def probe(self, url, response=None):
        return 0  # selected by the resolver after detect(), never by score alone

    def _entry_images(self, post):
        base = post.get('link') or ''
        images = _images((post.get('content') or {}).get('rendered', ''), base)
        if not images:
            for media in (post.get('_embedded') or {}).get('wp:featuredmedia') or []:
                if isinstance(media, dict) and media.get('source_url'):
                    try:
                        images.append(canonicalize_url(media['source_url']))
                    except ValueError:
                        pass
        return images

    def _entry(self, post, api_url):
        link = canonicalize_url(post.get('link') or api_url)
        terms = [term for group in (post.get('_embedded') or {}).get('wp:term') or [] for term in group or []
                 if isinstance(term, dict)]
        chapter = next((term for taxonomy in CHAPTER_TAXONOMIES for term in terms
                        if term.get('taxonomy') == taxonomy), None)
        entry = WebcomicEntry(
            f"wp:{post['id']}", link, _text(post.get('title')) or f"Post {post['id']}",
            published_at=post.get('date_gmt') or post.get('date'),
            assets=[WebcomicAsset(url, index + 1, provenance={'provider': self.id, 'source_url': link})
                    for index, url in enumerate(self._entry_images(post))],
            revision=post.get('modified_gmt') or post.get('modified'),
            provenance={'provider': self.id, 'api_url': api_url, 'native_id': post['id'],
                        'modified_gmt': post.get('modified_gmt')})
        if chapter:
            entry.chapter_id = identity(f"wp-term:{chapter.get('taxonomy')}:{chapter.get('id')}")
            entry.provenance['chapter_term'] = {'id': chapter.get('id'), 'name': _text(chapter.get('name')),
                                                'taxonomy': chapter.get('taxonomy'), 'link': chapter.get('link')}
        return entry

    def resolve_series(self, url, client, cursor=None, config=None):
        cursor, config = dict(cursor or {}), dict(config or {})
        state = dict(cursor.get('wordpress') or {})
        site = state.get('site') or config.get('wordpress') or {}
        if not site.get('api_root'):
            page = client.get(url)
            site = self.detect(page, client)
            if not site:
                raise ExtractionError('This WordPress site exposes no comic post type through its REST API')
        state['site'] = site  # persisted with the cursor, so later checks skip detection
        root, rest_base = site['api_root'], site['rest_base']
        params = {'per_page': PER_PAGE, 'orderby': 'date', 'order': 'asc', '_embed': 1}
        backfilling = not state.get('archive_done')
        if not backfilling and state.get('max_modified_gmt'):
            since = dt.datetime.fromisoformat(state['max_modified_gmt'].replace('Z', '')) - MODIFIED_OVERLAP
            params['modified_after'] = since.strftime('%Y-%m-%dT%H:%M:%S')
            params['orderby'] = 'modified'
        page = int(state.get('next_page') or 1) if backfilling else 1
        budget = int(config.get('wordpress_pages_per_check', PAGES_PER_CHECK))
        posts, fetched, total_pages = [], 0, 0
        while fetched < budget:
            batch, total_pages = self._posts(client, root, rest_base, dict(params, page=page))
            posts.extend(batch)
            fetched += 1
            if not batch or (total_pages and page >= total_pages) or len(batch) < PER_PAGE:
                page = None
                break
            page += 1
        if backfilling:
            state['next_page'] = page
            state['archive_done'] = page is None
        api_url = endpoint(root, f'wp/v2/{rest_base}')
        entries = [self._entry(post, api_url) for post in posts if isinstance(post, dict) and post.get('id')]
        modified = [entry.provenance.get('modified_gmt') for entry in entries if entry.provenance.get('modified_gmt')]
        if modified:
            state['max_modified_gmt'] = max([state.get('max_modified_gmt') or '', *modified])
        if backfilling and int((cursor.get('wordpress') or {}).get('next_page') or 1) > 1:
            # Later archive pages are newer posts of the existing backlog, not new releases.
            for entry in entries:
                entry.provenance['archive_continuation'] = True
        entries.sort(key=lambda entry: (entry.published_at or '', entry.source_id))
        for index, entry in enumerate(entries):
            entry.ordinal = index + 1
        chapters = {}
        for entry in entries:
            term = entry.provenance.get('chapter_term')
            if not term:
                continue
            chapter = chapters.get(entry.chapter_id)
            if chapter is None:
                chapter = chapters[entry.chapter_id] = WebcomicChapter(
                    entry.chapter_id, term['name'] or 'Chapter', ordinal=entry.ordinal,
                    organization='SOURCE_DECLARED',
                    evidence=[{'kind': 'wordpress_taxonomy', 'taxonomy': term['taxonomy'],
                               'term_id': term['id'], 'source_url': term.get('link')}])
            chapter.entry_ids.append(entry.source_id)
        title, author = None, None
        try:
            info, _response = self._json(client, endpoint(root, ''))
            title = _text(info.get('name')) if isinstance(info, dict) else None
        except Exception:
            pass
        author = next((((post.get('_embedded') or {}).get('author') or [{}])[0].get('name')
                       for post in posts if isinstance(post, dict)), None)
        series = new_series(url, title or urllib.parse.urlsplit(url).hostname, self.id)
        candidates = [MetadataCandidate('title', series.title, self.id, root)]
        if author:
            candidates.append(MetadataCandidate('creators', [{'name': author, 'role': 'author'}], self.id, root))
        merge_metadata(series, candidates)
        return Snapshot(series, entries, list(chapters.values()), complete=False,
                        cursor={'wordpress': state},
                        metrics={'entry_count': len(entries), 'api_pages': fetched,
                                 'archive_done': bool(state.get('archive_done', True)),
                                 'incremental': not backfilling, 'post_type': site.get('type')})

    def resolve_assets(self, entry, client, config=None):
        if not entry.assets:
            raise ExtractionError('This WordPress comic post has no images')
        return entry
