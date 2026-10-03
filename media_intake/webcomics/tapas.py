"""Public Tapas series: bounded episode-list paging, image lists, and locked episodes left alone.

- The series ID comes from the series page (``.subscribe-btn`` ``data-id``, or a
  ``data-series-id`` attribute).
- Episodes come from ``/series/<id>/episodes?page=N&sort=OLDEST&max_limit=20``,
  whose JSON ``data.body`` holds the list HTML (``data-permalink="/episode/<id>"``)
  and ``data.pagination.has_next``.
- Images are the episode page's ``.content__img`` ``data-src`` values.
- Locked, paid or age-gated episodes are recorded as inaccessible and never
  downloaded; no cookies, sign-in or age-gate workarounds are used.

Tested against recorded markup; the live site is not verified by the test suite.
"""
import json
import re
import urllib.parse

from .adapters import Adapter, MetadataCandidate, merge_metadata, new_series
from .extraction import Document
from .http import ExtractionError
from .models import Snapshot, WebcomicAsset, WebcomicEntry, canonicalize_url

PER_PAGE = 20
PAGES_PER_CHECK = 50
LOCKED = re.compile(r'locked|lock|paid|coin|wait-?until|mature|age', re.I)


class TapasAdapter(Adapter):
    id, version = 'tapas', '1'

    def probe(self, url, response=None):
        parts = urllib.parse.urlsplit(url)
        return 95 if (parts.hostname or '').casefold().removeprefix('www.') == 'tapas.io' and \
            parts.path.startswith('/series/') else 0

    def canonicalize_url(self, url):
        parts = urllib.parse.urlsplit(canonicalize_url(url))
        slug = parts.path.split('/')[2] if len(parts.path.split('/')) > 2 else ''
        if not slug:
            raise ValueError('Tapas URL must point to a series')
        return f'https://tapas.io/series/{slug}/info'

    def _series_id(self, document, body):
        for node in document.select('.subscribe-btn') + document.select('*'):
            for attribute in ('data-series-id', 'data-id'):
                value = node.attrs.get(attribute)
                if value and value.isdigit() and (attribute == 'data-series-id' or 'subscribe-btn' in node.attrs.get('class', '')):
                    return value
        match = re.search(rb'seriesId["\']?\s*[:=]\s*["\']?(\d+)', body)
        if match:
            return match.group(1).decode()
        raise ExtractionError('Could not find the Tapas series ID')

    def resolve_series(self, url, client, cursor=None, config=None):
        cursor, config = dict(cursor or {}), dict(config or {})
        url = self.canonicalize_url(url)
        state = dict(cursor.get('tapas') or {})
        if not state.get('series_id'):
            page = client.get(url)
            document = Document(page.body)
            state['series_id'] = self._series_id(document, page.body)
            titles = document.select('.title') + document.select('h1') + document.select('title')
            state['title'] = next((node.text() for node in titles if node.text()), 'Tapas series')
            authors = document.select('.creator') + document.select('.name')
            state['creator'] = next((node.text() for node in authors if node.text()), None)
        entries, page, fetched, has_next = [], 1, 0, True
        budget = int(config.get('tapas_pages_per_check', PAGES_PER_CHECK))
        while has_next and fetched < budget:
            api = (f"https://tapas.io/series/{state['series_id']}/episodes?"
                   + urllib.parse.urlencode({'page': page, 'sort': 'OLDEST', 'max_limit': PER_PAGE}))
            data = json.loads(client.get(api, conditional=False).body.decode('utf-8')).get('data') or {}
            fetched += 1
            listing = Document(data.get('body') or '')
            for node in listing.select('*'):
                permalink = node.attrs.get('data-permalink') or ''
                match = re.search(r'/episode/(\d+)', permalink)
                if not match:
                    continue
                classes = ' '.join([node.attrs.get('class', '')] + [child.attrs.get('class', '') for child in node.select('*')])
                titles = node.select('.title') + node.select('.episode-title')
                locked = bool(LOCKED.search(classes)) or node.attrs.get('data-free') == 'false'
                entry = WebcomicEntry(match.group(1), f'https://tapas.io/episode/{match.group(1)}',
                                      (titles[0].text() if titles else node.text()) or f'Episode {match.group(1)}',
                                      provenance={'provider': self.id, 'series_id': state['series_id'],
                                                  **({'locked': True} if locked else {})})
                if locked:
                    entry.content_type = 'locked'
                entries.append(entry)
            has_next = bool((data.get('pagination') or {}).get('has_next'))
            page += 1
        warnings = [] if not has_next else [f'Stopped after {budget} list pages; the rest arrives on later checks']
        for index, entry in enumerate(entries, 1):
            entry.ordinal = index
        series = new_series(url, state['title'], self.id)
        candidates = [MetadataCandidate('title', state['title'], self.id, url)]
        if state.get('creator'):
            candidates.append(MetadataCandidate('creators', [{'name': state['creator'], 'role': 'creator'}], self.id, url))
        merge_metadata(series, candidates)
        return Snapshot(series, entries, [], complete=False, warnings=warnings, cursor={'tapas': state},
                        metrics={'entry_count': len(entries), 'pages': fetched,
                                 'locked': sum(1 for e in entries if e.content_type == 'locked')})

    def resolve_assets(self, entry, client, config=None):
        if entry.provenance.get('locked') or entry.content_type == 'locked':
            raise ExtractionError('This Tapas episode is locked; it is recorded but not downloaded')
        response = client.get(entry.source_url)  # no cookies: only what is public
        document = Document(response.body)
        sources = [node.attrs.get('data-src') or node.attrs.get('src') for node in document.select('img.content__img')]
        sources = [urllib.parse.urljoin(response.url, source) for source in sources if source]
        if not sources:
            raise ExtractionError('This Tapas episode is not publicly readable (locked or age-gated)')
        entry.assets = [WebcomicAsset(source, index + 1, provenance={'provider': self.id, 'source_url': response.url})
                        for index, source in enumerate(sources)]
        return entry
