"""Independently testable extractors, recipes and bounded source enumeration."""
import datetime as dt
import json
import re
import urllib.parse
from .chapters import group_entries
from .extraction import Document, Recipe
from .http import ExtractionError
from .metadata import REGISTRY as METADATA, MetadataCandidate, merge_metadata
from .models import (Snapshot, WebcomicAsset, WebcomicEntry, WebcomicSeries,
                     canonicalize_url, identity, natural_key)
from ..serial import ChapterParser, parse_feed


def new_series(url, title, platform):
    url = canonicalize_url(url)
    return WebcomicSeries(identity(url), title, url, platform, homepage=url,
                         content_provenance={'provider': platform, 'source_url': url})


def image_assets(document, selector, base):
    images, seen = [], set()
    for node in document.select(selector):
        if node.tag != 'img':
            nodes = node.select('img')
        else:
            nodes = [node]
        for image in nodes:
            src = next((image.attrs.get(name) for name in
                        ('data-src', 'data-original', 'data-lazy-src', 'data-url', 'src')
                        if image.attrs.get(name)), None)
            if src:
                try:
                    url = canonicalize_url(urllib.parse.urljoin(base, src))
                except ValueError:
                    continue
                if url not in seen:
                    seen.add(url)
                    images.append(WebcomicAsset(url, len(images) + 1,
                        provenance={'source_url': base, 'selector': selector}))
    return images


class Adapter:
    id, version = 'base', '1'

    def probe(self, url, response=None):
        return 0

    def capabilities(self):
        return {'archive': True, 'incremental': 'conditional_index', 'chapters': 'evidence',
                'assets': True, 'browser': False, 'authenticated': False}

    def canonicalize_url(self, url):
        return canonicalize_url(url)

    def get_updates_since(self, url, client, cursor=None, config=None):
        return self.resolve_series(url, client, cursor or {}, config or {})

    def enumerate_entries(self, snapshot):
        yield from snapshot.entries

    def enumerate_chapters(self, snapshot):
        yield from snapshot.chapters

    def get_series_metadata(self, snapshot):
        return snapshot.series

    def get_entry_metadata(self, entry):
        return entry

    def get_latest_entry(self, snapshot):
        return max(snapshot.entries, key=lambda entry: entry.ordinal or 0, default=None)

    def health_check(self, snapshot):
        return {'entry_count': len(snapshot.entries), 'warnings': snapshot.warnings,
                'metrics': snapshot.metrics}


class FeedAdapter(Adapter):
    id = 'feed'

    def probe(self, url, response=None):
        if response:
            prefix = response.body[:512].lstrip().lower()
            if b'<rss' in prefix or b'<feed' in prefix or b'"version"' in prefix or b'"@type"' in prefix and b'comicseries' in prefix:
                return 90
        return 0

    def resolve_series(self, url, client, cursor=None, config=None):
        response = client.get(url)
        manifest = parse_feed(response.body, response.url, 'webcomic', 500)
        series = new_series(response.url, manifest['title'], self.id)
        merge_metadata(series, [MetadataCandidate('title', manifest['title'], 'feed', response.url),
                               MetadataCandidate('creators', [{'name': manifest['author'], 'role': 'author'}], 'feed', response.url)])
        entries = []
        for index, chapter in enumerate(manifest['chapters']):
            source_url = canonicalize_url(chapter['url'])
            native = chapter.get('provider_metadata') or {}
            key = str(native.get('entry_id') or source_url)
            assets = [WebcomicAsset(canonicalize_url(image), number + 1,
                                   provenance={'provider': 'feed', 'source_url': response.url})
                      for number, image in enumerate(chapter.get('images') or [])]
            entries.append(WebcomicEntry(key, source_url, chapter.get('title') or 'Untitled',
                                        ordinal=index + 1, published_at=chapter.get('published'),
                                        assets=assets, provenance={'provider': 'feed', 'feed_url': response.url,
                                                                  'native': native}))
        # Publication dates improve feed order only when every timestamp is valid;
        # stable source IDs, never timestamps alone, drive update discovery.
        if entries and all(entry.published_at for entry in entries):
            entries.sort(key=lambda entry: (entry.published_at, entry.source_id))
        for index, entry in enumerate(entries):
            entry.ordinal = index + 1
        return Snapshot(series, entries, group_entries(entries), complete=False,
                        unchanged=response.unchanged, cursor={'feed_url': response.url,
                        'etag': response.headers.get('etag'), 'last_modified': response.headers.get('last-modified')},
                        metrics={'entry_count': len(entries), 'http_status': response.status})

    def resolve_assets(self, entry, client, config=None):
        if entry.assets:
            return entry
        response = client.get(entry.source_url)
        parser = ChapterParser((config or {}).get('image_selector', '#comic,.comic,article,main'), response.url)
        parser.feed(response.body.decode('utf-8-sig', 'replace'))
        entry.assets = [WebcomicAsset(url, index + 1, provenance={'source_url': response.url,
                        'provider': self.id}) for index, url in enumerate(parser.images)]
        if not entry.assets:
            raise ExtractionError('Expected comic images were not found in feed entry')
        return entry


class HtmlAdapter(Adapter):
    id = 'static'

    def __init__(self, recipe=None):
        self.recipe = recipe or Recipe()
        self.id, self.version = self.recipe.id, self.recipe.version

    def probe(self, url, response=None):
        host = urllib.parse.urlsplit(url).hostname or ''
        if self.recipe.domains and any(host == domain or host.endswith('.' + domain) for domain in self.recipe.domains):
            return 75
        if response and Document(response.body).select(self.recipe.archive_selector):
            return 40
        return 0

    def resolve_series(self, url, client, cursor=None, config=None):
        recipe = Recipe.from_dict({**self.recipe.__dict__, **(config or {}).get('recipe', {})})
        response = client.get(recipe.archive_url or url)
        document = Document(response.body)
        titles = document.select(recipe.title_selector)
        title = next((node.text() for node in titles if node.text()), 'Untitled webcomic')
        series = new_series(url, title, self.id)
        merge_metadata(series, METADATA.extract(document, response.url))
        entries, declared, seen, pages = [], {}, set(), set()
        unchanged = response.unchanged
        complete = recipe.complete
        for page in range(recipe.max_pages):
            if response.url in pages:
                raise ExtractionError('Archive pagination loop')
            pages.add(response.url)
            links = document.select(recipe.archive_selector)
            for node in links:
                href = node.attrs.get('href')
                if not href:
                    continue
                try:
                    target = canonicalize_url(urllib.parse.urljoin(response.url, href))
                except ValueError:
                    continue
                if target in seen:
                    continue
                # Archive traversal is same-origin; image CDNs remain permitted.
                if urllib.parse.urlsplit(target).netloc != urllib.parse.urlsplit(response.url).netloc:
                    continue
                seen.add(target)
                key = str(node.attrs.get('data-entry-id') or target)
                label = node.attrs.get('data-chapter')
                parent = node.parent
                while not label and parent:
                    label = parent.attrs.get('data-chapter')
                    if not label:
                        headings = parent.select(recipe.chapter_selector)
                        if len(headings) == 1:
                            label = headings[0].text()
                    parent = parent.parent
                if label:
                    declared[key] = label
                entries.append(WebcomicEntry(key, target, node.text() or node.attrs.get('title') or target,
                    published_at=node.attrs.get('data-published'),
                    provenance={'provider': self.id, 'archive_url': response.url,
                                'selector': recipe.archive_selector}))
                if len(entries) > 10000:
                    raise ExtractionError('Archive exceeds entry budget; use paged API adapter')
            next_nodes = document.select(recipe.next_selector) if recipe.next_selector else []
            next_url = next((node.attrs.get('href') for node in next_nodes if node.attrs.get('href')), None)
            if not next_url:
                break
            target = canonicalize_url(urllib.parse.urljoin(response.url, next_url))
            if urllib.parse.urlsplit(target).netloc != urllib.parse.urlsplit(response.url).netloc:
                raise ExtractionError('Archive pagination changed origin')
            if page + 1 == recipe.max_pages:
                complete = False
                break
            response = client.get(target)
            unchanged = unchanged and response.unchanged
            document = Document(response.body)
        if not entries:
            raise ExtractionError('Expected archive selector returned zero entries')
        if recipe.order == 'reverse':
            entries.reverse()
        elif recipe.order == 'natural':
            entries.sort(key=lambda entry: natural_key(entry.title))
        for index, entry in enumerate(entries):
            entry.ordinal = index + 1
        return Snapshot(series, entries, group_entries(entries, declared), complete=complete,
                        unchanged=unchanged, metrics={'entry_count': len(entries),
                        'selector_success': True, 'archive_pages': len(pages), 'http_status': response.status},
                        cursor={'etag': response.headers.get('etag'), 'last_modified': response.headers.get('last-modified')})

    def resolve_assets(self, entry, client, config=None):
        recipe = Recipe.from_dict({**self.recipe.__dict__, **(config or {}).get('recipe', {})})
        response = client.get(entry.source_url)
        document = Document(response.body)
        entry.assets = image_assets(document, recipe.image_selector, response.url)
        if not entry.assets:
            raise ExtractionError('Expected image selector returned zero assets')
        dates = document.select(recipe.date_selector)
        if dates and not entry.published_at:
            entry.published_at = dates[0].attrs.get('datetime') or dates[0].text()
        return entry


class XkcdAdapter(Adapter):
    id = 'xkcd'

    def probe(self, url, response=None):
        return 100 if urllib.parse.urlsplit(url).hostname in {'xkcd.com', 'www.xkcd.com'} else 0

    def canonicalize_url(self, url):
        return 'https://xkcd.com/'

    def resolve_series(self, url, client, cursor=None, config=None):
        response = client.get('https://xkcd.com/info.0.json')
        data = json.loads(response.body)
        latest = int(data['num'])
        if not 1 <= latest <= 100000:
            raise ExtractionError('Unexpected xkcd archive size')
        series = new_series('https://xkcd.com/', 'xkcd', self.id)
        merge_metadata(series, [MetadataCandidate('title', 'xkcd', self.id, 'https://xkcd.com/json.html'),
            MetadataCandidate('creators', [{'name': 'Randall Munroe', 'role': 'author'}], self.id, 'https://xkcd.com/')])
        start = int((cursor or {}).get('latest_number', 0)) + 1
        entries = [WebcomicEntry(str(number), f'https://xkcd.com/{number}/',
                                 f'xkcd {number}', ordinal=number, number=str(number),
                                 provenance={'provider': self.id, 'source_url': 'https://xkcd.com/json.html'})
                   for number in range(start, latest + 1) if number != 404]
        if entries:
            entries[-1] = self._entry(data)
        return Snapshot(series, entries, unchanged=response.unchanged,
                        cursor={'latest_number': latest, 'latest_source_entry_id': str(latest),
                                'etag': response.headers.get('etag')},
                        metrics={'entry_count': len(entries), 'http_status': response.status, 'incremental': True})

    def _entry(self, data):
        number = int(data['num'])
        url = f'https://xkcd.com/{number}/'
        return WebcomicEntry(str(number), url, data['title'], ordinal=number, number=str(number),
            published_at=dt.date(int(data['year']), int(data['month']), int(data['day'])).isoformat(),
            assets=[WebcomicAsset(canonicalize_url(data['img']), 1, provenance={'source_url': url})],
            transcript=data.get('transcript'), notes=data.get('alt'),
            provenance={'provider': self.id, 'source_url': url + 'info.0.json'})

    def resolve_assets(self, entry, client, config=None):
        response = client.get(entry.source_url + 'info.0.json')
        result = self._entry(json.loads(response.body))
        if result.source_id != entry.source_id:
            raise ExtractionError('xkcd response returned a different entry')
        return result


class WebtoonAdapter(HtmlAdapter):
    """Public WEBTOON list/viewer pages only. Paid/app-only entries fail visibly."""
    def __init__(self):
        super().__init__(Recipe(id='webtoon', domains=['webtoons.com'],
            title_selector='h1,.subj', archive_selector='#_listUl a',
            image_selector='#_imageList img', order='reverse',
            next_selector='.paginate a.next', max_pages=100))

    def canonicalize_url(self, url):
        parts = urllib.parse.urlsplit(canonicalize_url(url))
        params = urllib.parse.parse_qs(parts.query)
        title = params.get('title_no', [''])[0]
        if not title.isdigit():
            raise ValueError('WEBTOON URL requires title_no')
        path = re.sub(r'/(viewer|list)/?$', '/list', parts.path)
        return urllib.parse.urlunsplit(('https', 'www.webtoons.com', path,
                                       urllib.parse.urlencode({'title_no': title}), ''))

    def resolve_series(self, url, client, cursor=None, config=None):
        # Initial bounded archive discovery; subsequent polls inspect the newest
        # list page. No absent entry can be removed from this partial snapshot.
        settings = dict(config or {})
        recipe = {**self.recipe.__dict__, **settings.get('recipe', {})}
        if cursor and cursor.get('latest_source_entry_id'):
            recipe.update({'max_pages': 1, 'complete': False})
        settings['recipe'] = recipe
        snapshot = super().resolve_series(self.canonicalize_url(url), client, cursor, settings)
        for entry in snapshot.entries:
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(entry.source_url).query)
            episode = params.get('episode_no', [''])[0]
            if not episode.isdigit():
                raise ExtractionError('WEBTOON entry is missing episode_no')
            entry.source_id, entry.number, entry.ordinal = episode, episode, int(episode)
            entry.provenance['layout'] = 'vertical_scroll'
        snapshot.entries.sort(key=lambda entry: entry.ordinal)
        snapshot.chapters = group_entries(snapshot.entries)
        snapshot.complete = False
        return snapshot


class AdapterRegistry:
    def __init__(self):
        self.adapters = {}

    def register(self, adapter):
        if adapter.id in self.adapters:
            raise ValueError('Duplicate adapter ID: ' + adapter.id)
        self.adapters[adapter.id] = adapter

    def select(self, url, response=None, adapter_id=None):
        if adapter_id:
            if adapter_id not in self.adapters:
                raise ValueError('Unknown webcomic adapter: ' + adapter_id)
            return self.adapters[adapter_id]
        ranked = sorted(((adapter.probe(url, response), adapter.id, adapter)
                         for adapter in self.adapters.values()), key=lambda item: (-item[0], item[1]))
        if not ranked or ranked[0][0] == 0:
            raise ExtractionError('No validated comic layout found. Supply a feed URL or extraction recipe.')
        return ranked[0][2]


def default_registry():
    registry = AdapterRegistry()
    for adapter in (XkcdAdapter(), FeedAdapter(), HtmlAdapter(), WebtoonAdapter(),
                    HtmlAdapter(Recipe(id='wordpress', archive_selector='.comic-archive a,.webcomic-archive a,#archive a',
                                       image_selector='.webcomic-image img,#comic img,.comic img')),
                    HtmlAdapter(Recipe(id='comicfury', domains=['comicfury.com', 'thecomicseries.com', 'thecomicstrip.org', 'thecomicseries.org', 'comicfury.net'],
                                       archive_selector='#archive a,.archive a,.comic-archive a',
                                       image_selector='#comicimage,img.comicimage,#comic img'))):
        registry.register(adapter)
    return registry
