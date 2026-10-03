"""Enrich webcomic series from external catalogs, durably and under review.

Catalog descriptors wrap the app's existing clients (Wikidata, AniList,
MangaUpdates, Comic Vine, Taddy). Searching stores *proposed* matches with a
score; nothing changes the series until a match is accepted. Accepted matches
and user locks are re-applied every time the source is refreshed, so
enrichment survives polling, and conflicting values from different catalogs
are all kept as field candidates with their provider and fetch time.

Credentials are passed in for each search and never stored with the series or
the matches.
"""
import datetime as dt
import json

from .metadata import MetadataCandidate, merge_metadata, normalize_title

CATALOG_CONFIDENCE = 0.8  # below on-page structured data (0.95), above Open Graph (0.6)
ENRICHABLE = ('title', 'alternate_titles', 'creators', 'description', 'cover_url', 'status', 'start_date', 'tags')


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS webcomic_metadata_matches (
        id INTEGER PRIMARY KEY, series_id TEXT NOT NULL, provider TEXT NOT NULL, external_id TEXT NOT NULL,
        candidate TEXT NOT NULL, score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'proposed',
        fetched_at TEXT NOT NULL, UNIQUE(series_id, provider, external_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS webcomic_metadata_locks (
        series_id TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL, locked_at TEXT NOT NULL,
        PRIMARY KEY(series_id, field))''')


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _creators(text):
    return [{'name': name.strip(), 'role': 'creator'} for name in str(text or '').split(',')
            if name.strip() and name.strip().casefold() != 'unknown']


def _anilist(query, credentials):
    from ..comics import search_anilist
    for row in search_anilist(query, limit=10):
        yield {'external_id': str(row.get('anilist_id')), 'title': row['title'], 'creators': _creators(row.get('author')),
               'description': row.get('description') or None, 'source_url': row.get('source_url'),
               'status': row.get('status'), 'start_date': row.get('release_date'), 'tags': row.get('genres') or []}


def _wikidata(query, credentials):
    from ..wikidata import search_wikidata
    for row in search_wikidata(query, limit=10):
        yield {'external_id': row['qid'], 'title': row['title'], 'description': row.get('description') or None,
               'source_url': row.get('source_url')}


def _mangaupdates(query, credentials):
    from ..media_catalogs import search_mangaupdates
    for row in search_mangaupdates(query, limit=8):
        yield {'external_id': str(row.get('catalog_id') or row.get('source_url')), 'title': row['title'],
               'creators': _creators(row.get('author')), 'source_url': row.get('source_url'),
               'description': row.get('description') or None}


def _comicvine(query, credentials):
    from ..comics import search_comicvine
    for row in search_comicvine(query, credentials['comicvine_api_key'], limit=10):
        yield {'external_id': str(row.get('catalog_id') or row.get('comicvine_id') or row.get('source_url')),
               'title': row['title'], 'creators': _creators(row.get('author')), 'source_url': row.get('source_url'),
               'description': row.get('description') or None, 'cover_url': row.get('cover_url') or row.get('image_url')}


def _taddy(query, credentials):
    from ..comics import search_taddy
    for row in search_taddy(query, credentials['taddy_user_id'], credentials['taddy_api_key'], limit=10):
        yield {'external_id': str(row.get('catalog_id') or row.get('taddy_uuid') or row.get('source_url')),
               'title': row['title'], 'creators': _creators(row.get('author')), 'source_url': row.get('source_url'),
               'description': row.get('description') or None, 'cover_url': row.get('cover_url') or row.get('image_url')}


CATALOGS = {
    'anilist': {'label': 'AniList', 'needs': (), 'search': _anilist},
    'wikidata': {'label': 'Wikidata', 'needs': (), 'search': _wikidata},
    'mangaupdates': {'label': 'MangaUpdates', 'needs': (), 'search': _mangaupdates},
    'comicvine': {'label': 'Comic Vine', 'needs': ('comicvine_api_key',), 'search': _comicvine},
    'taddy': {'label': 'Taddy', 'needs': ('taddy_user_id', 'taddy_api_key'), 'search': _taddy},
}


def available_catalogs(credentials=None):
    credentials = credentials or {}
    return [key for key, catalog in CATALOGS.items() if all(credentials.get(need) for need in catalog['needs'])]


def score(series_title, series_creators, candidate):
    """Exact normalized title (+ shared creator) scores high; anything else is review-only."""
    titles = {normalize_title(series_title)}
    candidate_titles = {normalize_title(candidate.get('title') or '')} | {
        normalize_title(value) for value in candidate.get('alternate_titles') or []}
    creators = {normalize_title(item['name']) for item in series_creators or [] if item.get('name')}
    shared_creator = bool(creators & {normalize_title(item['name']) for item in candidate.get('creators') or []})
    if titles & candidate_titles:
        return 0.95 if shared_creator else 0.75
    import difflib
    ratio = difflib.SequenceMatcher(None, normalize_title(series_title), normalize_title(candidate.get('title') or '')).ratio()
    return round(ratio * (0.6 if not shared_creator else 0.8), 3)


def find_matches(db, series_id, catalogs=None, credentials=None, query=None, search=None):
    """Search catalogs for a series and store proposed matches; returns them best first."""
    row = db.execute('SELECT metadata FROM webcomic_series WHERE id=?', (series_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown webcomic series')
    series = json.loads(row['metadata'])
    query = query or series.get('title') or ''
    credentials = credentials or {}
    errors = []
    for key in catalogs or available_catalogs(credentials):
        catalog = CATALOGS[key]
        try:
            found = list((search or {}).get(key, catalog['search'])(query, credentials))
        except Exception as exc:
            errors.append(f"{catalog['label']}: {exc}")
            continue
        for candidate in found:
            if not candidate.get('external_id') or not candidate.get('title'):
                continue
            db.execute('''INSERT INTO webcomic_metadata_matches(series_id, provider, external_id, candidate, score, fetched_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(series_id, provider, external_id) DO UPDATE SET
                candidate=excluded.candidate, score=excluded.score, fetched_at=excluded.fetched_at''',
                       (series_id, key, candidate['external_id'], json.dumps(candidate, ensure_ascii=False),
                        score(series.get('title') or '', series.get('creators'), candidate), _now()))
    db.commit()
    return matches(db, series_id), errors


def matches(db, series_id, status=None):
    sql = 'SELECT * FROM webcomic_metadata_matches WHERE series_id=?'
    params = [series_id]
    if status:
        sql += ' AND status=?'
        params.append(status)
    return [{**dict(row), 'candidate': json.loads(row['candidate'])}
            for row in db.execute(sql + ' ORDER BY score DESC, id', params)]


def set_match_status(db, match_id, status):
    if status not in ('proposed', 'accepted', 'rejected'):
        raise ValueError('Unknown match status')
    db.execute('UPDATE webcomic_metadata_matches SET status=? WHERE id=?', (status, match_id))
    db.commit()


def lock_field(db, series_id, field, value):
    if field not in ENRICHABLE:
        raise ValueError(f'{field} cannot be locked')
    db.execute('''INSERT INTO webcomic_metadata_locks VALUES(?,?,?,?) ON CONFLICT(series_id, field)
        DO UPDATE SET value=excluded.value, locked_at=excluded.locked_at''',
               (series_id, field, json.dumps(value, ensure_ascii=False), _now()))
    db.commit()


def unlock_field(db, series_id, field):
    db.execute('DELETE FROM webcomic_metadata_locks WHERE series_id=? AND field=?', (series_id, field))
    db.commit()


def apply_enrichment(db, series_id, series):
    """Merge accepted catalog matches and locks into a freshly extracted series."""
    locks = {row['field']: json.loads(row['value']) for row in
             db.execute('SELECT field, value FROM webcomic_metadata_locks WHERE series_id=?', (series_id,))}
    candidates = []
    for match in matches(db, series_id, 'accepted'):
        data, source = match['candidate'], match['candidate'].get('source_url') or match['provider']
        evidence = f"{CATALOGS.get(match['provider'], {}).get('label', match['provider'])} {match['external_id']}, fetched {match['fetched_at']}"
        for field in ENRICHABLE:
            if data.get(field):
                candidates.append(MetadataCandidate(field, data[field], match['provider'], source,
                                                    CATALOG_CONFIDENCE, evidence))
        series.external_ids = {**(series.external_ids or {}), match['provider']: match['external_id']}
    merge_metadata(series, candidates, locks=set(locks))
    for field, value in locks.items():
        setattr(series, field, value)
        series.metadata_provenance.setdefault(field, {'candidates': []})['selected'] = {
            'value': value, 'provider': 'user_lock', 'confidence': 1.0}
    return series


def refresh_series(db, series_id):
    """Apply accepted matches and locks to the stored series now, without waiting for a check."""
    from .models import WebcomicSeries, record
    row = db.execute('SELECT metadata FROM webcomic_series WHERE id=?', (series_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown webcomic series')
    data = json.loads(row['metadata'])
    fields = WebcomicSeries.__dataclass_fields__
    series = WebcomicSeries(**{key: value for key, value in data.items() if key in fields} |
                            {'canonical_id': data.get('canonical_id') or series_id,
                             'title': data.get('title') or 'Untitled webcomic',
                             'canonical_source_url': data.get('canonical_source_url') or '',
                             'platform': data.get('platform') or ''})
    apply_enrichment(db, series_id, series)
    updated = record(series)
    db.execute('UPDATE webcomic_series SET metadata=? WHERE id=?', (json.dumps(updated, ensure_ascii=False), series_id))
    db.execute("UPDATE watched_items SET title=? WHERE id IN (SELECT watch_id FROM webcomic_sources WHERE series_id=?)",
               (updated['title'], series_id))
    db.commit()
    return updated
