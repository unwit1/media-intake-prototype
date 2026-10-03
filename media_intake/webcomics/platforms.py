"""Blog-platform webcomics: Blogger, Substack and Tumblr through their public, keyless endpoints.

RSS from these platforms holds only recent posts; their public JSON endpoints
page through the whole archive. Each adapter:

- reads the archive newest-page-first in bounded pages, saving the position in
  the cursor; posts from older pages found on later checks are backfill;
- takes every image of a post in order (photosets and multi-panel posts);
- keeps only posts with images, skips reblogs unless asked, and filters to a
  comic tag/label when one is configured or clearly in use (``comic``,
  ``comics``, ``webcomic``...).

Ghost's Content API requires a key, so Ghost sites keep using their RSS feed.
"""
import html
import json
import re
import urllib.parse

from .adapters import Adapter, MetadataCandidate, merge_metadata, new_series
from .http import ExtractionError
from .models import Snapshot, WebcomicAsset, WebcomicEntry, canonicalize_url
from .wordpress import _images

COMIC_TAGS = ('comic', 'comics', 'webcomic', 'webcomics', 'comic strip', 'strip', 'manga', 'page')
PAGES_PER_CHECK = 20


def _strip(value):
    return html.unescape(re.sub(r'<[^>]+>', '', str(value or ''))).strip()


def _pick_tag(posts, configured=None):
    """The configured tag, or a comic tag used by a clear share of posts, else None (all image posts)."""
    if configured:
        return str(configured).casefold()
    counts = {}
    for post in posts:
        for tag in {tag.casefold() for tag in post['tags']}:
            counts[tag] = counts.get(tag, 0) + 1
    candidates = [(counts.get(tag, 0), tag) for tag in COMIC_TAGS]
    best = max(candidates)
    return best[1] if best[0] >= max(2, len(posts) // 5) else None


class _PlatformAdapter(Adapter):
    version = '1'
    hosts = ()

    def probe(self, url, response=None):
        host = (urllib.parse.urlsplit(url).hostname or '').casefold()
        return 95 if any(host == base or host.endswith('.' + base) for base in self.hosts) else 0

    def capabilities(self):
        return {'archive': True, 'incremental': 'paged_api', 'chapters': 'evidence', 'assets': True,
                'browser': False, 'authenticated': False}

    def fetch_page(self, client, url, page):
        """Return (posts, has_more); each post: id, url, title, published, html, tags, reblog."""
        raise NotImplementedError

    def site_title(self, client, url):
        return None

    def resolve_series(self, url, client, cursor=None, config=None):
        cursor, config = dict(cursor or {}), dict(config or {})
        state = dict(cursor.get(self.id) or {})
        page = int(state.get('next_page') or 0)
        budget = int(config.get('platform_pages_per_check', PAGES_PER_CHECK))
        posts, fetched, has_more = [], 0, True
        # Archive: walk every page once; afterwards only the newest page is re-read.
        start = page if not state.get('archive_done') else 0
        page = start
        while has_more and fetched < budget:
            batch, has_more = self.fetch_page(client, url, page)
            posts.extend(batch)
            fetched += 1
            page += 1
            if state.get('archive_done'):
                break
        if not state.get('archive_done'):
            state['next_page'] = page if has_more else None
            state['archive_done'] = not has_more
        tag = _pick_tag(posts, config.get('comic_tag'))
        if tag:
            state['comic_tag'] = tag
        tag = state.get('comic_tag')
        entries, seen = [], set()
        for post in posts:
            if post['id'] in seen:
                continue
            seen.add(post['id'])
            if post.get('reblog') and not config.get('include_reblogs'):
                continue
            if tag and tag not in {value.casefold() for value in post['tags']}:
                continue
            images = post.get('images') or _images(post.get('html') or '', post['url'])
            if not images:
                continue
            entries.append(WebcomicEntry(
                f"{self.id}:{post['id']}", canonicalize_url(post['url']), post['title'] or f"Post {post['id']}",
                published_at=post.get('published'),
                assets=[WebcomicAsset(image, index + 1, provenance={'provider': self.id, 'source_url': post['url']})
                        for index, image in enumerate(images)],
                provenance={'provider': self.id, 'native_id': post['id'], 'tags': post['tags']}))
        entries.sort(key=lambda entry: (entry.published_at or '', entry.source_id))
        for index, entry in enumerate(entries):
            entry.ordinal = index + 1
            if start:
                entry.provenance['backfill'] = True  # older posts found after newer ones
        series = new_series(url, self.site_title(client, url) or urllib.parse.urlsplit(url).hostname, self.id)
        merge_metadata(series, [MetadataCandidate('title', series.title, self.id, url)])
        return Snapshot(series, entries, [], complete=False, cursor={self.id: state},
                        metrics={'entry_count': len(entries), 'pages': fetched, 'comic_tag': tag,
                                 'incremental': bool(state.get('archive_done'))})

    def resolve_assets(self, entry, client, config=None):
        if not entry.assets:
            raise ExtractionError('This post has no images')
        return entry


class BloggerAdapter(_PlatformAdapter):
    """Blogger's public JSON feed: /feeds/posts/default?alt=json, paged by start-index (oldest first)."""
    id, hosts, per_page = 'blogger', ('blogspot.com',), 100

    def _feed(self, url):
        parts = urllib.parse.urlsplit(url)
        return f'{parts.scheme}://{parts.netloc}/feeds/posts/default'

    def fetch_page(self, client, url, page):
        params = {'alt': 'json', 'max-results': self.per_page, 'start-index': page * self.per_page + 1,
                  'orderby': 'published'}
        response = client.get(f'{self._feed(url)}?{urllib.parse.urlencode(params)}', conditional=False)
        feed = json.loads(response.body.decode('utf-8')).get('feed') or {}
        total = int((feed.get('openSearch$totalResults') or {}).get('$t') or 0)
        posts = []
        for item in feed.get('entry') or []:
            link = next((link['href'] for link in item.get('link') or [] if link.get('rel') == 'alternate'), None)
            if not link:
                continue
            posts.append({'id': (item.get('id') or {}).get('$t', link).rsplit('-', 1)[-1], 'url': link,
                          'title': _strip((item.get('title') or {}).get('$t')),
                          'published': (item.get('published') or {}).get('$t'),
                          'html': (item.get('content') or item.get('summary') or {}).get('$t', ''),
                          'tags': [category.get('term', '') for category in item.get('category') or []]})
        return list(reversed(posts)), (page + 1) * self.per_page < total

    def site_title(self, client, url):
        try:
            response = client.get(f'{self._feed(url)}?alt=json&max-results=0', conditional=False)
            return _strip(((json.loads(response.body.decode('utf-8')).get('feed') or {}).get('title') or {}).get('$t'))
        except Exception:
            return None


class SubstackAdapter(_PlatformAdapter):
    """Substack's public archive API (/api/v1/archive), then /api/v1/posts/<slug> for each body."""
    id, hosts, per_page = 'substack', ('substack.com',), 25

    def _origin(self, url):
        parts = urllib.parse.urlsplit(url)
        return f'{parts.scheme}://{parts.netloc}'

    def fetch_page(self, client, url, page):
        origin = self._origin(url)
        params = urllib.parse.urlencode({'sort': 'new', 'offset': page * self.per_page, 'limit': self.per_page})
        listing = json.loads(client.get(f'{origin}/api/v1/archive?{params}', conditional=False).body.decode('utf-8'))
        posts = []
        for item in listing or []:
            if item.get('audience') not in (None, 'everyone'):
                continue  # paid posts: never fetched
            try:
                detail = json.loads(client.get(f"{origin}/api/v1/posts/{item['slug']}", conditional=False).body.decode('utf-8'))
            except Exception:
                continue
            posts.append({'id': str(item.get('id') or item['slug']), 'url': item.get('canonical_url') or f"{origin}/p/{item['slug']}",
                          'title': item.get('title') or '', 'published': item.get('post_date'),
                          'html': detail.get('body_html') or '',
                          'tags': [tag.get('name', '') for tag in item.get('postTags') or []]})
        return list(reversed(posts)), len(listing or []) == self.per_page

    def site_title(self, client, url):
        try:
            data = json.loads(client.get(f'{self._origin(url)}/api/v1/publication', conditional=False).body.decode('utf-8'))
            return data.get('name')
        except Exception:
            return None


class TumblrAdapter(_PlatformAdapter):
    """Tumblr's keyless v1 read API (/api/read/json): photosets, captions and reblog markers."""
    id, hosts, per_page = 'tumblr', ('tumblr.com',), 50

    def fetch_page(self, client, url, page):
        parts = urllib.parse.urlsplit(url)
        params = urllib.parse.urlencode({'num': self.per_page, 'start': page * self.per_page})
        body = client.get(f'{parts.scheme}://{parts.netloc}/api/read/json?{params}', conditional=False).body.decode('utf-8')
        match = re.search(r'=\s*(\{.*\})\s*;?\s*$', body, re.S)
        data = json.loads(match.group(1) if match else body)
        self._title = (data.get('tumblelog') or {}).get('title')
        posts = []
        for item in data.get('posts') or []:
            photos = [photo.get('photo-url-1280') for photo in item.get('photos') or [] if photo.get('photo-url-1280')]
            if not photos and item.get('photo-url-1280'):
                photos = [item['photo-url-1280']]
            caption = item.get('photo-caption') or item.get('regular-body') or ''
            posts.append({'id': str(item.get('id')), 'url': item.get('url-with-slug') or item.get('url'),
                          'title': _strip(item.get('regular-title') or caption)[:120],
                          'published': item.get('date-gmt'), 'html': caption,
                          'images': photos, 'tags': item.get('tags') or [],
                          'reblog': bool(item.get('reblogged-from-url') or item.get('reblogged-root-url'))})
        total = int(data.get('posts-total') or 0)
        return list(reversed(posts)), (page + 1) * self.per_page < total

    def site_title(self, client, url):
        return getattr(self, '_title', None)


ADAPTERS = (BloggerAdapter, SubstackAdapter, TumblrAdapter)
