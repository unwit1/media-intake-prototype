"""Independently testable extractors, recipes and bounded source enumeration."""
import datetime as dt
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
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


ATOM_NS = '{http://www.w3.org/2005/Atom}'
HISTORY_NS = '{http://purl.org/syndication/history/1.0}'
ARCHIVE_PAGES_PER_CHECK = 10
MAX_ARCHIVE_ENTRIES = 10_000


def feed_links(body):
    """Older-page links of a feed (RFC 5005 paging/archives, JSON Feed next_url).

    Returns ({'next': url?, 'prev-archive': url?}, complete) without network I/O.
    """
    try:
        data = json.loads(body.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        return ({'next': data['next_url']} if isinstance(data.get('next_url'), str) else {}), False
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}, False
    container = root.find('channel') if root.find('channel') is not None else root
    links = {}
    for link in container.findall(f'{ATOM_NS}link') + container.findall('link'):
        rel, href = link.get('rel'), link.get('href')
        if rel in ('next', 'prev-archive') and href and rel not in links:
            links[rel] = href
    complete = container.find(f'{HISTORY_NS}complete') is not None
    return links, complete


class FeedAdapter(Adapter):
    id = 'feed'

    def probe(self, url, response=None):
        if response:
            prefix = response.body[:512].lstrip().lower()
            if b'<rss' in prefix or b'<feed' in prefix or b'"version"' in prefix or b'"@type"' in prefix and b'comicseries' in prefix:
                return 90
        return 0

    def _page_entries(self, body, page_url, backfill=False):
        manifest = parse_feed(body, page_url, 'webcomic', 500)
        entries = []
        for chapter in manifest['chapters']:
            source_url = canonicalize_url(chapter['url'])
            native = chapter.get('provider_metadata') or {}
            key = str(native.get('entry_id') or source_url)
            assets = [WebcomicAsset(canonicalize_url(image), number + 1,
                                   provenance={'provider': 'feed', 'source_url': page_url})
                      for number, image in enumerate(chapter.get('images') or [])]
            provenance = {'provider': 'feed', 'feed_url': page_url, 'native': native}
            if backfill:
                provenance['backfill'] = True
            entries.append(WebcomicEntry(key, source_url, chapter.get('title') or 'Untitled',
                                        published_at=chapter.get('published'), assets=assets,
                                        provenance=provenance))
        # Publication dates improve feed order only when every timestamp is valid;
        # stable source IDs, never timestamps alone, drive update discovery.
        if entries and all(entry.published_at for entry in entries):
            entries.sort(key=lambda entry: (entry.published_at, entry.source_id))
        return manifest, entries

    def _walk_archive(self, client, head_url, start_url, archive, config, seen_keys):
        """Follow older pages from `start_url`, within this check's page budget."""
        budget = int(config.get('archive_pages_per_check', ARCHIVE_PAGES_PER_CHECK))
        host = urllib.parse.urlsplit(head_url).hostname
        visited = list(archive.get('visited') or [])
        pages, next_url, warnings = [], start_url, []
        while next_url and len(pages) < budget:
            try:
                next_url = canonicalize_url(urllib.parse.urljoin(head_url, next_url))
            except ValueError:
                warnings.append('Ignored an invalid archive link')
                next_url = None
                break
            if urllib.parse.urlsplit(next_url).hostname != host:
                warnings.append(f'Ignored an archive link to another site: {next_url}')
                next_url = None
                break
            if next_url in visited or next_url == head_url:
                warnings.append('Feed archive links loop; stopped following them')
                next_url = None
                break
            visited.append(next_url)
            response = client.get(next_url, conditional=False)
            _manifest, entries = self._page_entries(response.body, response.url, backfill=True)
            fresh = [entry for entry in entries if entry.source_id not in seen_keys]
            seen_keys.update(entry.source_id for entry in entries)
            pages.append(fresh)
            links, _complete = feed_links(response.body)
            next_url = links.get('next') or links.get('prev-archive')
            if sum(map(len, pages)) + int(archive.get('entries', 0)) >= MAX_ARCHIVE_ENTRIES:
                warnings.append(f'Stopped at the {MAX_ARCHIVE_ENTRIES:,}-entry archive limit')
                next_url = None
        archive.update({'next_url': next_url, 'exhausted': not next_url, 'visited': visited[-500:],
                        'pages': int(archive.get('pages', 0)) + len(pages),
                        'entries': int(archive.get('entries', 0)) + sum(map(len, pages))})
        return pages, warnings

    def resolve_series(self, url, client, cursor=None, config=None):
        cursor, config = dict(cursor or {}), dict(config or {})
        response = client.get(url)
        manifest, head_entries = self._page_entries(response.body, response.url)
        series = new_series(response.url, manifest['title'], self.id)
        merge_metadata(series, [MetadataCandidate('title', manifest['title'], 'feed', response.url),
                               MetadataCandidate('creators', [{'name': manifest['author'], 'role': 'author'}], 'feed', response.url)])
        archive = dict(cursor.get('feed_archive') or {})
        links, complete = feed_links(response.body)
        if not archive:
            archive = {'next_url': links.get('next') or links.get('prev-archive'), 'pages': 0, 'entries': 0}
        warnings = []
        pages = []
        if config.get('follow_feed_archive', True) and archive.get('next_url') and not archive.get('exhausted'):
            # A 304 on the head page never stops an unfinished backlog.
            seen_keys = {entry.source_id for entry in head_entries}
            pages, warnings = self._walk_archive(client, response.url, archive['next_url'], archive, config, seen_keys)
        # Oldest page first, then the head: chronological order for first-time ordinals.
        entries = [entry for page in reversed(pages) for entry in page] + head_entries
        for index, entry in enumerate(entries):
            entry.ordinal = index + 1
        backfilled = sum(map(len, pages))
        return Snapshot(series, entries, group_entries(entries), complete=False,
                        unchanged=response.unchanged and not backfilled, warnings=warnings,
                        cursor={'feed_url': response.url, 'etag': response.headers.get('etag'),
                                'last_modified': response.headers.get('last-modified'), 'feed_archive': archive},
                        metrics={'entry_count': len(entries), 'http_status': response.status,
                                 'archive_pages': archive.get('pages', 0), 'archive_entries': archive.get('entries', 0),
                                 'archive_complete': bool(archive.get('exhausted')) or complete,
                                 'backfilled_this_check': backfilled})

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
            next_selector='.paginate a.next,.paginate a.pg_next', max_pages=100))

    def canonicalize_url(self, url):
        parts = urllib.parse.urlsplit(canonicalize_url(url))
        params = urllib.parse.parse_qs(parts.query)
        title = params.get('title_no', [''])[0]
        if not title.isdigit():
            raise ValueError('WEBTOON URL requires title_no')
        path = re.sub(r'/(viewer|list)/?$', '/list', parts.path)
        return urllib.parse.urlunsplit(('https', 'www.webtoons.com', path,
                                       urllib.parse.urlencode({'title_no': title}), ''))

    LOCK_MARKERS = re.compile(r'lock|daily_?pass|dailypass|paid|coin|fast_?pass', re.I)
    INITIAL_PAGES, CATCHUP_PAGES = 100, 20

    def _page_url(self, list_url, page):
        parts = urllib.parse.urlsplit(list_url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if page > 1:
            query['page'] = str(page)
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))

    @staticmethod
    def _date(text):
        for pattern in ('%b %d, %Y', '%B %d, %Y', '%Y.%m.%d', '%d %b %Y'):
            try:
                return dt.datetime.strptime(text.strip(), pattern).date().isoformat()
            except ValueError:
                continue
        return None

    def _parse_list(self, body, base):
        document = Document(body)
        items, has_next = [], bool(document.select('.paginate a.next') or document.select('.paginate a.pg_next'))
        for item in document.select('#_listUl li') or []:
            links = item.select('a')
            if not links or not links[0].attrs.get('href'):
                continue
            target = canonicalize_url(urllib.parse.urljoin(base, links[0].attrs['href']))
            episode = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query).get('episode_no', [''])[0]
            if not episode.isdigit():
                raise ExtractionError('WEBTOON entry is missing episode_no')
            classes = ' '.join([item.attrs.get('class', '')] + [node.attrs.get('class', '') for node in item.select('*')])
            subject = item.select('.subj')
            dates = item.select('.date')
            items.append({'episode': int(episode), 'url': target,
                          'title': (subject[0].text() if subject else links[0].text()) or f'Episode {episode}',
                          'date': self._date(dates[0].text()) if dates else None,
                          'locked': bool(self.LOCK_MARKERS.search(classes))})
        if not items:  # older markup: plain links in the list
            for node in document.select('#_listUl a'):
                href = node.attrs.get('href')
                if href:
                    target = canonicalize_url(urllib.parse.urljoin(base, href))
                    episode = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query).get('episode_no', [''])[0]
                    if not episode.isdigit():
                        raise ExtractionError('WEBTOON entry is missing episode_no')
                    items.append({'episode': int(episode), 'url': target, 'title': node.text() or f'Episode {episode}',
                                  'date': None, 'locked': False})
        return document, items, has_next

    def resolve_series(self, url, client, cursor=None, config=None):
        """Initial checks read the whole list; later checks page back until they reach a known episode."""
        cursor, config = dict(cursor or {}), dict(config or {})
        list_url = self.canonicalize_url(url)
        known = int(cursor.get('latest_source_entry_id') or 0) if str(cursor.get('latest_source_entry_id') or '').isdigit() else 0
        limit = int(config.get('webtoon_catchup_pages', self.CATCHUP_PAGES) if known else
                    config.get('webtoon_pages', self.INITIAL_PAGES))
        found, warnings, first_document, overlap, pages = {}, [], None, False, 0
        for page in range(1, limit + 1):
            response = client.get(self._page_url(list_url, page))
            document, items, has_next = self._parse_list(response.body, response.url)
            pages += 1
            first_document = first_document or document
            for item in items:
                found.setdefault(item['episode'], item)
            if known and any(item['episode'] <= known for item in items):
                overlap = True
                break
            if not items or not has_next:
                break
        else:
            if known and not overlap:
                warnings.append(f'More than {limit} list pages of new episodes since the last check; '
                                'some episodes may be missing until the next check')
        if known and not overlap and pages < limit and found and min(found) > known + 1:
            warnings.append('The episode list did not reach the last known episode; possible gap')
        titles = first_document.select('h1') + first_document.select('.subj') if first_document else []
        title = next((node.text() for node in titles if node.text()), 'WEBTOON series')
        series = new_series(list_url, title, self.id)
        candidates = [MetadataCandidate('title', title, self.id, list_url)]
        authors = [node.text() for node in (first_document.select('.author_area') + first_document.select('.author'))
                   if node.text()] if first_document else []
        if authors:
            names = [name.strip() for name in re.split(r'\s*[,/]\s*', re.sub(r'(author info|info)$', '', authors[0], flags=re.I))
                     if name.strip()]
            candidates.append(MetadataCandidate('creators', [{'name': name, 'role': 'creator'} for name in names],
                                                self.id, list_url, 0.9))
        merge_metadata(series, candidates)
        entries = []
        for episode in sorted(found):
            item = found[episode]
            entry = WebcomicEntry(str(episode), item['url'], item['title'], ordinal=episode, number=str(episode),
                                  published_at=item['date'],
                                  provenance={'provider': self.id, 'layout': 'vertical_scroll', 'list_url': list_url,
                                              **({'locked': True} if item['locked'] else {})})
            entries.append(entry)
        chapters = group_entries(entries)
        for entry in entries:
            if entry.provenance.get('locked'):
                entry.content_type = 'locked'  # paid/Daily Pass: recorded, never downloaded
        return Snapshot(series, entries, chapters, complete=False, warnings=warnings,
                        cursor={'list_url': list_url},
                        metrics={'entry_count': len(entries), 'pages': pages,
                                 'locked': sum(bool(e.provenance.get('locked')) for e in entries),
                                 'incremental': bool(known)})

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
