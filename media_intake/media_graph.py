"""Provider-neutral media entities, relationships, provenance, and graph history."""
import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timezone


ENTITY_TYPES = {
    'franchise', 'universe', 'continuity', 'series', 'season', 'arc', 'work',
    'edition', 'collection', 'release', 'person', 'creator',
}

RELATIONSHIP_TYPES = {
    'contains', 'part_of', 'parent_series', 'subseries', 'follows', 'followed_by',
    'sequel_to', 'prequel_to', 'direct_sequel_series', 'direct_prequel_series',
    'spin_off_of', 'character_spin_off', 'interquel', 'parallel_story',
    'crossover_with', 'same_universe', 'adaptation_of', 'remake_of', 'reboot_of',
    'alternate_continuity_of', 'edition_of', 'collection_of', 'includes_work',
    'supersedes', 'superseded_by', 'created_by', 'possible_duplicate_of',
}

_RELATIONSHIP_STATUSES = {'accepted', 'candidate', 'ignored'}
_REVIEW_STATUSES = {'open', 'accepted', 'rejected', 'dismissed'}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def initialize_media_graph(db):
    """Create graph tables and attach pre-existing watched rows without merging them."""
    db.executescript('''
        CREATE TABLE IF NOT EXISTS media_entities (
            id INTEGER PRIMARY KEY,
            entity_type TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'unknown',
            title TEXT NOT NULL,
            canonical_url TEXT,
            release_date TEXT,
            status TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS media_entity_aliases (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            alias TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT '',
            source_url TEXT,
            observed_at TEXT NOT NULL,
            UNIQUE(entity_id, alias, provider)
        );
        CREATE TABLE IF NOT EXISTS media_external_ids (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            source_url TEXT,
            observed_at TEXT NOT NULL,
            UNIQUE(provider, external_id)
        );
        CREATE TABLE IF NOT EXISTS media_relationships (
            id INTEGER PRIMARY KEY,
            source_entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            target_entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            relationship_type TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            inferred INTEGER NOT NULL DEFAULT 0 CHECK(inferred IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'candidate'
                CHECK(status IN ('accepted', 'candidate', 'ignored')),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_entity_id, target_entity_id, relationship_type),
            CHECK(source_entity_id != target_entity_id)
        );
        CREATE TABLE IF NOT EXISTS media_relationship_evidence (
            id INTEGER PRIMARY KEY,
            relationship_id INTEGER NOT NULL REFERENCES media_relationships(id),
            provider TEXT NOT NULL,
            provider_entity_id TEXT,
            raw_relationship_type TEXT NOT NULL,
            source_url TEXT,
            observed_at TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            is_explicit INTEGER NOT NULL DEFAULT 1 CHECK(is_explicit IN (0, 1)),
            payload_json TEXT NOT NULL DEFAULT '{}',
            evidence_key TEXT NOT NULL,
            UNIQUE(relationship_id, evidence_key)
        );
        CREATE TABLE IF NOT EXISTS media_releases (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            release_type TEXT NOT NULL DEFAULT 'publication',
            release_date TEXT,
            region TEXT,
            platform TEXT,
            status TEXT,
            provider TEXT NOT NULL DEFAULT '',
            provider_release_id TEXT,
            source_url TEXT,
            observed_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(entity_id,provider,provider_release_id,release_type)
        );
        CREATE INDEX IF NOT EXISTS ix_media_releases_entity_date
            ON media_releases(entity_id, release_date);
        CREATE TABLE IF NOT EXISTS media_graph_snapshots (
            id INTEGER PRIMARY KEY,
            root_entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            graph_hash TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_media_graph_snapshots_root
            ON media_graph_snapshots(root_entity_id,captured_at,id);
        CREATE TABLE IF NOT EXISTS media_graph_changes (
            id INTEGER PRIMARY KEY,
            root_entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            snapshot_id INTEGER NOT NULL REFERENCES media_graph_snapshots(id),
            change_type TEXT NOT NULL,
            entity_id INTEGER,
            relationship_id INTEGER,
            change_key TEXT NOT NULL,
            before_json TEXT,
            after_json TEXT,
            detected_at TEXT NOT NULL,
            UNIQUE(snapshot_id, change_key)
        );
        CREATE TABLE IF NOT EXISTS media_review_queue (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER REFERENCES media_entities(id),
            relationship_id INTEGER REFERENCES media_relationships(id),
            issue_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open', 'accepted', 'rejected', 'dismissed')),
            title TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ix_media_review_open_key
            ON media_review_queue(dedupe_key) WHERE status='open';
        CREATE INDEX IF NOT EXISTS ix_media_relationships_source
            ON media_relationships(source_entity_id, relationship_type);
        CREATE INDEX IF NOT EXISTS ix_media_relationships_target
            ON media_relationships(target_entity_id, relationship_type);
    ''')

    columns = {row['name'] for row in db.execute('PRAGMA table_info(watched_items)')}
    if 'media_entity_id' not in columns:
        db.execute('ALTER TABLE watched_items ADD COLUMN media_entity_id INTEGER REFERENCES media_entities(id)')
    _migrate_watched_rows(db)
    db.commit()


def _entity_type_for_watch(kind):
    if kind == 'author':
        return 'person'
    if kind in {'series', 'season', 'arc', 'franchise', 'universe', 'continuity',
                'edition', 'collection', 'release'}:
        return kind
    return 'work'


def _media_type_for_watch(kind):
    if kind in {'book', 'lightnovel', 'series', 'author'}:
        return 'book'
    if kind == 'manga':
        return 'manga'
    if kind in {'comic', 'comic_issue', 'webcomic', 'webtoon'}:
        return 'comic'
    if kind in {'webfiction', 'fanfiction'}:
        return kind
    return kind or 'unknown'


def _insert_alias(db, entity_id, alias, provider='', source_url=None, observed_at=None):
    alias = str(alias or '').strip()
    if alias:
        db.execute('''INSERT OR IGNORE INTO media_entity_aliases
            (entity_id,alias,provider,source_url,observed_at) VALUES (?,?,?,?,?)''',
                   (entity_id, alias, str(provider or '').casefold(), source_url,
                    observed_at or _now()))


def _link_external_id(db, entity_id, provider, external_id, source_url=None,
                      observed_at=None):
    provider = str(provider or '').strip().casefold()
    external_id = str(external_id or '').strip()
    if not provider or not external_id or len(provider) > 100 or len(external_id) > 512:
        raise ValueError('External IDs require a provider and an ID no longer than 512 characters')
    existing = db.execute('''SELECT entity_id FROM media_external_ids
        WHERE provider=? AND external_id=?''', (provider, external_id)).fetchone()
    if existing and int(existing['entity_id']) != int(entity_id):
        raise ValueError(f'{provider} ID {external_id} is already linked to media entity {existing["entity_id"]}')
    db.execute('''INSERT INTO media_external_ids
        (entity_id,provider,external_id,source_url,observed_at) VALUES (?,?,?,?,?)
        ON CONFLICT(provider,external_id) DO UPDATE SET
            source_url=COALESCE(excluded.source_url,media_external_ids.source_url),
            observed_at=excluded.observed_at''',
               (entity_id, provider, external_id, source_url, observed_at or _now()))


def upsert_media_entity(db, entity_type, title, media_type='unknown', canonical_url=None,
                        release_date=None, status=None, metadata=None, aliases=None,
                        external_ids=None, entity_id=None, provider=''):
    """Create or update an entity by stable external ID; ambiguous IDs never merge silently."""
    entity_type = str(entity_type or 'work').strip().casefold()
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f'Unsupported media entity type: {entity_type}')
    title = str(title or '').strip()
    if not title or len(title) > 500:
        raise ValueError('Media entity title must contain 1 to 500 characters')
    external_ids = list(external_ids or [])
    matched = set()
    for value in external_ids:
        if isinstance(value, dict):
            ext_provider, ext_id = value.get('provider'), value.get('external_id')
        else:
            ext_provider, ext_id = value[0], value[1]
        row = db.execute('''SELECT entity_id FROM media_external_ids
            WHERE provider=? AND external_id=?''',
                         (str(ext_provider or '').casefold(), str(ext_id or ''))).fetchone()
        if row:
            matched.add(int(row['entity_id']))
    if len(matched) > 1:
        raise ValueError('The supplied external IDs point to different media entities; review the identity match')
    if entity_id is not None:
        existing_entity = db.execute('SELECT * FROM media_entities WHERE id=?', (entity_id,)).fetchone()
        if not existing_entity:
            raise ValueError(f'Unknown media entity ID: {entity_id}')
        if matched and int(entity_id) not in matched:
            raise ValueError('An external ID is already attached to another media entity; resolve it before merging')
        row = existing_entity
        keep_title = True
    else:
        row = db.execute('SELECT * FROM media_entities WHERE id=?',
                          (next(iter(matched)),)).fetchone() if matched else None
        keep_title = False

    values = dict(metadata or {})
    if row:
        entity_id = int(row['id'])
        old_meta = json.loads(row['metadata_json'] or '{}')
        old_meta.update(values)
        if keep_title and row['title'].casefold() != title.casefold():
            _insert_alias(db, entity_id, title, provider, canonical_url)
            title = row['title']
        elif not keep_title:
            _insert_alias(db, entity_id, row['title'], provider, canonical_url)
        next_type = entity_type if row['entity_type'] == 'work' else row['entity_type']
        next_media_type = media_type if row['media_type'] in ('', 'unknown') else row['media_type']
        db.execute('''UPDATE media_entities SET entity_type=?,media_type=?,title=?,
            canonical_url=COALESCE(canonical_url,?),release_date=COALESCE(?,release_date),
            status=COALESCE(?,status),metadata_json=?,updated_at=? WHERE id=?''',
                   (next_type, media_type if next_media_type == 'unknown' else next_media_type,
                    title, canonical_url, release_date, status, _json(old_meta), _now(), entity_id))
    else:
        cursor = db.execute('''INSERT INTO media_entities
            (entity_type,media_type,title,canonical_url,release_date,status,metadata_json)
            VALUES (?,?,?,?,?,?,?)''',
                            (entity_type, media_type or 'unknown', title, canonical_url,
                             release_date, status, _json(values)))
        entity_id = int(cursor.lastrowid)

    for value in external_ids:
        if isinstance(value, dict):
            ext_provider, ext_id = value.get('provider'), value.get('external_id')
            source_url = value.get('source_url')
        else:
            ext_provider, ext_id = value[0], value[1]
            source_url = value[2] if len(value) > 2 else None
        _link_external_id(db, entity_id, ext_provider, ext_id, source_url)
    for alias in aliases or []:
        if isinstance(alias, str):
            _insert_alias(db, entity_id, alias, provider, canonical_url)
        else:
            _insert_alias(db, entity_id, alias.get('alias'), alias.get('provider', provider),
                          alias.get('source_url'), alias.get('observed_at'))
    if provider:
        _insert_alias(db, entity_id, title, provider, canonical_url)
    return entity_id


def add_media_relationship(db, source_entity_id, target_entity_id, relationship_type,
                           confidence=0.5, inferred=False, status='candidate', provider='',
                           provider_entity_id=None, raw_relationship_type=None,
                           source_url=None, observed_at=None, evidence=None, metadata=None,
                           is_explicit=True):
    relationship_type = str(relationship_type or '').strip().casefold()
    if relationship_type not in RELATIONSHIP_TYPES:
        raise ValueError(f'Unsupported media relationship type: {relationship_type}')
    if int(source_entity_id) == int(target_entity_id):
        raise ValueError('A media entity cannot relate to itself')
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError('Relationship confidence must be between 0 and 1')
    if status not in _RELATIONSHIP_STATUSES:
        raise ValueError(f'Unsupported relationship status: {status}')
    for entity_id in (source_entity_id, target_entity_id):
        if not db.execute('SELECT 1 FROM media_entities WHERE id=?', (entity_id,)).fetchone():
            raise ValueError(f'Unknown media entity ID: {entity_id}')
    row = db.execute('''SELECT * FROM media_relationships WHERE source_entity_id=?
        AND target_entity_id=? AND relationship_type=?''',
                     (source_entity_id, target_entity_id, relationship_type)).fetchone()
    if row:
        relationship_id = int(row['id'])
        merged_status = ('accepted' if 'accepted' in (row['status'], status) else
                         'candidate' if 'candidate' in (row['status'], status) else 'ignored')
        old_metadata = json.loads(row['metadata_json'] or '{}')
        old_metadata.update(metadata or {})
        db.execute('''UPDATE media_relationships SET confidence=?,inferred=?,status=?,
            metadata_json=?,updated_at=? WHERE id=?''',
                   (max(float(row['confidence']), confidence),
                    int(bool(row['inferred']) and bool(inferred)), merged_status,
                    _json(old_metadata), _now(), relationship_id))
    else:
        cursor = db.execute('''INSERT INTO media_relationships
            (source_entity_id,target_entity_id,relationship_type,confidence,inferred,status,metadata_json)
            VALUES (?,?,?,?,?,?,?)''',
                            (source_entity_id, target_entity_id, relationship_type, confidence,
                             int(bool(inferred)), status, _json(metadata or {})))
        relationship_id = int(cursor.lastrowid)

    evidence_value = dict(evidence or {})
    evidence_value.update({key: value for key, value in {
        'provider_entity_id': provider_entity_id,
        'raw_relationship_type': raw_relationship_type or relationship_type,
        'source_url': source_url,
    }.items() if value is not None})
    evidence_row = {
        'provider': str(provider or 'unknown').casefold(),
        'provider_entity_id': provider_entity_id,
        'raw_relationship_type': str(raw_relationship_type or relationship_type),
        'source_url': source_url,
        'observed_at': observed_at or _now(),
        'confidence': confidence,
        'is_explicit': bool(is_explicit),
        'payload_json': _json(evidence_value),
    }
    evidence_key = _digest({key: value for key, value in evidence_row.items()
                            if key != 'observed_at'})
    db.execute('''INSERT INTO media_relationship_evidence
        (relationship_id,provider,provider_entity_id,raw_relationship_type,source_url,
         observed_at,confidence,is_explicit,payload_json,evidence_key)
        VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(relationship_id,evidence_key) DO UPDATE SET
            observed_at=excluded.observed_at''',
               (relationship_id, evidence_row['provider'], provider_entity_id,
                evidence_row['raw_relationship_type'], source_url, evidence_row['observed_at'],
                confidence, int(bool(is_explicit)), evidence_row['payload_json'], evidence_key))
    if confidence < 0.65 or not is_explicit:
        queue_graph_review(db, 'uncertain_relationship',
                           f'Review {relationship_type.replace("_", " ")} relationship',
                           {'confidence': confidence, 'provider': provider,
                            'raw_relationship_type': raw_relationship_type},
                           relationship_id=relationship_id)
    return relationship_id


def queue_graph_review(db, issue_type, title, detail=None, entity_id=None,
                       relationship_id=None, dedupe_key=None):
    detail_json = _json(detail or {})
    dedupe_key = dedupe_key or _digest({
        'issue_type': issue_type, 'entity_id': entity_id,
        'relationship_id': relationship_id, 'detail': detail or {},
    })
    row = db.execute("SELECT id FROM media_review_queue WHERE dedupe_key=? AND status='open'",
                     (dedupe_key,)).fetchone()
    if row:
        return int(row['id'])
    cursor = db.execute('''INSERT INTO media_review_queue
        (entity_id,relationship_id,issue_type,status,title,detail_json,dedupe_key,created_at,updated_at)
        VALUES (?,?,?,'open',?,?,?,?,?)''',
                        (entity_id, relationship_id, issue_type, str(title), detail_json,
                         dedupe_key, _now(), _now()))
    return int(cursor.lastrowid)


def list_graph_reviews(db, status='open'):
    if status is not None and status not in _REVIEW_STATUSES:
        raise ValueError(f'Unsupported review status: {status}')
    rows = db.execute('''SELECT * FROM media_review_queue
        WHERE (? IS NULL OR status=?) ORDER BY created_at,id''', (status, status)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['detail'] = json.loads(item.pop('detail_json') or '{}')
        result.append(item)
    return result


def resolve_graph_review(db, review_id, status):
    if status not in {'accepted', 'rejected', 'dismissed'}:
        raise ValueError('Review resolution must be accepted, rejected, or dismissed')
    cursor = db.execute('''UPDATE media_review_queue SET status=?,updated_at=?
        WHERE id=? AND status='open' ''', (status, _now(), int(review_id)))
    if cursor.rowcount == 0:
        raise ValueError(f'No open media graph review item: {review_id}')
    return True


def _register_watch_url(db, entity_id, source_url, watch_id):
    if not source_url:
        return
    parsed = urllib.parse.urlsplit(source_url)
    host = (parsed.hostname or '').casefold()
    if parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password:
        return
    provider = 'source-url:' + host
    try:
        _link_external_id(db, entity_id, provider, source_url, source_url)
    except ValueError as exc:
        existing = db.execute('''SELECT entity_id FROM media_external_ids
            WHERE provider=? AND external_id=?''', (provider, source_url)).fetchone()
        queue_graph_review(db, 'suspected_duplicate', 'Watched URL is linked to multiple entities',
                           {'watch_id': watch_id, 'other_entity_id': existing['entity_id'] if existing else None,
                            'source_url': source_url, 'error': str(exc)}, entity_id=entity_id,
                           dedupe_key=f'watch-url-collision:{source_url}')


def _watch_external_ids(metadata):
    result = []
    catalog_provider = str(metadata.get('catalog_provider') or '').strip().casefold()
    catalog = metadata.get('catalog_metadata') or {}
    if not catalog:
        catalog = metadata.get('lightnovel') or {}
    if catalog_provider and isinstance(catalog, dict):
        catalog_id = (catalog.get('catalog_id') or catalog.get('goodreads_id') or
                      catalog.get('openlibrary_id'))
        if catalog_id:
            provider = {'open library': 'openlibrary', 'goodreads': 'goodreads'}.get(
                catalog_provider, catalog_provider.replace(' ', ''))
            result.append({'provider': provider, 'external_id': str(catalog_id),
                           'source_url': metadata.get('source_url')})
    comic = metadata.get('comic') or {}
    for key, provider in (('anilist_id', 'anilist'), ('mangadex_id', 'mangadex'),
                          ('taddy_id', 'taddy'), ('comicvine_id', 'comicvine'),
                          ('ranobedb_id', 'ranobedb')):
        value = (comic.get(key) if isinstance(comic, dict) else None)
        if value is None and isinstance(metadata.get('lightnovel'), dict):
            value = metadata['lightnovel'].get(key)
        if value is not None and str(value).strip():
            result.append({'provider': provider, 'external_id': str(value).strip()})
    return result


def _sync_watch_row(db, watch):
    watch_id = int(watch['id'])
    kind = str(watch['kind'] or '')
    metadata = json.loads(watch['metadata'] or '{}')
    entity_type = _entity_type_for_watch(kind)
    media_type = _media_type_for_watch(kind)
    linked_id = watch['media_entity_id'] if 'media_entity_id' in watch.keys() else None
    if linked_id and not db.execute('SELECT 1 FROM media_entities WHERE id=?', (linked_id,)).fetchone():
        linked_id = None
    preferred_id = linked_id
    provider_ids = _watch_external_ids(metadata)
    matched_entities = set()
    for external_id in provider_ids:
        match = db.execute('''SELECT entity_id FROM media_external_ids
            WHERE provider=? AND external_id=?''',
                           (external_id['provider'].casefold(), external_id['external_id'])).fetchone()
        if match:
            matched_entities.add(int(match['entity_id']))
    if linked_id is None and len(matched_entities) == 1:
        preferred_id = next(iter(matched_entities))
    elif linked_id is not None and any(value != int(linked_id) for value in matched_entities):
        queue_graph_review(db, 'suspected_duplicate', 'Watched metadata IDs refer to another entity',
                           {'watch_id': watch_id, 'watched_entity_id': int(linked_id),
                            'provider_entity_ids': sorted(matched_entities)}, entity_id=int(linked_id),
                           dedupe_key=f'watch-metadata-id-conflict:{watch_id}')
    elif linked_id is None and len(matched_entities) > 1:
        queue_graph_review(db, 'ambiguous_entity', 'Watched metadata IDs point to different entities',
                           {'watch_id': watch_id, 'provider_entity_ids': sorted(matched_entities)},
                           dedupe_key=f'watch-metadata-id-ambiguous:{watch_id}')
    entity_id = upsert_media_entity(
        db, entity_type, watch['title'], media_type=media_type,
        canonical_url=watch['source_url'], release_date=watch['release_date'],
        status=watch['state'], metadata={'watch_id': watch_id, 'watch_kind': kind},
        external_ids=[{'provider': 'media-intake', 'external_id': f'watch:{watch_id}'}],
        entity_id=preferred_id, provider='Media Intake')
    for external_id in provider_ids:
        try:
            _link_external_id(db, entity_id, external_id['provider'],
                              external_id['external_id'], external_id.get('source_url'))
        except ValueError as exc:
            other = db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider=? AND external_id=?''',
                               (external_id['provider'].casefold(),
                                external_id['external_id'])).fetchone()
            queue_graph_review(db, 'suspected_duplicate', 'Provider ID is linked to another entity',
                               {'watch_id': watch_id, 'provider': external_id['provider'],
                                'external_id': external_id['external_id'],
                                'other_entity_id': other['entity_id'] if other else None,
                                'error': str(exc)}, entity_id=entity_id,
                               dedupe_key=f'watch-provider-id-collision:{external_id["provider"]}:{external_id["external_id"]}')
    _register_watch_url(db, entity_id, watch['source_url'], watch_id)
    if linked_id != entity_id:
        db.execute('UPDATE watched_items SET media_entity_id=? WHERE id=?', (entity_id, watch_id))
    return entity_id


def _sync_watch_parent(db, watch):
    if not watch['parent_id']:
        return None
    parent = db.execute('SELECT * FROM watched_items WHERE id=?', (watch['parent_id'],)).fetchone()
    if not parent:
        return None
    source_id = watch['media_entity_id'] or _sync_watch_row(db, watch)
    target_id = parent['media_entity_id'] or _sync_watch_row(db, parent)
    rel_type = 'created_by' if parent['kind'] == 'author' else 'part_of'
    existing_direct = db.execute('''SELECT 1 FROM media_relationships
        WHERE source_entity_id=? AND target_entity_id=? AND relationship_type IN ('part_of','parent_series')''',
                                  (source_id, target_id)).fetchone()
    existing_inverse = db.execute('''SELECT 1 FROM media_relationships
        WHERE source_entity_id=? AND target_entity_id=? AND relationship_type IN ('contains','includes_work')''',
                                   (target_id, source_id)).fetchone()
    if (existing_direct or existing_inverse) and rel_type == 'part_of':
        return None
    return add_media_relationship(
        db, source_id, target_id, rel_type, confidence=1, inferred=False,
        status='accepted', provider='media-intake', provider_entity_id=str(watch['id']),
        raw_relationship_type='watched-parent', evidence={'watch_parent_id': parent['id']},
        source_url=watch['source_url'], is_explicit=True)


def sync_watch_entity(db, watch_id):
    """Link a newly added or updated watch to a graph entity and its parent relation."""
    watch = db.execute('SELECT * FROM watched_items WHERE id=?', (int(watch_id),)).fetchone()
    if not watch:
        raise ValueError(f'Unknown watched item: {watch_id}')
    entity_id = _sync_watch_row(db, watch)
    watch = db.execute('SELECT * FROM watched_items WHERE id=?', (int(watch_id),)).fetchone()
    _sync_watch_parent(db, watch)
    return entity_id


def _migrate_watched_rows(db):
    rows = db.execute('''SELECT * FROM watched_items WHERE media_entity_id IS NULL
        ORDER BY id''').fetchall()
    for watch in rows:
        _sync_watch_row(db, watch)
    rows = db.execute('''SELECT * FROM watched_items WHERE parent_id IS NOT NULL
        AND media_entity_id IS NOT NULL ORDER BY id''').fetchall()
    for watch in rows:
        parent = db.execute('SELECT media_entity_id,kind FROM watched_items WHERE id=?',
                            (watch['parent_id'],)).fetchone()
        rel_type = 'created_by' if parent and parent['kind'] == 'author' else 'part_of'
        exists = (db.execute('''SELECT 1 FROM media_relationships
            WHERE source_entity_id=? AND target_entity_id=? AND relationship_type=?''',
                             (watch['media_entity_id'], parent['media_entity_id'], rel_type)).fetchone()
                  if parent and parent['media_entity_id'] else None)
        if parent and parent['media_entity_id'] and not exists:
            _sync_watch_parent(db, watch)


def get_media_graph(db, root_entity_id, max_depth=3, max_entities=500):
    """Return a bounded, undirected neighborhood around one entity."""
    max_depth = max(0, min(int(max_depth), 8))
    max_entities = max(1, min(int(max_entities), 2000))
    root_entity_id = int(root_entity_id)
    if not db.execute('SELECT 1 FROM media_entities WHERE id=?', (root_entity_id,)).fetchone():
        raise ValueError(f'Unknown media entity ID: {root_entity_id}')
    found = {root_entity_id}
    frontier = {root_entity_id}
    truncated = False
    for _ in range(max_depth):
        if not frontier:
            break
        placeholders = ','.join('?' for _ in frontier)
        args = tuple(frontier) + tuple(frontier)
        rows = db.execute(f'''SELECT source_entity_id,target_entity_id FROM media_relationships
            WHERE source_entity_id IN ({placeholders}) OR target_entity_id IN ({placeholders})''', args).fetchall()
        next_frontier = set()
        for row in rows:
            for entity_id in (int(row['source_entity_id']), int(row['target_entity_id'])):
                if entity_id in found or entity_id in next_frontier:
                    continue
                if len(found) + len(next_frontier) < max_entities:
                    next_frontier.add(entity_id)
                else:
                    truncated = True
        found.update(next_frontier)
        frontier = next_frontier
    ordered_ids = sorted(found)
    placeholders = ','.join('?' for _ in ordered_ids)
    entities = [dict(row) for row in db.execute(
        f'SELECT * FROM media_entities WHERE id IN ({placeholders}) ORDER BY id', ordered_ids)]
    relationships = [dict(row) for row in db.execute(f'''SELECT * FROM media_relationships
        WHERE source_entity_id IN ({placeholders}) AND target_entity_id IN ({placeholders})
        ORDER BY id''', tuple(ordered_ids) + tuple(ordered_ids))]
    for entity in entities:
        entity['metadata'] = json.loads(entity.pop('metadata_json') or '{}')
        entity['external_ids'] = [dict(row) for row in db.execute(
            'SELECT provider,external_id,source_url FROM media_external_ids WHERE entity_id=? ORDER BY provider,external_id',
            (entity['id'],))]
        entity['aliases'] = [dict(row) for row in db.execute(
            'SELECT alias,provider,source_url FROM media_entity_aliases WHERE entity_id=? ORDER BY alias',
            (entity['id'],))]
    for relationship in relationships:
        relationship['metadata'] = json.loads(relationship.pop('metadata_json') or '{}')
        relationship['evidence'] = [dict(row) for row in db.execute(
            'SELECT provider,provider_entity_id,raw_relationship_type,source_url,observed_at,confidence,is_explicit,payload_json '
            'FROM media_relationship_evidence WHERE relationship_id=? ORDER BY id',
            (relationship['id'],))]
        for evidence_row in relationship['evidence']:
            evidence_row['payload'] = json.loads(evidence_row.pop('payload_json') or '{}')
    return {'root_entity_id': root_entity_id, 'entities': entities,
            'relationships': relationships, 'truncated': truncated}


def capture_graph_snapshot(db, root_entity_id, max_depth=3, max_entities=500):
    """Save a graph snapshot and persist an idempotent diff from its predecessor."""
    graph = get_media_graph(db, root_entity_id, max_depth=max_depth, max_entities=max_entities)
    stable_graph = dict(graph)
    stable_graph['entities'] = []
    for entity in graph['entities']:
        stable_graph['entities'].append({key: value for key, value in entity.items()
                                         if key not in {'created_at', 'updated_at'}})
    stable_graph['relationships'] = []
    for relationship in graph['relationships']:
        stable_relationship = {key: value for key, value in relationship.items()
                               if key not in {'created_at', 'updated_at'}}
        stable_relationship['evidence'] = []
        seen_evidence = set()
        for evidence_row in relationship['evidence']:
            stable_evidence = {key: value for key, value in evidence_row.items()
                               if key != 'observed_at'}
            evidence_key = _digest(stable_evidence)
            if evidence_key not in seen_evidence:
                seen_evidence.add(evidence_key)
                stable_relationship['evidence'].append(stable_evidence)
        stable_graph['relationships'].append(stable_relationship)
    snapshot_json = _json(stable_graph)
    graph_hash = hashlib.sha256(snapshot_json.encode('utf-8')).hexdigest()
    latest = db.execute('''SELECT * FROM media_graph_snapshots WHERE root_entity_id=?
        ORDER BY captured_at DESC,id DESC LIMIT 1''', (root_entity_id,)).fetchone()
    if latest and latest['graph_hash'] == graph_hash:
        return {'snapshot_id': int(latest['id']), 'baseline': False, 'unchanged': True, 'changes': []}
    cursor = db.execute('''INSERT INTO media_graph_snapshots
        (root_entity_id,graph_hash,snapshot_json,captured_at) VALUES (?,?,?,?)''',
                        (root_entity_id, graph_hash, snapshot_json, _now()))
    snapshot_id = int(cursor.lastrowid)
    if not latest:
        return {'snapshot_id': snapshot_id, 'baseline': True, 'unchanged': False, 'changes': []}

    before = json.loads(latest['snapshot_json'])
    before_entities = {str(item['id']): item for item in before['entities']}
    after_entities = {str(item['id']): item for item in stable_graph['entities']}
    before_edges = {str(item['id']): item for item in before['relationships']}
    after_edges = {str(item['id']): item for item in stable_graph['relationships']}
    changes = []

    def record(change_type, key, old, new, entity_id=None, relationship_id=None):
        change_key = f'{change_type}:{key}:{_digest([old, new])[:16]}'
        values = {'change_type': change_type, 'change_key': change_key,
                  'before': old, 'after': new, 'entity_id': entity_id,
                  'relationship_id': relationship_id}
        db.execute('''INSERT OR IGNORE INTO media_graph_changes
            (root_entity_id,snapshot_id,change_type,entity_id,relationship_id,change_key,
             before_json,after_json,detected_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                   (root_entity_id, snapshot_id, change_type, entity_id, relationship_id,
                    change_key, _json(old) if old is not None else None,
                    _json(new) if new is not None else None, _now()))
        changes.append(values)

    for key in sorted(after_entities.keys() - before_entities.keys(), key=int):
        record('entity_added', key, None, after_entities[key], int(key))
    for key in sorted(before_entities.keys() - after_entities.keys(), key=int):
        record('entity_removed', key, before_entities[key], None, int(key))
    for key in sorted(before_entities.keys() & after_entities.keys(), key=int):
        if before_entities[key] != after_entities[key]:
            record('entity_updated', key, before_entities[key], after_entities[key], int(key))
    for key in sorted(after_edges.keys() - before_edges.keys(), key=int):
        record('relationship_added', key, None, after_edges[key],
               relationship_id=int(key))
    for key in sorted(before_edges.keys() - after_edges.keys(), key=int):
        record('relationship_removed', key, before_edges[key], None,
               relationship_id=int(key))
    for key in sorted(before_edges.keys() & after_edges.keys(), key=int):
        if before_edges[key] != after_edges[key]:
            record('relationship_updated', key, before_edges[key], after_edges[key],
                   relationship_id=int(key))
    return {'snapshot_id': snapshot_id, 'baseline': False, 'unchanged': not changes,
            'changes': changes}
