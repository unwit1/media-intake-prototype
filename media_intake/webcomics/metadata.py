"""Independent metadata providers; all candidates retain field-level evidence."""
from dataclasses import dataclass
import json
import re
import unicodedata
from .models import canonicalize_url


@dataclass
class MetadataCandidate:
    field: str
    value: object
    provider: str
    source_url: str
    confidence: float = 1.0
    evidence: str = ''


class MetadataRegistry:
    def __init__(self):
        self.providers = {}

    def register(self, provider):
        if provider.id in self.providers:
            raise ValueError('Duplicate metadata provider: ' + provider.id)
        self.providers[provider.id] = provider

    def extract(self, document, url):
        return [candidate for provider in self.providers.values()
                for candidate in provider.extract(document, url)]


class OpenGraphProvider:
    id = 'opengraph'

    def extract(self, document, url):
        mapping = {'og:site_name': 'title', 'og:description': 'description',
                   'og:image': 'cover_url', 'og:locale': 'language'}
        for node in document.select('meta'):
            name = node.attrs.get('property') or node.attrs.get('name')
            value = node.attrs.get('content')
            if name in mapping and value:
                yield MetadataCandidate(mapping[name], value, self.id, url, 0.6, name)


class SchemaOrgProvider:
    id = 'schemaorg'

    def extract(self, document, url):
        for node in document.select('script'):
            if node.attrs.get('type') != 'application/ld+json':
                continue
            try:
                payload = json.loads(''.join(node.chunks))
            except ValueError:
                continue
            stack = payload if isinstance(payload, list) else [payload]
            budget = 1000
            while stack and budget:
                budget -= 1
                value = stack.pop()
                if not isinstance(value, dict):
                    continue
                graph = value.get('@graph')
                if isinstance(graph, list):
                    stack.extend(graph)
                types = value.get('@type', [])
                types = [types] if isinstance(types, str) else types
                if not isinstance(types, list) or not set(types) & {'ComicSeries', 'CreativeWorkSeries', 'BookSeries'}:
                    continue
                for source, target in {'name': 'title', 'description': 'description',
                                       'inLanguage': 'language', 'license': 'license',
                                       'copyrightNotice': 'copyright', 'image': 'cover_url',
                                       'alternateName': 'alternate_titles'}.items():
                    item = value.get(source)
                    if isinstance(item, dict):
                        item = item.get('url')
                    if item:
                        if target == 'alternate_titles' and isinstance(item, str):
                            item = [item]
                        yield MetadataCandidate(target, item, self.id, url, 0.95, source)
                creators = []
                for role in ('author', 'artist', 'creator', 'illustrator'):
                    items = value.get(role, [])
                    items = items if isinstance(items, list) else [items]
                    for item in items:
                        name = item.get('name') if isinstance(item, dict) else item
                        if name:
                            creators.append({'name': name, 'role': role})
                if creators:
                    yield MetadataCandidate('creators', creators, self.id, url, 0.95, 'creator fields')


def merge_metadata(series, candidates, locks=None):
    locks = set(locks or ())
    for candidate in sorted(candidates, key=lambda value: value.confidence, reverse=True):
        if candidate.field not in series.__dataclass_fields__ or candidate.field == 'metadata_provenance':
            continue
        evidence = {'value': candidate.value, 'provider': candidate.provider,
                    'source_url': candidate.source_url, 'confidence': candidate.confidence,
                    'evidence': candidate.evidence}
        field = series.metadata_provenance.setdefault(candidate.field, {'candidates': []})
        if evidence not in field['candidates']:
            field['candidates'].append(evidence)
        selected = field.get('selected', {})
        if candidate.field not in locks and candidate.confidence > selected.get('confidence', -1):
            setattr(series, candidate.field, candidate.value)
            field['selected'] = evidence
    return series


def normalize_title(value):
    return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', value).casefold()).strip()


def match_entities(left, right):
    """Only exact source/ID matches auto-link; fuzzy evidence is review-only."""
    if canonicalize_url(left.canonical_source_url) == canonicalize_url(right.canonical_source_url):
        return {'confidence': 1.0, 'auto_merge': True, 'evidence': ['canonical_url']}
    ids = set(left.external_ids.items()) & set(right.external_ids.items())
    if ids:
        return {'confidence': 1.0, 'auto_merge': True, 'evidence': ['external_id']}
    titles = {normalize_title(left.title), *map(normalize_title, left.alternate_titles)}
    title_match = bool(titles & {normalize_title(right.title), *map(normalize_title, right.alternate_titles)})
    creators = {normalize_title(item['name']) for item in left.creators}
    creator_match = bool(creators & {normalize_title(item['name']) for item in right.creators})
    return {'confidence': 0.85 if title_match and creator_match else 0.4 if title_match else 0.0,
            'auto_merge': False, 'evidence': [name for name, matched in
                [('normalized_title', title_match), ('creator', creator_match)] if matched]}


REGISTRY = MetadataRegistry()
REGISTRY.register(SchemaOrgProvider())
REGISTRY.register(OpenGraphProvider())
