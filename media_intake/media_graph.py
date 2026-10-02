"""Provider-neutral media entities, relationships, provenance, and graph history."""
import hashlib
import json
import math
import re
import unicodedata
import urllib.parse
from datetime import date, datetime, timezone

from .book_identifiers import book_work_title_key


ENTITY_TYPES = {
    'franchise', 'universe', 'continuity', 'series', 'season', 'arc', 'work',
    'edition', 'collection', 'release', 'person', 'creator',
}

RELATIONSHIP_TYPES = {
    'contains', 'part_of', 'parent_series', 'subseries', 'follows', 'followed_by',
    'season_of', 'episode_of', 'special_of',
    'sequel_to', 'prequel_to', 'direct_sequel_series', 'direct_prequel_series',
    'spin_off_of', 'character_spin_off', 'interquel', 'parallel_story',
    'crossover_with', 'same_universe', 'adaptation_of', 'remake_of', 'reboot_of',
    'revival_of', 'derivative_work', 'alternate_continuity_of',
    'alternate_version_of', 'alternate_cut_of',
    'edition_of', 'collection_of', 'includes_work',
    'supersedes', 'superseded_by', 'created_by', 'possible_duplicate_of',
}

_RELATIONSHIP_STATUSES = {'accepted', 'candidate', 'ignored'}
_REVIEW_STATUSES = {'open', 'accepted', 'rejected', 'dismissed'}
_ANNOUNCEMENT_EVENT_TYPES = {
    'series_cancellation', 'series_renewal', 'series_hiatus',
    'release_date_change', 'premiere_announcement', 'availability_announcement',
}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _identity_title_key(value, media_type='unknown', entity_type='work'):
    if str(media_type or '').casefold() == 'book' and entity_type == 'work':
        return book_work_title_key(value)
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    return ''.join(char for char in text if char.isalnum())


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
            is_canonical INTEGER NOT NULL DEFAULT 0 CHECK(is_canonical IN (0, 1)),
            UNIQUE(entity_id,provider,provider_release_id,release_type)
        );
        CREATE INDEX IF NOT EXISTS ix_media_releases_entity_date
            ON media_releases(entity_id, release_date);
        CREATE TABLE IF NOT EXISTS media_claim_observations (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER NOT NULL REFERENCES media_entities(id),
            field_name TEXT NOT NULL,
            value_json TEXT NOT NULL,
            provider TEXT NOT NULL,
            source_url TEXT,
            observed_at TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5 CHECK(confidence >= 0 AND confidence <= 1),
            is_canonical INTEGER NOT NULL DEFAULT 0 CHECK(is_canonical IN (0, 1)),
            claim_key TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS ix_media_claim_entity_field
            ON media_claim_observations(entity_id,field_name,observed_at);
        CREATE TABLE IF NOT EXISTS media_watch_policies (
            id INTEGER PRIMARY KEY,
            scope_type TEXT NOT NULL CHECK(scope_type IN
                ('global','media_type','franchise','series','season','work')),
            scope_key TEXT NOT NULL DEFAULT '',
            policy_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            UNIQUE(scope_type,scope_key)
        );
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

    release_columns = {row['name'] for row in db.execute('PRAGMA table_info(media_releases)')}
    if 'is_canonical' not in release_columns:
        db.execute('''ALTER TABLE media_releases ADD COLUMN is_canonical INTEGER NOT NULL
            DEFAULT 0 CHECK(is_canonical IN (0, 1))''')

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
    if kind == 'book_list':
        return 'collection'
    return 'work'


def _media_type_for_watch(kind):
    if kind in {'book', 'lightnovel', 'series', 'author', 'book_list'}:
        return 'book'
    if kind == 'manga':
        return 'manga'
    if kind in {'comic', 'comic_issue', 'webcomic', 'webtoon'}:
        return 'comic'
    if kind in {'webfiction', 'fanfiction'}:
        return kind
    if kind in {'video', 'video_episode'}:
        return 'video'
    if kind == 'game':
        return 'game'
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
    possible_duplicate_ids = []
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
        field_sources = old_meta.setdefault('_field_sources', {})
        if not isinstance(field_sources, dict):
            field_sources = old_meta['_field_sources'] = {}
        old_meta.update(values)
        incoming_priority = _claim_confidence(provider)
        next_title = row['title']
        if row['title'].casefold() != title.casefold():
            old_priority = _claim_confidence(field_sources.get('title'))
            if not row['title'] or incoming_priority >= old_priority:
                _insert_alias(db, entity_id, row['title'], field_sources.get('title') or provider,
                              row['canonical_url'])
                next_title = title
                field_sources['title'] = provider or 'unknown'
            else:
                _insert_alias(db, entity_id, title, provider, canonical_url)
        for field_name, value in (('media_type', media_type), ('canonical_url', canonical_url),
                                  ('release_date', release_date), ('status', status)):
            if value is None or value == '':
                continue
            if field_name == 'media_type' and value == 'unknown':
                continue
            current_value = row[field_name]
            old_priority = _claim_confidence(field_sources.get(field_name))
            if current_value in (None, '') or incoming_priority >= old_priority:
                field_sources[field_name] = provider or 'unknown'
            else:
                if field_name == 'media_type':
                    media_type = current_value
                elif field_name == 'canonical_url':
                    canonical_url = current_value
                elif field_name == 'release_date':
                    release_date = current_value
                elif field_name == 'status':
                    status = current_value
        next_type = entity_type if row['entity_type'] == 'work' else row['entity_type']
        next_media_type = media_type or row['media_type']
        db.execute('''UPDATE media_entities SET entity_type=?,media_type=?,title=?,
            canonical_url=?,release_date=?,status=?,metadata_json=?,updated_at=? WHERE id=?''',
                   (next_type, next_media_type if next_media_type != 'unknown' else row['media_type'],
                    next_title, canonical_url or row['canonical_url'], release_date or row['release_date'],
                    status or row['status'], _json(old_meta), _now(), entity_id))
        title = next_title
    else:
        values['_field_sources'] = {field: provider or 'unknown' for field, value in (
            ('title', title), ('media_type', media_type), ('canonical_url', canonical_url),
            ('release_date', release_date), ('status', status)) if value not in (None, '')}
        cursor = db.execute('''INSERT INTO media_entities
            (entity_type,media_type,title,canonical_url,release_date,status,metadata_json)
            VALUES (?,?,?,?,?,?,?)''',
                            (entity_type, media_type or 'unknown', title, canonical_url,
                             release_date, status, _json(values)))
        entity_id = int(cursor.lastrowid)
        title_key = _identity_title_key(title, media_type, entity_type)
        if title_key:
            possible_duplicate_ids = [int(candidate['id']) for candidate in db.execute(
                'SELECT id,title FROM media_entities WHERE id!=? AND entity_type=? AND media_type=? ORDER BY id DESC LIMIT 100',
                (entity_id, entity_type, media_type or 'unknown'))
                if _identity_title_key(candidate['title'], media_type, entity_type) == title_key][:5]

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
    current = db.execute('SELECT title,media_type,canonical_url,release_date,status,metadata_json '
                         'FROM media_entities WHERE id=?', (entity_id,)).fetchone()
    for field_name, value in (
            ('title', title), ('media_type', media_type), ('canonical_url', canonical_url),
            ('release_date', release_date), ('status', status)):
        if value is None or value == '':
            continue
        canonical_value = current[field_name] if current else None
        record_media_claim(db, entity_id, field_name, value, provider or 'unknown',
                           source_url=canonical_url, confidence=_claim_confidence(provider),
            is_canonical=(value == canonical_value))
    for duplicate_id in possible_duplicate_ids:
        relationship_id = add_media_relationship(
            db, int(entity_id), duplicate_id, 'possible_duplicate_of',
            confidence=0.55, inferred=True, status='candidate', provider='Media Intake',
            raw_relationship_type='exact-title-match', source_url=canonical_url,
            evidence={'reason': 'same normalized title and media/entity type',
                      'candidate_entity_id': duplicate_id}, is_explicit=False)
        review = db.execute('''SELECT id FROM media_review_queue
            WHERE relationship_id=? AND status='open' ORDER BY id LIMIT 1''',
                            (relationship_id,)).fetchone()
        if review:
            db.execute('''UPDATE media_review_queue SET entity_id=?,issue_type='suspected_duplicate',
                title=?,detail_json=?,updated_at=? WHERE id=?''',
                       (int(entity_id), f'Review possible duplicate: {title}',
                        _json({'candidate_entity_id': duplicate_id, 'title': title,
                               'media_type': media_type, 'entity_type': entity_type}),
                        _now(), review['id']))
    try:
        entity_metadata = json.loads(current['metadata_json'] or '{}') if current else {}
    except (TypeError, ValueError):
        entity_metadata = {}
    for field_name in ('series_position', 'series_index', 'publication_status',
                       'video_subtype', 'game_subtype', 'release_date_source'):
        if field_name in values and values[field_name] is not None:
            record_media_claim(db, entity_id, field_name, values[field_name], provider or 'unknown',
                               source_url=canonical_url, confidence=_claim_confidence(provider),
                               is_canonical=(values[field_name] == entity_metadata.get(field_name)))
    for field_name in ('release_date', 'status', 'series_index'):
        _queue_provider_claim_conflicts(db, entity_id, field_name)
    return entity_id


def _claim_confidence(provider):
    provider = str(provider or '').casefold()
    if any(token in provider for token in ('media intake', 'manual')):
        return 1.0
    if any(token in provider for token in ('dragonsteel', 'publisher', 'official', 'studio', 'network')):
        return 0.98
    if provider in {'wikidata', 'tmdb', 'igdb', 'tvmaze', 'steam'}:
        return 0.82
    return 0.65


def record_media_claim(db, entity_id, field_name, value, provider, source_url=None,
                       confidence=0.5, is_canonical=False, observed_at=None):
    """Retain every distinct provider observation, even when it loses precedence."""
    if value is None:
        return None
    field_name = str(field_name or '').strip()[:120]
    provider = str(provider or 'unknown').strip().casefold()[:100]
    confidence = max(0.0, min(float(confidence), 1.0))
    value_json = _json(value)
    if is_canonical:
        db.execute('''UPDATE media_claim_observations SET is_canonical=0
            WHERE entity_id=? AND field_name=? AND is_canonical=1''',
                   (int(entity_id), field_name))
    claim_key = _digest({'entity_id': int(entity_id), 'field_name': field_name,
                         'value_json': value_json, 'provider': provider,
                         'source_url': source_url})
    db.execute('''INSERT INTO media_claim_observations
        (entity_id,field_name,value_json,provider,source_url,observed_at,confidence,
         is_canonical,claim_key) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(claim_key) DO UPDATE SET observed_at=excluded.observed_at,
            confidence=MAX(media_claim_observations.confidence,excluded.confidence),
            is_canonical=excluded.is_canonical''',
               (int(entity_id), field_name, value_json, provider, source_url,
                observed_at or _now(), confidence, int(bool(is_canonical)), claim_key))
    return db.execute('SELECT id FROM media_claim_observations WHERE claim_key=?',
                      (claim_key,)).fetchone()['id']


DEFAULT_WATCH_POLICY = {
    'series_members': 'auto_add',
    'future_installments': 'auto_add',
    'direct_sequel_works': 'auto_add',
    'direct_sequel_series': 'auto_add',
    'direct_prequel_works': 'auto_add',
    'direct_prequel_series': 'auto_add',
    'canonical_side_works': 'auto_add',
    'spin_off_of': 'discover',
    'character_spin_off': 'discover',
    'interquel': 'auto_add',
    'parallel_story': 'discover',
    'crossover_with': 'discover',
    'same_universe': 'metadata_only',
    'adaptation_of': 'metadata_only',
    'derivative_work': 'discover',
    'remake_of': 'metadata_only',
    'reboot_of': 'metadata_only',
    'revival_of': 'metadata_only',
    'alternate_continuity_of': 'metadata_only',
    'alternate_version_of': 'metadata_only',
    'alternate_cut_of': 'metadata_only',
    'edition_of': 'ignore',
    'collection_of': 'ignore',
    'episodes': 'discover',
    'specials': 'discover',
    'new_seasons': 'auto_add',
    'game_dlc': 'discover',
    'game_expansions': 'auto_add',
    'remasters_and_ports': 'metadata_only',
}
_WATCH_POLICY_ACTIONS = {'auto_add', 'discover', 'ignore', 'metadata_only'}


def continuation_watch_policy_key(relationship_type, target_type='work', release_date=None):
    """Choose a sequel/prequel policy by target shape and exact release date."""
    relation = str(relationship_type or '').casefold()
    target = str(target_type or 'work').casefold()
    is_series = target in {'series', 'franchise'}
    if relation in {'followed_by', 'sequel_to'}:
        if is_series:
            return 'direct_sequel_series'
        try:
            if release_date and date.fromisoformat(str(release_date)) > date.today():
                return 'future_installments'
        except ValueError:
            pass
        return 'direct_sequel_works'
    if relation in {'follows', 'prequel_to'}:
        return 'direct_prequel_series' if is_series else 'direct_prequel_works'
    return None


def set_watch_policy(db, scope_type='global', scope_key='', policy=None):
    scope_type = str(scope_type or 'global').casefold()
    if scope_type not in {'global', 'media_type', 'franchise', 'series', 'season', 'work'}:
        raise ValueError('Unsupported watch-policy scope')
    scope_key = str(scope_key or '').strip()
    payload = dict(policy or {})
    for relationship, action in payload.items():
        if action not in _WATCH_POLICY_ACTIONS:
            raise ValueError(f'Unsupported watch action for {relationship}: {action}')
    db.execute('''INSERT INTO media_watch_policies(scope_type,scope_key,policy_json,updated_at)
        VALUES (?,?,?,?) ON CONFLICT(scope_type,scope_key) DO UPDATE SET
        policy_json=excluded.policy_json,updated_at=excluded.updated_at''',
               (scope_type, scope_key, _json(payload), _now()))
    return payload


def get_watch_policy(db, scope_type='global', scope_key=''):
    row = db.execute('''SELECT policy_json FROM media_watch_policies
        WHERE scope_type=? AND scope_key=?''',
                     (str(scope_type or 'global').casefold(), str(scope_key or '').strip())).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row['policy_json'] or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def effective_watch_policy(db, media_type='unknown', scopes=None):
    """Merge global, media-type, then specific entity policy overrides."""
    policy = dict(DEFAULT_WATCH_POLICY)
    policy.update(get_watch_policy(db, 'global', ''))
    policy.update(get_watch_policy(db, 'media_type', media_type or 'unknown'))
    for scope_type, scope_key in scopes or ():
        policy.update(get_watch_policy(db, scope_type, scope_key))
    return policy


def relationship_watch_action(policy, relationship_type, confidence=1.0):
    action = (policy or DEFAULT_WATCH_POLICY).get(relationship_type, 'discover')
    if action == 'auto_add' and float(confidence or 0) < 0.65:
        return 'discover'
    return action


def upsert_media_release(db, entity_id, release_type='publication', release_date=None,
                         region=None, platform=None, status=None, provider='',
                         provider_release_id=None, source_url=None, metadata=None,
                         observed_at=None):
    """Save a provider release observation without discarding prior dates."""
    provider = str(provider or 'unknown').strip()
    release_type = str(release_type or 'publication').strip().casefold()
    provider_release_id = str(provider_release_id or '').strip()
    if not provider_release_id:
        provider_release_id = _digest([int(entity_id), provider, release_type,
                                       release_date, region, platform])
    when = observed_at or _now()
    old = db.execute('''SELECT * FROM media_releases WHERE entity_id=? AND provider=?
        AND provider_release_id=? AND release_type=?''',
                     (int(entity_id), provider, provider_release_id, release_type)).fetchone()
    if old:
        old_date = old['release_date']
        next_date = release_date or old['release_date']
        next_region = region or old['region']
        next_platform = platform or old['platform']
        next_status = status or old['status']
        db.execute('''UPDATE media_releases SET release_date=?,region=?,platform=?,status=?,
            source_url=COALESCE(?,source_url),observed_at=?,metadata_json=? WHERE id=?''',
                   (next_date, next_region, next_platform, next_status, source_url, when,
                    _json(metadata or {}), old['id']))
        changed = any((old[field] != value) for field, value in (
            ('release_date', next_date), ('region', next_region), ('platform', next_platform),
            ('status', next_status)))
        if old_date and next_date and old_date != next_date:
            record_media_claim(db, entity_id, f'release_date:{provider_release_id}', next_date,
                               provider, source_url, _claim_confidence(provider), False, when)
        _queue_release_date_reconciliation(
            db, int(entity_id), release_type, next_region, next_platform,
            provider_release_id=provider_release_id, metadata=metadata or {})
        return {'id': int(old['id']), 'created': False, 'changed': changed}
    cursor = db.execute('''INSERT INTO media_releases
        (entity_id,release_type,release_date,region,platform,status,provider,
         provider_release_id,source_url,observed_at,metadata_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                        (int(entity_id), release_type, release_date, region, platform,
                         status, provider, provider_release_id, source_url, when,
                         _json(metadata or {})))
    if release_date:
        record_media_claim(db, entity_id, f'release_date:{provider_release_id}', release_date,
                           provider, source_url, _claim_confidence(provider), False, when)
    _queue_release_date_reconciliation(
        db, int(entity_id), release_type, region, platform,
        provider_release_id=provider_release_id, metadata=metadata or {})
    return {'id': int(cursor.lastrowid), 'created': True, 'changed': True}


def list_media_changes(db, root_entity_id, limit=200):
    rows = db.execute('''SELECT * FROM media_graph_changes WHERE root_entity_id=?
        ORDER BY detected_at DESC,id DESC LIMIT ?''',
                      (int(root_entity_id), max(1, min(int(limit), 1000)))).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ('before_json', 'after_json'):
            value = item.pop(key)
            item[key.removesuffix('_json')] = json.loads(value) if value else None
        result.append(item)
    return result


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


def _reconcile_lifecycle_status_consensus(db, entity_id, claims):
    """Apply a strict, confidence-weighted majority of independent catalog statuses."""
    if len(claims) < 3:
        return None
    groups = {}
    for claim in claims:
        value = claim.get('value')
        if not isinstance(value, str) or not value.strip():
            continue
        bucket = groups.setdefault(value, {'claims': [], 'score': 0.0})
        bucket['claims'].append(claim)
        bucket['score'] += float(claim.get('confidence') or 0)
    ranked = sorted(groups.items(), key=lambda item: (
        -len(item[1]['claims']), -item[1]['score'], item[0].casefold()))
    if not ranked:
        return None
    status, winner = ranked[0]
    other_provider_count = sum(len(group['claims']) for _value, group in ranked[1:])
    other_score = max((group['score'] for _value, group in ranked[1:]), default=0.0)
    if (len(winner['claims']) < 2 or
            len(winner['claims']) <= other_provider_count or
            winner['score'] <= other_score):
        return None

    entity = db.execute('SELECT status,metadata_json FROM media_entities WHERE id=?',
                        (int(entity_id),)).fetchone()
    if not entity:
        return None
    metadata = json.loads(entity['metadata_json'] or '{}')
    if metadata.get('status_source') == 'review':
        return None
    field_sources = metadata.setdefault('_field_sources', {})
    if not isinstance(field_sources, dict):
        field_sources = metadata['_field_sources'] = {}
    current_source = str(field_sources.get('status') or '').casefold()
    if any(token in current_source for token in ('manual', 'media intake review')):
        return None

    supporting = winner['claims']
    selected = sorted(supporting, key=lambda claim: (
        -float(claim.get('confidence') or 0), str(claim.get('provider') or '')))[0]
    claim_id = record_media_claim(
        db, int(entity_id), 'status', status, selected['provider'],
        source_url=selected.get('source_url'), confidence=selected.get('confidence') or 0.65,
        is_canonical=True)
    supporting_providers = sorted({str(claim.get('provider') or '') for claim in supporting})
    metadata['status_source'] = 'provider_consensus'
    metadata['canonical_status_observation'] = {
        'value': status, 'provider': selected.get('provider'),
        'source_url': selected.get('source_url'), 'claim_id': int(claim_id),
        'supporting_providers': supporting_providers,
        'supporting_claim_ids': sorted(int(claim['id']) for claim in supporting),
        'reconciled_at': _now(),
    }
    field_sources['status'] = selected.get('provider') or 'unknown'
    db.execute('UPDATE media_entities SET status=?,metadata_json=?,updated_at=? WHERE id=?',
               (status, _json(metadata), _now(), int(entity_id)))
    for watch in db.execute('SELECT id,metadata FROM watched_items WHERE media_entity_id=?',
                            (int(entity_id),)).fetchall():
        watch_metadata = json.loads(watch['metadata'] or '{}')
        if watch_metadata.get('catalog_status_source') == 'review':
            continue
        watch_metadata['catalog_status'] = status
        watch_metadata['catalog_status_source'] = 'provider_consensus'
        db.execute('UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (_json(watch_metadata), watch['id']))
    return {'status': status, 'supporting_providers': supporting_providers,
            'supporting_claim_ids': sorted(int(claim['id']) for claim in supporting),
            'selected_claim_id': int(claim_id)}


def _queue_provider_claim_conflicts(db, entity_id, field_name):
    """Surface current cross-provider date/status disagreements for review."""
    rows = db.execute('''SELECT id,claim_key,provider,value_json,source_url,observed_at,confidence
        FROM media_claim_observations WHERE entity_id=? AND field_name=?
        ORDER BY observed_at DESC,id DESC''', (int(entity_id), str(field_name))).fetchall()
    latest_by_provider = {}
    for row in rows:
        provider = str(row['provider'] or 'unknown').casefold()
        if provider in latest_by_provider or float(row['confidence'] or 0) < 0.65:
            continue
        try:
            value = json.loads(row['value_json'])
        except (TypeError, ValueError):
            continue
        latest_by_provider[provider] = {
            'id': int(row['id']), 'claim_key': row['claim_key'],
            'provider': provider, 'value': value, 'source_url': row['source_url'],
            'observed_at': row['observed_at'], 'confidence': float(row['confidence'] or 0),
        }
    distinct = {_json(value['value']) for value in latest_by_provider.values()}
    if len(latest_by_provider) < 2 or len(distinct) < 2:
        return None
    evidence = sorted(latest_by_provider.values(), key=lambda value: value['provider'])
    title = f'Conflicting {field_name.replace("_", " ")} metadata from connected catalogs'
    detail = {'field': field_name, 'claims': evidence}
    signature = [{key: value[key] for key in ('provider', 'value', 'source_url', 'confidence')}
                 for value in evidence]
    dedupe_key = _digest({'issue_type': 'conflicting_metadata',
                          'entity_id': int(entity_id), 'field': field_name,
                          'claims': signature})
    prior = db.execute('SELECT id,status FROM media_review_queue WHERE dedupe_key=?',
                       (dedupe_key,)).fetchone()
    consensus = (_reconcile_lifecycle_status_consensus(db, int(entity_id), evidence)
                 if field_name == 'status' else None)
    if consensus:
        review_id = (int(prior['id']) if prior else queue_graph_review(
            db, 'conflicting_metadata', title, detail,
            entity_id=int(entity_id), dedupe_key=dedupe_key))
        resolution = {'method': 'strict provider majority', **consensus,
                      'reconciled_at': _now()}
        for row in db.execute('''SELECT id,detail_json FROM media_review_queue
            WHERE entity_id=? AND issue_type='conflicting_metadata' AND status='open' ''',
                              (int(entity_id),)).fetchall():
            review_detail = json.loads(row['detail_json'] or '{}')
            if review_detail.get('field') != 'status':
                continue
            review_detail['automatic_resolution'] = resolution
            db.execute('''UPDATE media_review_queue SET status='accepted',detail_json=?,updated_at=?
                WHERE id=? AND status='open' ''', (_json(review_detail), _now(), row['id']))
        capture_graph_snapshot(db, int(entity_id))
        return review_id
    if prior:
        return int(prior['id'])
    return queue_graph_review(db, 'conflicting_metadata', title, detail,
                              entity_id=int(entity_id), dedupe_key=dedupe_key)


def record_catalog_metadata_variants(db, entity_id, variants):
    """Attach merged book-catalog member observations to the graph and review conflicts."""
    row = db.execute('SELECT release_date,status,metadata_json FROM media_entities WHERE id=?',
                     (int(entity_id),)).fetchone()
    if not row:
        raise ValueError(f'Unknown media entity ID: {entity_id}')
    metadata = json.loads(row['metadata_json'] or '{}')
    canonical = {
        'release_date': row['release_date'], 'status': row['status'],
        'series_index': metadata.get('series_index'),
    }
    for variant in variants or []:
        if not isinstance(variant, dict):
            continue
        provider = str(variant.get('provider') or 'Catalog')
        source_url = variant.get('source_url')
        claims = {
            'release_date': variant.get('release_date'),
            'status': variant.get('publication_status'),
            'series_index': variant.get('series_index'),
        }
        for field_name, value in claims.items():
            if value is None or value == '':
                continue
            is_canonical = value == canonical[field_name]
            record_media_claim(
                db, entity_id, field_name, value, provider, source_url=source_url,
                confidence=_claim_confidence(provider), is_canonical=is_canonical)
    for field_name in ('release_date', 'status', 'series_index'):
        _queue_provider_claim_conflicts(db, entity_id, field_name)


def _queue_release_date_reconciliation(db, entity_id, release_type, region, platform,
                                      provider_release_id=None, metadata=None):
    """Review conflicting game/video dates for the same type, region, and platform."""
    reconciled_types = {
        'game_launch', 'platform_launch', 'premiere', 'limited_theatrical',
        'theatrical', 'digital', 'physical', 'television',
        'video_premiere', 'video_limited_theatrical', 'video_theatrical',
        'video_digital', 'video_physical', 'video_television', 'episode',
    }
    if release_type not in reconciled_types:
        return None
    normalize = lambda value: ' '.join(str(value or '').casefold().split())
    episode_position = None
    if release_type == 'episode':
        current = metadata or {}
        try:
            season_number = int(current.get('season_number'))
            episode_number = int(current.get('episode_number'))
        except (TypeError, ValueError, OverflowError):
            return None
        if season_number < 0 or episode_number <= 0:
            return None
        episode_position = (season_number, episode_number)
    rows = db.execute('''SELECT id,provider,provider_release_id,release_date,region,platform,
        status,source_url,observed_at,metadata_json FROM media_releases
        WHERE entity_id=? AND release_type=?
        AND release_date IS NOT NULL ORDER BY observed_at DESC,id DESC''',
                      (int(entity_id), release_type)).fetchall()
    matching = []
    for row in rows:
        value = dict(row)
        if normalize(value['region']) != normalize(region) or normalize(value['platform']) != normalize(platform):
            continue
        if episode_position:
            try:
                row_position = (int(json.loads(value.get('metadata_json') or '{}').get('season_number')),
                                int(json.loads(value.get('metadata_json') or '{}').get('episode_number')))
            except (TypeError, ValueError, OverflowError):
                continue
            if row_position != episode_position:
                continue
        matching.append(value)
    by_provider = {}
    for row in matching:
        provider = str(row.get('provider') or 'unknown').casefold()
        by_provider.setdefault(provider, []).append(row)
    provider_ambiguity = any(len({str(row['release_date']) for row in values}) > 1
                             for values in by_provider.values())
    if len(by_provider) < 2 and not provider_ambiguity:
        return None
    provider_dates = {provider: {str(row['release_date']) for row in values}
                      for provider, values in by_provider.items()}
    if len({date for values in provider_dates.values() for date in values}) < 2:
        return None
    ambiguous = any(len(values) > 1 for values in provider_dates.values())
    evidence = sorted(matching, key=lambda row: (
        str(row['provider']).casefold(), str(row['release_date']),
        str(row.get('provider_release_id') or '')))
    issue_type = 'ambiguous_release_dates' if ambiguous else 'conflicting_release_dates'
    if release_type == 'episode':
        issue_type = 'ambiguous_episode_dates' if ambiguous else 'conflicting_episode_dates'
    region_text = str(region or 'unspecified region')
    release_label = {
        'game_launch': 'game launch', 'platform_launch': f'{platform or "platform"} release',
        'premiere': 'premiere', 'limited_theatrical': 'limited theatrical',
        'theatrical': 'theatrical', 'digital': 'digital availability',
        'physical': 'physical release', 'television': 'television availability',
    }.get(release_type.removeprefix('video_'), release_type.replace('_', ' '))
    if episode_position:
        episode_meta = metadata or {}
        episode_code = f'S{episode_position[0]:02d}E{episode_position[1]:02d}'
        episode_title = str(episode_meta.get('title') or '').strip()
        release_label = f"episode {episode_code}" + (f' — {episode_title}' if episode_title else '')
    title = (f'Multiple {release_label} dates listed for {region_text}' if ambiguous else
             f'Catalogs disagree on the {release_label} date for {region_text}')
    signature = []
    for row in evidence:
        claim = {key: row.get(key) for key in (
            'provider', 'provider_release_id', 'release_date')}
        claim['region'] = normalize(row.get('region'))
        claim['platform'] = normalize(row.get('platform'))
        signature.append(claim)
    dedupe_key = _digest({'issue_type': issue_type, 'entity_id': int(entity_id),
                          'release_type': release_type, 'region': normalize(region),
                          'platform': normalize(platform), 'episode_position': episode_position,
                          'claims': signature})
    if db.execute('SELECT 1 FROM media_review_queue WHERE dedupe_key=?',
                  (dedupe_key,)).fetchone():
        return None
    return queue_graph_review(
        db, issue_type, title,
        {'release_type': release_type, 'region': region, 'platform': platform,
         'season_number': episode_position[0] if episode_position else None,
         'episode_number': episode_position[1] if episode_position else None,
         'episode_title': (metadata or {}).get('title') if episode_position else None,
         'claims': evidence}, entity_id=int(entity_id), dedupe_key=dedupe_key)


def list_graph_reviews(db, status='open'):
    if status is not None and status not in _REVIEW_STATUSES:
        raise ValueError(f'Unsupported review status: {status}')
    rows = db.execute('''SELECT * FROM media_review_queue
        WHERE (? IS NULL OR status=?) ORDER BY created_at,id''', (status, status)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['detail'] = json.loads(item.pop('detail_json') or '{}')
        if (item.get('issue_type') == 'conflicting_metadata' and
                item['detail'].get('field') and item.get('entity_id')):
            for claim in item['detail'].get('claims') or []:
                if claim.get('id') is not None:
                    continue
                candidate = db.execute('''SELECT id,claim_key FROM media_claim_observations
                    WHERE entity_id=? AND field_name=? AND provider=? AND value_json=?
                    AND source_url IS ? ORDER BY observed_at DESC,id DESC LIMIT 1''',
                    (int(item['entity_id']), item['detail']['field'],
                     str(claim.get('provider') or '').casefold(), _json(claim.get('value')),
                     claim.get('source_url'))).fetchone()
                if candidate:
                    claim['id'] = int(candidate['id'])
                    claim['claim_key'] = candidate['claim_key']
        result.append(item)
    return result


def resolve_graph_review(db, review_id, status):
    if status not in {'accepted', 'rejected', 'dismissed'}:
        raise ValueError('Review resolution must be accepted, rejected, or dismissed')
    review = db.execute('''SELECT relationship_id,entity_id,issue_type,detail_json FROM media_review_queue
        WHERE id=? AND status='open' ''', (int(review_id),)).fetchone()
    if not review:
        raise ValueError(f'No open media graph review item: {review_id}')
    snapshot_entity_id = None
    if status == 'accepted' and review['issue_type'] == 'uncertain_series_member':
        detail = json.loads(review['detail_json'] or '{}')
        candidate = detail.get('candidate') or {}
        parent = db.execute('''SELECT * FROM watched_items
            WHERE media_entity_id=? AND kind='series' ORDER BY id LIMIT 1''',
                            (review['entity_id'],)).fetchone()
        if not parent:
            raise ValueError('Accepting this series member requires its parent series watch')
        parent_metadata = json.loads(parent['metadata'] or '{}')
        candidate_catalog = candidate.get('catalog_metadata') or {}
        provider = (candidate.get('provider') or candidate_catalog.get('catalog_provider') or
                    parent_metadata.get('catalog_provider') or 'Catalog')
        from .watched import _add_catalog_book_watch
        member_id, _created = _add_catalog_book_watch(
            db, candidate, candidate.get('author') or parent['author'] or 'Unknown',
            parent_metadata.get('sources') or {}, int(parent['id']), provider,
            force_add=True)
        if member_id is None:
            raise ValueError('The selected series member has no usable title')
    if status == 'accepted' and review['issue_type'] == 'possible_feed_episode':
        detail = json.loads(review['detail_json'] or '{}')
        from .watched import _accept_video_feed_episode_review
        _accept_video_feed_episode_review(db, int(review['entity_id']), detail)
        snapshot_entity_id = int(review['entity_id'])
    if status == 'accepted' and review['issue_type'] == 'release_announcement':
        detail = json.loads(review['detail_json'] or '{}')
        event = detail.get('typed_event_candidate') or {}
        event_type = str(event.get('event_type') or '')
        if event_type == 'episode_announcement' and detail.get('media_type') == 'video':
            from .watched import _accept_video_feed_episode_review
            episode_detail = dict(detail)
            episode_detail.update({
                'feed_entry_key': detail.get('provider_entry_id'),
                'feed_entry_type': 'episode',
                'release_date': event.get('announced_date') or detail.get('schedule_date'),
            })
            provider_metadata = detail.get('provider_metadata')
            provider_metadata = dict(provider_metadata) if isinstance(provider_metadata, dict) else {}
            provider_metadata.setdefault('provider', 'ical')
            if detail.get('summary'):
                provider_metadata.setdefault('summary', detail['summary'])
            episode_detail['provider_metadata'] = provider_metadata
            _accept_video_feed_episode_review(db, int(review['entity_id']), episode_detail)
            snapshot_entity_id = int(review['entity_id'])
        if event_type in _ANNOUNCEMENT_EVENT_TYPES:
            snapshot_entity_id = int(review['entity_id'])
            upsert_media_release(
                db, int(review['entity_id']), event_type, None, status='announced',
                provider=str(detail.get('provider') or 'announcement_feed'),
                provider_release_id=str(detail.get('provider_entry_id') or
                                        f'review:{int(review_id)}'),
                source_url=detail.get('source_url'),
                metadata={'title': detail.get('title'), 'feed_url': detail.get('feed_url'),
                          'feed_title': detail.get('feed_title'),
                          'summary': detail.get('summary'),
                          'schedule_date': detail.get('schedule_date'),
                          'provider_metadata': detail.get('provider_metadata'),
                          'announcement_date': detail.get('announcement_date'),
                          'typed_event_candidate': event})
            lifecycle_status = {
                'series_renewal': 'ongoing',
                'series_cancellation': 'cancelled',
                'series_hiatus': 'hiatus',
            }.get(event_type)
            if lifecycle_status:
                entity = db.execute('SELECT entity_type,metadata_json FROM media_entities WHERE id=?',
                                    (int(review['entity_id']),)).fetchone()
                if entity and entity['entity_type'] in {'series', 'franchise'}:
                    entity_metadata = json.loads(entity['metadata_json'] or '{}')
                    observation = {
                        'value': lifecycle_status, 'event_type': event_type,
                        'provider': str(detail.get('provider') or 'announcement_feed'),
                        'source_url': detail.get('source_url'), 'selected_at': _now(),
                    }
                    entity_metadata['status_source'] = 'review'
                    entity_metadata['canonical_status_observation'] = observation
                    field_sources = entity_metadata.setdefault('_field_sources', {})
                    if not isinstance(field_sources, dict):
                        field_sources = entity_metadata['_field_sources'] = {}
                    field_sources['status'] = 'Media Intake review'
                    db.execute('''UPDATE media_entities SET status=?,metadata_json=?,updated_at=?
                        WHERE id=?''', (lifecycle_status, _json(entity_metadata), _now(),
                                        int(review['entity_id'])))
                    record_media_claim(
                        db, int(review['entity_id']), 'status', lifecycle_status,
                        'Media Intake review', source_url=detail.get('source_url'),
                        confidence=1.0, is_canonical=True)
                    for watch in db.execute('''SELECT id,metadata FROM watched_items
                        WHERE media_entity_id=?''', (int(review['entity_id']),)).fetchall():
                        watch_metadata = json.loads(watch['metadata'] or '{}')
                        watch_metadata['catalog_status'] = lifecycle_status
                        watch_metadata['catalog_status_source'] = 'review'
                        db.execute('''UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP
                            WHERE id=?''', (_json(watch_metadata), watch['id']))
            announced_date = event.get('announced_date')
            if event.get('announced_date_precision') == 'day' and announced_date:
                try:
                    parsed_date = datetime.strptime(str(announced_date), '%Y-%m-%d').date()
                except ValueError:
                    parsed_date = None
                release_context = str(event.get('announced_date_context') or '')
                media_type = str(detail.get('media_type') or '').casefold()
                release_type = None
                if release_context == 'premiere':
                    release_type = 'game_launch' if media_type == 'game' else 'video_premiere'
                elif release_context == 'availability':
                    release_type = ('game_availability' if media_type == 'game' else
                                    'video_streaming' if event.get('announced_platform') else
                                    'video_availability')
                elif release_context == 'release':
                    release_type = 'game_launch' if media_type == 'game' else 'video_premiere'
                if parsed_date and release_type:
                    source_provider = str(detail.get('provider') or 'announcement_feed')
                    entry_id = str(detail.get('provider_entry_id') or f'review:{int(review_id)}')
                    release = upsert_media_release(
                        db, int(review['entity_id']), release_type, parsed_date.isoformat(),
                        platform=event.get('announced_platform'),
                        status=('upcoming' if parsed_date > datetime.now(timezone.utc).date()
                                else 'released'), provider=source_provider,
                        provider_release_id=f'{entry_id}:{release_type}',
                        source_url=detail.get('source_url'),
                        metadata={'title': detail.get('title'), 'feed_url': detail.get('feed_url'),
                                  'feed_title': detail.get('feed_title'),
                                  'summary': detail.get('summary'),
                                  'schedule_date': detail.get('schedule_date'),
                                  'provider_metadata': detail.get('provider_metadata'),
                                  'typed_event_candidate': event,
                                  'announced_date_source': event.get('announced_date_source')})
                    primary_types = {'game_launch', 'video_premiere'}
                    if release_type in primary_types:
                        entity = db.execute('SELECT release_date,metadata_json FROM media_entities WHERE id=?',
                                            (int(review['entity_id']),)).fetchone()
                        entity_metadata = json.loads(entity['metadata_json'] or '{}') if entity else {}
                        release_date_source = str(entity_metadata.get('release_date_source') or '').casefold()
                        if (entity and not entity['release_date'] and
                                release_date_source not in {'manual', 'review'}):
                            release_row = db.execute('SELECT * FROM media_releases WHERE id=?',
                                                     (release['id'],)).fetchone()
                            selection = {
                                'release_id': int(release['id']), 'release_type': release_type,
                                'release_date': parsed_date.isoformat(), 'region': None,
                                'platform': None, 'provider': source_provider,
                                'provider_release_id': release_row['provider_release_id'],
                                'source_url': detail.get('source_url'),
                                'selected_at': _now(),
                            }
                            db.execute('''UPDATE media_releases SET is_canonical=0
                                WHERE entity_id=? AND release_type=? AND region IS NULL
                                AND platform IS NULL AND is_canonical=1''',
                                       (int(review['entity_id']), release_type))
                            db.execute('UPDATE media_releases SET is_canonical=1 WHERE id=?',
                                       (release['id'],))
                            entity_metadata['release_date_source'] = 'review'
                            entity_metadata['canonical_release_date_observation'] = selection
                            field_sources = entity_metadata.setdefault('_field_sources', {})
                            if not isinstance(field_sources, dict):
                                field_sources = entity_metadata['_field_sources'] = {}
                            field_sources['release_date'] = 'Media Intake review'
                            db.execute('''UPDATE media_entities SET release_date=?,metadata_json=?,updated_at=?
                                WHERE id=?''', (parsed_date.isoformat(), _json(entity_metadata), _now(),
                                                 int(review['entity_id'])))
                            record_media_claim(
                                db, int(review['entity_id']), 'release_date', parsed_date.isoformat(),
                                'Media Intake review', source_url=detail.get('source_url'),
                                confidence=1.0, is_canonical=True)
                            today = datetime.now(timezone.utc).date().isoformat()
                            next_state = 'upcoming' if parsed_date.isoformat() > today else 'watching'
                            next_check = (f'{parsed_date.isoformat()}T00:00:00'
                                          if next_state == 'upcoming' else _now())
                            for watch in db.execute('''SELECT id,metadata FROM watched_items
                                WHERE media_entity_id=? AND kind IN ('video','game')''',
                                                    (int(review['entity_id']),)).fetchall():
                                watch_metadata = json.loads(watch['metadata'] or '{}')
                                if watch_metadata.get('release_date_source') in {'manual', 'review'}:
                                    continue
                                watch_metadata['release_date_source'] = 'review'
                                watch_metadata['canonical_release_date'] = selection
                                db.execute('''UPDATE watched_items SET release_date=?,metadata=?,state=?,
                                    next_check_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                                           (parsed_date.isoformat(), _json(watch_metadata), next_state,
                                            next_check, watch['id']))
                        elif (entity and entity['release_date'] != parsed_date.isoformat() and
                              release_date_source not in {'manual', 'review'}):
                            record_media_claim(
                                db, int(review['entity_id']), 'release_date',
                                parsed_date.isoformat(), source_provider,
                                source_url=detail.get('source_url'), confidence=0.65,
                                is_canonical=False)
                            _queue_provider_claim_conflicts(
                                db, int(review['entity_id']), 'release_date')
    relationship_id = review['relationship_id']
    if relationship_id and status in {'accepted', 'rejected'}:
        relationship_status = 'accepted' if status == 'accepted' else 'ignored'
        db.execute('''UPDATE media_relationships SET status=?,updated_at=? WHERE id=?''',
                   (relationship_status, _now(), relationship_id))
    cursor = db.execute('''UPDATE media_review_queue SET status=?,updated_at=?
        WHERE id=? AND status='open' ''', (status, _now(), int(review_id)))
    if cursor.rowcount == 0:
        raise ValueError(f'No open media graph review item: {review_id}')
    if snapshot_entity_id is not None:
        capture_graph_snapshot(db, snapshot_entity_id)
    return True


def _update_watch_queue_metadata(db, watch_id, values, search_state=None):
    """Keep active and completed queue records aligned with reviewed watch metadata."""
    watch_id = int(watch_id)
    for row in db.execute('SELECT id,url,metadata,state,error FROM items').fetchall():
        try:
            metadata = json.loads(row['metadata'] or '{}')
        except (TypeError, ValueError):
            continue
        is_search = (metadata.get('watch_search') and
                     str(metadata.get('watch_id')) == str(watch_id))
        is_download = str(metadata.get('watch_request_id')) == str(watch_id)
        if not (is_search or is_download):
            continue
        metadata.update(values)
        state = search_state if is_search and search_state else row['state']
        error = None if is_search and search_state else row['error']
        db.execute('''UPDATE items SET metadata=?,state=?,error=?,updated_at=CURRENT_TIMESTAMP
            WHERE id=?''', (_json(metadata), state, error, row['id']))


def set_reviewed_media_status(db, review_id, claim_id):
    """Apply one provider's reviewed lifecycle status without losing other claims."""
    review = db.execute('''SELECT entity_id,issue_type,detail_json FROM media_review_queue
        WHERE id=? AND status='open' ''', (int(review_id),)).fetchone()
    if not review or review['issue_type'] != 'conflicting_metadata':
        raise ValueError('Select an open lifecycle metadata disagreement first')
    detail = json.loads(review['detail_json'] or '{}')
    if detail.get('field') != 'status':
        raise ValueError('This review item does not contain a lifecycle status disagreement')
    allowed_ids = {int(item['id']) for item in detail.get('claims', [])
                   if item.get('id') is not None}
    if int(claim_id) not in allowed_ids:
        raise ValueError('The selected status observation is not part of this review item')
    claim = db.execute('''SELECT * FROM media_claim_observations WHERE id=? AND entity_id=?
        AND field_name='status' ''', (int(claim_id), int(review['entity_id']))).fetchone()
    if not claim:
        raise ValueError('The selected status observation no longer exists')
    try:
        value = json.loads(claim['value_json'])
    except (TypeError, ValueError) as exc:
        raise ValueError('The selected status observation is malformed') from exc
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ValueError('The selected observation is not a usable lifecycle status')

    db.execute('''UPDATE media_claim_observations SET is_canonical=0
        WHERE entity_id=? AND field_name='status' AND is_canonical=1''',
               (int(review['entity_id']),))
    db.execute('UPDATE media_claim_observations SET is_canonical=1 WHERE id=?',
               (int(claim_id),))
    entity = db.execute('SELECT metadata_json FROM media_entities WHERE id=?',
                        (int(review['entity_id']),)).fetchone()
    entity_metadata = json.loads(entity['metadata_json'] or '{}') if entity else {}
    entity_metadata['status_source'] = 'review'
    entity_metadata['canonical_status_observation'] = {
        'claim_id': int(claim['id']), 'claim_key': claim['claim_key'],
        'value': value, 'provider': claim['provider'], 'source_url': claim['source_url'],
        'selected_at': _now(),
    }
    field_sources = entity_metadata.setdefault('_field_sources', {})
    if isinstance(field_sources, dict):
        field_sources['status'] = 'Media Intake review'
    db.execute('UPDATE media_entities SET status=?,metadata_json=?,updated_at=? WHERE id=?',
               (value, _json(entity_metadata), _now(), int(review['entity_id'])))

    for watch in db.execute('SELECT id,metadata FROM watched_items WHERE media_entity_id=?',
                            (int(review['entity_id']),)).fetchall():
        metadata = json.loads(watch['metadata'] or '{}')
        metadata['catalog_status'] = value
        metadata['catalog_status_source'] = 'review'
        db.execute('UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (_json(metadata), watch['id']))
    resolve_graph_review(db, review_id, 'accepted')
    capture_graph_snapshot(db, int(review['entity_id']))
    return {'status': value, 'provider': claim['provider'], 'source_url': claim['source_url']}


def set_reviewed_media_series_index(db, review_id, claim_id):
    """Choose a provider's series position while retaining every observed claim."""
    review = db.execute('''SELECT entity_id,issue_type,detail_json FROM media_review_queue
        WHERE id=? AND status='open' ''', (int(review_id),)).fetchone()
    if not review or review['issue_type'] != 'conflicting_metadata':
        raise ValueError('Select an open series-position disagreement first')
    detail = json.loads(review['detail_json'] or '{}')
    if detail.get('field') != 'series_index':
        raise ValueError('This review item does not contain a series-position disagreement')
    allowed_ids = {int(item['id']) for item in detail.get('claims', [])
                   if item.get('id') is not None}
    if int(claim_id) not in allowed_ids:
        raise ValueError('The selected position observation is not part of this review item')
    claim = db.execute('''SELECT * FROM media_claim_observations WHERE id=? AND entity_id=?
        AND field_name='series_index' ''', (int(claim_id), int(review['entity_id']))).fetchone()
    if not claim:
        raise ValueError('The selected series-position observation no longer exists')
    try:
        raw_value = json.loads(claim['value_json'])
        if isinstance(raw_value, bool):
            raise ValueError
        position = float(raw_value)
        if not math.isfinite(position) or position < 0:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('The selected observation is not a usable series position') from exc
    value = int(position) if position.is_integer() else position

    db.execute('''UPDATE media_claim_observations SET is_canonical=0
        WHERE entity_id=? AND field_name='series_index' AND is_canonical=1''',
               (int(review['entity_id']),))
    db.execute('UPDATE media_claim_observations SET is_canonical=1 WHERE id=?',
               (int(claim_id),))
    selection = {
        'claim_id': int(claim['id']), 'claim_key': claim['claim_key'],
        'series_index': value, 'provider': claim['provider'],
        'source_url': claim['source_url'], 'selected_at': _now(),
    }
    entity = db.execute('SELECT metadata_json FROM media_entities WHERE id=?',
                        (int(review['entity_id']),)).fetchone()
    entity_metadata = json.loads(entity['metadata_json'] or '{}') if entity else {}
    entity_metadata['series_index'] = value
    entity_metadata['series_position'] = str(value)
    entity_metadata['series_position_source'] = 'Media Intake review'
    entity_metadata['canonical_series_position_observation'] = selection
    field_sources = entity_metadata.setdefault('_field_sources', {})
    if isinstance(field_sources, dict):
        field_sources['series_index'] = 'Media Intake review'
    db.execute('UPDATE media_entities SET metadata_json=?,updated_at=? WHERE id=?',
               (_json(entity_metadata), _now(), int(review['entity_id'])))

    watches = db.execute('''SELECT id,parent_id,metadata FROM watched_items
        WHERE media_entity_id=?''', (int(review['entity_id']),)).fetchall()
    for watch in watches:
        metadata = json.loads(watch['metadata'] or '{}')
        metadata['series_index'] = value
        metadata['series_position'] = str(value)
        metadata['series_position_source'] = 'Media Intake review'
        metadata['canonical_series_position_observation'] = selection
        db.execute('UPDATE watched_items SET metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                   (_json(metadata), watch['id']))
        _update_watch_queue_metadata(db, watch['id'], {
            'series_index': value, 'series_position': str(value),
            'series_position_source': 'Media Intake review',
        })
        if not watch['parent_id']:
            continue
        parent = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                            (int(watch['parent_id']),)).fetchone()
        if not parent or not parent['media_entity_id']:
            continue
        edges = db.execute('''SELECT id,source_entity_id,target_entity_id,
            relationship_type,metadata_json FROM media_relationships
            WHERE (source_entity_id=? AND target_entity_id=? AND
                   relationship_type IN ('part_of','parent_series'))
               OR (source_entity_id=? AND target_entity_id=? AND
                   relationship_type IN ('contains','includes_work'))''',
                           (int(review['entity_id']), int(parent['media_entity_id']),
                            int(parent['media_entity_id']), int(review['entity_id']))).fetchall()
        for edge in edges:
            edge_metadata = json.loads(edge['metadata_json'] or '{}')
            edge_metadata.update({
                'series_index': value, 'series_position': str(value),
                'series_position_source': 'Media Intake review',
                'canonical_series_position_observation': selection,
            })
            db.execute('UPDATE media_relationships SET metadata_json=?,updated_at=? WHERE id=?',
                       (_json(edge_metadata), _now(), edge['id']))

    resolve_graph_review(db, review_id, 'accepted')
    capture_graph_snapshot(db, int(review['entity_id']))
    return {'series_index': value, 'provider': claim['provider'],
            'source_url': claim['source_url']}


def set_reviewed_media_release_date(db, review_id, claim_id):
    """Apply one provider's reviewed primary release date and reschedule its watch."""
    review = db.execute('''SELECT entity_id,issue_type,detail_json FROM media_review_queue
        WHERE id=? AND status='open' ''', (int(review_id),)).fetchone()
    if not review or review['issue_type'] != 'conflicting_metadata':
        raise ValueError('Select an open release-date metadata disagreement first')
    detail = json.loads(review['detail_json'] or '{}')
    if detail.get('field') != 'release_date':
        raise ValueError('This review item does not contain a primary release-date disagreement')
    allowed_ids = {int(item['id']) for item in detail.get('claims', [])
                   if item.get('id') is not None}
    if int(claim_id) not in allowed_ids:
        raise ValueError('The selected date observation is not part of this review item')
    claim = db.execute('''SELECT * FROM media_claim_observations WHERE id=? AND entity_id=?
        AND field_name='release_date' ''', (int(claim_id), int(review['entity_id']))).fetchone()
    if not claim:
        raise ValueError('The selected release-date observation no longer exists')
    try:
        value = json.loads(claim['value_json'])
        parsed_date = datetime.strptime(str(value), '%Y-%m-%d').date()
    except (TypeError, ValueError) as exc:
        raise ValueError('The selected observation is not a full release date') from exc
    value = parsed_date.isoformat()
    db.execute('''UPDATE media_claim_observations SET is_canonical=0
        WHERE entity_id=? AND field_name='release_date' AND is_canonical=1''',
               (int(review['entity_id']),))
    db.execute('UPDATE media_claim_observations SET is_canonical=1 WHERE id=?',
               (int(claim_id),))
    entity = db.execute('SELECT metadata_json FROM media_entities WHERE id=?',
                        (int(review['entity_id']),)).fetchone()
    entity_metadata = json.loads(entity['metadata_json'] or '{}') if entity else {}
    selection = {
        'claim_id': int(claim['id']), 'claim_key': claim['claim_key'],
        'release_date': value, 'provider': claim['provider'],
        'source_url': claim['source_url'], 'selected_at': _now(),
    }
    entity_metadata['release_date_source'] = 'review'
    entity_metadata['canonical_release_date_observation'] = selection
    field_sources = entity_metadata.setdefault('_field_sources', {})
    if not isinstance(field_sources, dict):
        field_sources = entity_metadata['_field_sources'] = {}
    field_sources['release_date'] = 'Media Intake review'
    db.execute('UPDATE media_entities SET release_date=?,metadata_json=?,updated_at=? WHERE id=?',
               (value, _json(entity_metadata), _now(), int(review['entity_id'])))

    matching = db.execute('''SELECT id,release_type,region,platform FROM media_releases
        WHERE entity_id=? AND lower(provider)=? AND release_date=? AND source_url IS ?''',
                          (int(review['entity_id']), str(claim['provider']).casefold(), value,
                           claim['source_url'])).fetchall()
    for release in matching:
        db.execute('''UPDATE media_releases SET is_canonical=0 WHERE entity_id=?
            AND release_type=? AND region IS ? AND platform IS ?''',
                   (int(review['entity_id']), release['release_type'],
                    release['region'], release['platform']))
        db.execute('UPDATE media_releases SET is_canonical=1 WHERE id=?', (release['id'],))

    today = datetime.now(timezone.utc).date().isoformat()
    state = 'upcoming' if value > today else 'watching'
    next_check = f'{value}T00:00:00' if state == 'upcoming' else _now()
    watches = db.execute('''SELECT id,metadata,state,next_check_at FROM watched_items
        WHERE media_entity_id=?''', (int(review['entity_id']),)).fetchall()
    for watch in watches:
        metadata = json.loads(watch['metadata'] or '{}')
        if metadata.get('release_date_source') == 'manual':
            continue
        metadata['release_date_source'] = 'review'
        metadata['canonical_release_date'] = selection
        watch_state = ('downloaded' if watch['state'] == 'downloaded' else state)
        watch_next_check = (watch['next_check_at'] if watch_state == 'downloaded' else next_check)
        db.execute('''UPDATE watched_items SET release_date=?,metadata=?,state=?,next_check_at=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                   (value, _json(metadata), watch_state, watch_next_check, watch['id']))
        if watch_state != 'downloaded':
            _update_watch_queue_metadata(
                db, watch['id'], {'release_date': value},
                search_state='upcoming' if state == 'upcoming' else 'searching')
        else:
            _update_watch_queue_metadata(db, watch['id'], {'release_date': value})
    resolve_graph_review(db, review_id, 'accepted')
    capture_graph_snapshot(db, int(review['entity_id']))
    return {'release_date': value, 'provider': claim['provider'],
            'source_url': claim['source_url']}


def set_reviewed_release_date(db, review_id, release_id):
    """Choose one observed date for a release window and retain its source record."""
    review = db.execute('''SELECT entity_id,issue_type,detail_json FROM media_review_queue
        WHERE id=? AND status='open' ''', (int(review_id),)).fetchone()
    if not review or review['issue_type'] not in {
            'conflicting_release_dates', 'ambiguous_release_dates',
            'conflicting_episode_dates', 'ambiguous_episode_dates'}:
        raise ValueError('Select an open release-date disagreement before choosing a date')
    detail = json.loads(review['detail_json'] or '{}')
    allowed_ids = {int(item['id']) for item in detail.get('claims', [])
                   if item.get('id') is not None}
    if int(release_id) not in allowed_ids:
        raise ValueError('The selected release observation is not part of this review item')
    release = db.execute('''SELECT * FROM media_releases WHERE id=? AND entity_id=?''',
                         (int(release_id), int(review['entity_id']))).fetchone()
    if not release or not release['release_date']:
        raise ValueError('The selected release observation no longer has a date')

    normalize = lambda value: ' '.join(str(value or '').casefold().split())
    release_metadata = json.loads(release['metadata_json'] or '{}')
    if (release['release_type'] != detail.get('release_type') or
            normalize(release['region']) != normalize(detail.get('region')) or
            normalize(release['platform']) != normalize(detail.get('platform'))):
        raise ValueError('The selected observation no longer matches the reviewed platform and region')
    episode_position = None
    if release['release_type'] == 'episode':
        try:
            episode_position = (int(release_metadata.get('season_number')),
                                int(release_metadata.get('episode_number')))
            reviewed_position = (int(detail.get('season_number')),
                                 int(detail.get('episode_number')))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError('The review item no longer has a usable episode position') from exc
        if episode_position != reviewed_position:
            raise ValueError('The selected observation is for a different episode position')

    matching_release_ids = []
    for row in db.execute('''SELECT id,region,platform,metadata_json
        FROM media_releases WHERE entity_id=? AND release_type=?''',
                          (int(review['entity_id']), release['release_type'])):
        if (normalize(row['region']) != normalize(release['region']) or
                normalize(row['platform']) != normalize(release['platform'])):
            continue
        if episode_position:
            try:
                row_metadata = json.loads(row['metadata_json'] or '{}')
                row_position = (int(row_metadata.get('season_number')),
                                int(row_metadata.get('episode_number')))
            except (TypeError, ValueError, OverflowError):
                continue
            if row_position != episode_position:
                continue
        matching_release_ids.append(int(row['id']))
    for matching_release_id in matching_release_ids:
        db.execute('UPDATE media_releases SET is_canonical=0 WHERE id=?',
                   (matching_release_id,))
    db.execute('UPDATE media_releases SET is_canonical=1 WHERE id=?', (int(release_id),))

    entity = db.execute('SELECT metadata_json FROM media_entities WHERE id=?',
                        (int(review['entity_id']),)).fetchone()
    entity_metadata = json.loads(entity['metadata_json'] or '{}') if entity else {}
    selection = {
        'release_id': int(release['id']), 'release_type': release['release_type'],
        'release_date': release['release_date'], 'region': release['region'],
        'platform': release['platform'], 'provider': release['provider'],
        'provider_release_id': release['provider_release_id'],
        'source_url': release['source_url'], 'selected_at': _now(),
    }
    canonical_dates = entity_metadata.setdefault('canonical_release_dates', {})
    if not isinstance(canonical_dates, dict):
        canonical_dates = entity_metadata['canonical_release_dates'] = {}
    group_key = _digest([release['release_type'], normalize(release['region']),
                         normalize(release['platform']), episode_position])
    canonical_dates[group_key] = selection
    primary_release_types = {'game_launch', 'platform_launch', 'premiere', 'theatrical',
                             'video_premiere', 'video_theatrical'}
    is_primary_date = release['release_type'] in primary_release_types
    if is_primary_date:
        entity_metadata['canonical_release_date_observation'] = selection
    field_sources = entity_metadata.setdefault('_field_sources', {})
    if is_primary_date and isinstance(field_sources, dict):
        field_sources['release_date'] = 'Media Intake review'
    if is_primary_date:
        db.execute('''UPDATE media_entities SET release_date=?,metadata_json=?,updated_at=? WHERE id=?''',
                   (release['release_date'], _json(entity_metadata), _now(), int(review['entity_id'])))
        record_media_claim(db, int(review['entity_id']), 'release_date', release['release_date'],
                           'Media Intake review', source_url=release['source_url'],
                           confidence=1.0, is_canonical=True)
    else:
        db.execute('UPDATE media_entities SET metadata_json=?,updated_at=? WHERE id=?',
                   (_json(entity_metadata), _now(), int(review['entity_id'])))

    now = _now()
    today = datetime.now(timezone.utc).date().isoformat()
    state = 'upcoming' if release['release_date'] > today else 'watching'
    next_check = (f"{release['release_date']}T00:00:00" if state == 'upcoming' else now)
    if is_primary_date:
        watches = db.execute('''SELECT id,metadata FROM watched_items WHERE media_entity_id=?
            AND kind IN ('video','game') ''', (int(review['entity_id']),)).fetchall()
        for watch in watches:
            metadata = json.loads(watch['metadata'] or '{}')
            metadata['release_date_source'] = 'review'
            metadata['canonical_release_date'] = selection
            db.execute('''UPDATE watched_items SET release_date=?,metadata=?,state=?,next_check_at=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                       (release['release_date'], _json(metadata), state, next_check, watch['id']))
    elif episode_position:
        stable_episode_id = (f"{int(review['entity_id'])}:S{episode_position[0]:02d}"
                             f"E{episode_position[1]:02d}")
        episode_watches = db.execute('''SELECT w.id,w.media_entity_id,w.metadata
            FROM media_external_ids x JOIN watched_items w ON w.media_entity_id=x.entity_id
            WHERE x.provider='media-intake-episode' AND x.external_id=?
              AND w.kind='video_episode' ''', (stable_episode_id,)).fetchall()
        for watch in episode_watches:
            watch_metadata = json.loads(watch['metadata'] or '{}')
            watch_metadata['release_date_source'] = 'review'
            watch_metadata['canonical_episode_release_date'] = selection
            db.execute('''UPDATE watched_items SET release_date=?,metadata=?,state=?,next_check_at=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                       (release['release_date'], _json(watch_metadata), state, next_check,
                        watch['id']))
            child_entity = db.execute('SELECT metadata_json FROM media_entities WHERE id=?',
                                      (watch['media_entity_id'],)).fetchone()
            if child_entity:
                child_metadata = json.loads(child_entity['metadata_json'] or '{}')
                child_metadata['canonical_release_date_observation'] = selection
                child_sources = child_metadata.setdefault('_field_sources', {})
                if isinstance(child_sources, dict):
                    child_sources['release_date'] = 'Media Intake review'
                db.execute('''UPDATE media_entities SET release_date=?,metadata_json=?,updated_at=?
                    WHERE id=?''', (release['release_date'], _json(child_metadata), _now(),
                                    watch['media_entity_id']))
                record_media_claim(db, int(watch['media_entity_id']), 'release_date',
                                   release['release_date'], 'Media Intake review',
                                   source_url=release['source_url'], confidence=1.0,
                                   is_canonical=True)
    resolve_graph_review(db, review_id, 'accepted')
    return selection


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
    generic_ids = list(metadata.get('external_ids') or [])
    if isinstance(catalog, dict):
        generic_ids.extend(catalog.get('external_ids') or [])
    catalog_ids = metadata.get('catalog_ids') or {}
    catalog_sources = metadata.get('catalog_sources') or {}
    if isinstance(catalog_ids, dict):
        for source_name, values in catalog_ids.items():
            ids = values if isinstance(values, list) else [values]
            urls = catalog_sources.get(source_name) or []
            urls = urls if isinstance(urls, list) else [urls]
            provider = str(source_name or '').strip().casefold().replace(' ', '')
            for index, external_id in enumerate(ids):
                if external_id:
                    generic_ids.append({
                        'provider': provider,
                        'external_id': str(external_id),
                        'source_url': urls[index] if index < len(urls) else None,
                    })
    for value in generic_ids:
        if not isinstance(value, dict):
            continue
        provider = str(value.get('provider') or '').strip().casefold()
        external_id = str(value.get('external_id') or '').strip()
        if provider and external_id and not any(
                old['provider'] == provider and old['external_id'] == external_id
                for old in result):
            result.append({'provider': provider, 'external_id': external_id,
                           'source_url': value.get('source_url') or metadata.get('source_url')})
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
    media_type = _media_type_for_watch(kind)
    linked_id = watch['media_entity_id'] if 'media_entity_id' in watch.keys() else None
    if linked_id and not db.execute('SELECT 1 FROM media_entities WHERE id=?', (linked_id,)).fetchone():
        linked_id = None
    preferred_id = linked_id
    entity_type = metadata.get('entity_type') or _entity_type_for_watch(kind)
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
        status=(None if metadata.get('catalog_status_source') in {'review', 'provider_consensus'} else
                metadata.get('catalog_status') or metadata.get('publication_status')),
        metadata={'watch_id': watch_id, 'watch_kind': kind},
        external_ids=[{'provider': 'media-intake', 'external_id': f'watch:{watch_id}'}],
        entity_id=preferred_id, provider=metadata.get('catalog_provider') or 'Media Intake')
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


def _watch_policy_has_override(db, media_type, scopes, policy_key):
    if policy_key in get_watch_policy(db, 'global', ''):
        return True
    if policy_key in get_watch_policy(db, 'media_type', media_type or 'unknown'):
        return True
    return any(policy_key in get_watch_policy(db, scope_type, scope_key)
               for scope_type, scope_key in scopes or ())


def _watch_parent_relationship_type(watch, parent):
    if parent['kind'] == 'author':
        return 'created_by'
    if watch['kind'] == 'video_episode':
        try:
            metadata = json.loads(watch['metadata'] or '{}')
        except (TypeError, ValueError):
            metadata = {}
        episode = metadata.get('episode') if isinstance(metadata.get('episode'), dict) else {}
        entry_type = str(episode.get('feed_entry_type') or '').casefold()
        if entry_type in {'trailer', 'announcement'}:
            return 'part_of'
        if str(metadata.get('video_subtype') or '').casefold() == 'special':
            return 'special_of'
        return 'episode_of'
    try:
        metadata = json.loads(watch['metadata'] or '{}')
    except (TypeError, ValueError):
        metadata = {}
    if str(metadata.get('entity_type') or '').casefold() == 'season':
        return 'season_of'
    return 'part_of'


def _sync_watch_parent(db, watch):
    if not watch['parent_id']:
        return None
    parent = db.execute('SELECT * FROM watched_items WHERE id=?', (watch['parent_id'],)).fetchone()
    if not parent:
        return None
    source_id = watch['media_entity_id'] or _sync_watch_row(db, watch)
    target_id = parent['media_entity_id'] or _sync_watch_row(db, parent)
    rel_type = _watch_parent_relationship_type(watch, parent)
    if rel_type == 'part_of':
        existing_direct = db.execute('''SELECT 1 FROM media_relationships
            WHERE source_entity_id=? AND target_entity_id=?
              AND relationship_type IN ('part_of','parent_series')''',
                                     (source_id, target_id)).fetchone()
    else:
        existing_direct = db.execute('''SELECT 1 FROM media_relationships
            WHERE source_entity_id=? AND target_entity_id=? AND relationship_type=?''',
                                     (source_id, target_id, rel_type)).fetchone()
    existing_inverse = db.execute('''SELECT 1 FROM media_relationships
        WHERE source_entity_id=? AND target_entity_id=? AND relationship_type IN ('contains','includes_work')''',
                                   (target_id, source_id)).fetchone()
    if existing_direct or (existing_inverse and rel_type == 'part_of'):
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
        rel_type = (_watch_parent_relationship_type(watch, parent)
                    if parent else 'part_of')
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
    releases = [dict(row) for row in db.execute(
        f'SELECT * FROM media_releases WHERE entity_id IN ({placeholders}) ORDER BY entity_id,release_date,id',
        ordered_ids)]
    for release in releases:
        release['metadata'] = json.loads(release.pop('metadata_json') or '{}')
    claims = [dict(row) for row in db.execute(
        f'SELECT * FROM media_claim_observations WHERE entity_id IN ({placeholders}) ORDER BY entity_id,field_name,observed_at,id',
        ordered_ids)]
    for claim in claims:
        claim['value'] = json.loads(claim.pop('value_json'))
    return {'root_entity_id': root_entity_id, 'entities': entities,
            'relationships': relationships, 'releases': releases, 'claims': claims,
            'truncated': truncated}


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
    stable_graph['releases'] = [
        {key: value for key, value in release.items() if key not in {'observed_at'}}
        for release in graph.get('releases', [])]
    stable_graph['claims'] = [
        {key: value for key, value in claim.items() if key not in {'observed_at'}}
        for claim in graph.get('claims', [])]
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
    for collection_name, change_prefix in (('releases', 'release'), ('claims', 'claim')):
        before_values = {str(item.get('id') if collection_name == 'releases' else item.get('claim_key')): item
                         for item in before.get(collection_name, [])}
        after_values = {str(item.get('id') if collection_name == 'releases' else item.get('claim_key')): item
                        for item in stable_graph.get(collection_name, [])}
        for key in sorted(after_values.keys() - before_values.keys()):
            item = after_values[key]
            record(f'{change_prefix}_added', key, None, item, int(item.get('entity_id') or 0) or None)
        for key in sorted(before_values.keys() & after_values.keys()):
            if before_values[key] != after_values[key]:
                item = after_values[key]
                record(f'{change_prefix}_updated', key, before_values[key], item,
                       int(item.get('entity_id') or 0) or None)
    return {'snapshot_id': snapshot_id, 'baseline': False, 'unchanged': not changes,
            'changes': changes}
