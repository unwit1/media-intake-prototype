"""Evidence-backed series supplements missing from the large book catalogs."""
import re
import unicodedata


_STORMLIGHT_KEY = 'stormlight archive'
_STORMLIGHT_PLAN = {
    'title': 'The Stormlight Archive',
    'planned_mainline_novel_count': 10,
    'next_mainline_number': 6,
    'next_mainline_title': None,
    'next_mainline_release_date': None,
    'release_status': 'planned; title and date unannounced',
    'evidence': [{
        'source': 'Brandon Sanderson, State of the Sanderson 2025',
        'url': 'https://www.brandonsanderson.com/blogs/blog/state-of-the-sanderson-2025',
        'claim': 'Horneater and possible early work on Book Six are likely in the next few years; no firm date is given.',
    }],
}

_STORMLIGHT_MEMBERS = [
    {
        'title': 'Elsecaller / King Lopen the First of Alethkar',
        'author': 'Brandon Sanderson, Dan Wells, and Isaac Stewart',
        'kind': 'book',
        'provider': 'Curated continuity',
        'source_url': 'https://www.dragonsteelbooks.com/products/elsecaller-king-lopen-the-first-of-alethkar-ebook',
        'series': 'The Stormlight Archive',
        'series_index': 2.1,
        'series_position': '2.1',
        'series_position_source': 'Goodreads series listing',
        'publication_status': 'released',
        'continuity_status': 'canonical short fiction; published together as one double book',
        'catalog_metadata': {
            'catalog_provider': 'Dragonsteel',
            'catalog_id': '9781938570650',
            'evidence': [
                {
                    'source': 'Brandon Sanderson, State of the Sanderson 2025',
                    'url': 'https://www.brandonsanderson.com/blogs/blog/state-of-the-sanderson-2025',
                    'claim': 'Dragonsteel reports releasing the double book in 2025.',
                },
                {
                    'source': 'Goodreads, The Stormlight Archive series',
                    'url': 'https://www.goodreads.com/series/49075-the-stormlight-archive',
                    'claim': 'Lists the double book at series position 2.1.',
                },
                {
                    'source': 'Dragonsteel Books product record',
                    'url': 'https://www.dragonsteelbooks.com/products/elsecaller-king-lopen-the-first-of-alethkar-ebook',
                    'claim': 'Official ebook page identifies both stories as Stormlight tales.',
                },
            ],
        },
    },
    {
        'title': 'Horneater',
        'author': 'Brandon Sanderson',
        'kind': 'book',
        'provider': 'Curated continuity',
        'source_url': 'https://www.goodreads.com/book/show/222842956-horneater',
        'series': 'The Stormlight Archive',
        'series_index': 4.5,
        'series_position': '4.5',
        'series_position_source': 'Goodreads series listing; author confirms it is set during Book Four',
        'publication_status': 'announced; release date unconfirmed',
        'search_after_release_date': True,
        'release_date_policy': 'official_or_manual_only',
        'continuity_status': 'planned Rock novella; chronologically during Rhythm of War',
        'catalog_metadata': {
            'catalog_provider': 'Brandon Sanderson / Dragonsteel',
            'catalog_id': 'goodreads:222842956',
            'evidence': [
                {
                    'source': 'Brandon Sanderson, State of the Sanderson 2024',
                    'url': 'https://www.brandonsanderson.com/blogs/blog/state-of-the-sanderson-2024',
                    'claim': 'Describes Horneater as a Rock novella taking place during Book Four.',
                },
                {
                    'source': 'Brandon Sanderson, State of the Sanderson 2025',
                    'url': 'https://www.brandonsanderson.com/blogs/blog/state-of-the-sanderson-2025',
                    'claim': 'Says Horneater is likely in the next few years, without a firm release date.',
                },
                {
                    'source': 'Goodreads, The Stormlight Archive series',
                    'url': 'https://www.goodreads.com/series/49075-the-stormlight-archive',
                    'claim': 'Lists Horneater at 4.5, but its expected-2026 date conflicts with the newer official unscheduled update and is not used for scheduling.',
                },
            ],
        },
    },
]

_SERIES = {_STORMLIGHT_KEY: {'plan': _STORMLIGHT_PLAN,
                             'members': _STORMLIGHT_MEMBERS}}
_PACKAGING_SUFFIX = re.compile(
    r'^(?:part\s+(?:one|two|three|four|\d+)|volume\s+\d+|'
    r'(?:complete|collected)\s+(?:series|edition)|series\s+collection|'
    r'omnibus|box(?:ed)?\s+set|collection)\b')
_SPLIT_ENDING = re.compile(
    r'\b(?:part\s+(?:one|two|three|four|\d+)|volume\s+'
    r'(?:one|two|three|four|\d+)(?:\s+of\s+(?:one|two|three|four|\d+))?|'
    r'part\s+\d+\s+of\s+\d+)\s*$')
_EDITION_LABEL = re.compile(
    r'\b(?:omnibus|box(?:ed)?\s+set|complete\s+series|collected\s+edition|'
    r'(?:anniversary|leatherbound|illustrated|deluxe|revised|expanded)\s+edition)\b')
_FORMAT_EDITION_SUFFIX = re.compile(
    r'(?:\(|\[|:\s*|\s+-\s*|\s+)'
    r'(?:mass\s+market\s+|trade\s+)?'
    r'(?:paperback|hardback|hardcover|kindle|e-?book|audiobook|audio\s+book|large\s+print)'
    r'(?:\s+edition)?\s*[)\]]?\s*$')


def _key(value):
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    words = re.findall(r'[a-z0-9]+', text)
    if words and words[0] in {'a', 'an', 'the'}:
        words = words[1:]
    return ' '.join(words)


def enrich_series_members(series_title, members):
    """Merge verified continuity supplements and return its forward plan."""
    key = _key(series_title)
    record = _SERIES.get(key)
    if not record:
        return members, None

    merged = list(members or [])
    for supplement in record['members']:
        target = _key(supplement.get('title'))
        existing = next((item for item in merged
                         if _key(item.get('title')) == target), None)
        if existing is None:
            merged.append({**supplement, 'catalog_metadata': dict(supplement['catalog_metadata'])})
            continue

        # This registry is used specifically when global catalogs omit a work or
        # have a clearly weaker status/date claim. Keep their IDs as alternates,
        # but let the author/publisher evidence control continuity and scheduling.
        existing['series'] = supplement['series']
        existing['series_index'] = supplement['series_index']
        existing['series_position'] = supplement['series_position']
        existing['series_position_source'] = supplement['series_position_source']
        existing['provider'] = supplement['provider']
        existing['publication_status'] = supplement['publication_status']
        existing['continuity_status'] = supplement['continuity_status']
        if supplement.get('search_after_release_date'):
            existing['search_after_release_date'] = True
            existing['release_date_policy'] = supplement['release_date_policy']
            # Goodreads' current day-level estimate is not in the author's latest
            # schedule. Do not turn it into a search date.
            existing['release_date'] = None
        existing['source_url'] = existing.get('source_url') or supplement['source_url']
        catalog = dict(existing.get('catalog_metadata') or {})
        catalog.update(supplement['catalog_metadata'])
        existing['catalog_metadata'] = catalog

    return merged, dict(record['plan'])


def is_unscheduled_announced_member(metadata):
    """Whether a book watch is announced but must wait for a release date."""
    return bool((metadata or {}).get('search_after_release_date') and
                (metadata or {}).get('publication_status') ==
                'announced; release date unconfirmed')


def series_member_review_reason(series_title, member):
    """Flag catalog rows that look like packaging instead of a logical work.

    Suspicious rows go to review so split editions or unusual canonical titles
    remain recoverable without becoming duplicate story watches automatically.
    """
    member = member or {}
    entity_type = str(member.get('entity_type') or
                      (member.get('catalog_metadata') or {}).get('entity_type') or '').casefold()
    if entity_type in {'edition', 'collection', 'container', 'bundle'}:
        return f"Catalog classifies '{member.get('title') or 'this entry'}' as {entity_type}."
    title_key, series_key = _key(member.get('title')), _key(series_title)
    if not title_key or not series_key:
        return None
    if title_key.startswith(series_key):
        suffix = title_key[len(series_key):].strip()
        if _PACKAGING_SUFFIX.match(suffix):
            return f"Title suffix '{suffix}' looks like a split edition or collection label."
    if _SPLIT_ENDING.search(title_key):
        return 'Title ends with a part or volume label and may be a split release.'
    if _EDITION_LABEL.search(title_key):
        return 'Title includes a collected, boxed, or special-edition label.'
    if _FORMAT_EDITION_SUFFIX.search(title_key):
        return 'Title ends with a common book-format label and may be an edition duplicate.'
    return None
