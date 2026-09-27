"""Wikidata search and relationship import for the shared media graph."""
import concurrent.futures
import json
import re
import urllib.parse
from datetime import date, datetime, timezone

from .core import http_get
from .media_graph import (add_media_relationship, queue_graph_review,
                          upsert_media_entity)


API_URL = 'https://www.wikidata.org/w/api.php'
ENTITY_DATA_URL = 'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json'
_QID = re.compile(r'^Q[1-9]\d{0,11}$')

_RELATIONSHIP_PROPERTIES = {
    'P179': 'parent_series',
    'P361': 'part_of',
    'P155': 'follows',
    'P156': 'followed_by',
    'P1365': 'supersedes',
    'P1366': 'superseded_by',
    'P144': 'adaptation_of',
    'P527': 'contains',
    'P50': 'created_by',
}


def _get_json(url):
    raw, final_url = http_get(url, max_bytes=8_000_000)
    hostname = (urllib.parse.urlsplit(final_url).hostname or '').casefold()
    if hostname != 'wikidata.org' and not hostname.endswith('.wikidata.org'):
        raise ValueError('Wikidata request redirected away from wikidata.org')
    try:
        result = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Wikidata returned invalid JSON') from exc
    if not isinstance(result, dict):
        raise ValueError('Wikidata returned an unexpected response')
    return result


def search_wikidata(query, limit=10):
    """Return entity candidates from Wikidata's public MediaWiki API."""
    query = str(query or '').strip()
    if not query or len(query) > 200 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a Wikidata search up to 200 characters')
    limit = max(1, min(int(limit), 20))
    params = urllib.parse.urlencode({
        'action': 'wbsearchentities', 'search': query, 'language': 'en',
        'uselang': 'en', 'format': 'json', 'formatversion': 2, 'limit': limit,
    })
    payload = _get_json(API_URL + '?' + params)
    results = []
    for row in payload.get('search', []):
        qid = str(row.get('id') or '')
        title = str(row.get('label') or '').strip()
        if not _QID.fullmatch(qid) or not title:
            continue
        results.append({
            'provider': 'Wikidata', 'qid': qid, 'kind': 'entity', 'title': title,
            'description': str(row.get('description') or ''),
            'source_url': row.get('concepturi') or f'https://www.wikidata.org/entity/{qid}',
        })
    return results


def fetch_wikidata_entity(qid):
    qid = str(qid or '').strip().upper()
    if not _QID.fullmatch(qid):
        raise ValueError('Enter a valid Wikidata item ID such as Q123')
    payload = _get_json(ENTITY_DATA_URL.format(qid=urllib.parse.quote(qid)))
    entity = (payload.get('entities') or {}).get(qid)
    if not isinstance(entity, dict) or entity.get('id') != qid:
        raise ValueError(f'Wikidata did not return entity {qid}')
    return entity


def _qid_from_statement(statement):
    try:
        value = statement['mainsnak']['datavalue']['value']
        qid = value.get('id') if isinstance(value, dict) else None
    except (KeyError, TypeError):
        return None
    return qid if isinstance(qid, str) and _QID.fullmatch(qid) else None


def _claim_ids(entity, properties):
    found = []
    for property_id in properties:
        for statement in (entity.get('claims') or {}).get(property_id, []):
            qid = _qid_from_statement(statement)
            if qid:
                found.append((property_id, statement, qid))
    return found


def _label(entity):
    labels = entity.get('labels') or {}
    value = labels.get('en') or next(iter(labels.values()), {})
    if isinstance(value, dict):
        return str(value.get('value') or '').strip()
    return ''


def _description(entity):
    descriptions = entity.get('descriptions') or {}
    value = descriptions.get('en') or next(iter(descriptions.values()), {})
    return str(value.get('value') or '') if isinstance(value, dict) else ''


def _aliases(entity):
    result = []
    for value in (entity.get('aliases') or {}).values():
        for alias in value:
            if isinstance(alias, dict) and alias.get('value'):
                result.append(str(alias['value']))
    return list(dict.fromkeys(result))


def _type_labels(entity, related_payloads):
    ids = [qid for _, _, qid in _claim_ids(entity, ('P31', 'P279'))]
    return [_label(related_payloads.get(qid) or {}) for qid in ids]


def _classify_entity(entity, related_payloads):
    labels = [value.casefold() for value in _type_labels(entity, related_payloads) if value]
    text = ' '.join(labels + [_description(entity).casefold()])
    if 'franchise' in text or 'media franchise' in text:
        entity_type = 'franchise'
    elif any(word in text for word in ('fictional universe', 'shared universe', 'universe')):
        entity_type = 'universe'
    elif any(word in text for word in ('book series', 'novel series', 'literary series', 'television series',
                                       'film series', 'video game series', 'comic book series',
                                       'series of books')) or any(label == 'series' for label in labels):
        entity_type = 'series'
    elif any(label == 'season' or label.endswith(' season') for label in labels):
        entity_type = 'season'
    elif any(word in text for word in ('anthology', 'collection', 'box set', 'omnibus')):
        entity_type = 'collection'
    elif any(word in text for word in ('edition', 'version, edition')):
        entity_type = 'edition'
    elif any(word in text for word in ('human', 'person', 'fictional character')):
        entity_type = 'person'
    else:
        entity_type = 'work'

    if entity_type == 'person':
        media_type = 'person'
    elif any(word in text for word in ('video game', 'computer game', 'board game')):
        media_type = 'game'
    elif any(word in text for word in ('manga', 'manhwa', 'manhua')):
        media_type = 'manga'
    elif any(word in text for word in ('comic book', 'graphic novel')):
        media_type = 'comic'
    elif any(word in text for word in ('film', 'movie', 'television', 'web series', 'episode', 'video')):
        media_type = 'video'
    elif any(word in text for word in ('book', 'novel', 'literary work', 'written work')):
        media_type = 'book'
    else:
        media_type = 'unknown'
    return entity_type, media_type


def _wikidata_release_date(entity):
    partial = (None, None, None)
    for property_id in ('P577', 'P571', 'P580'):
        for statement in (entity.get('claims') or {}).get(property_id, []):
            try:
                value = statement['mainsnak']['datavalue']['value']
                raw = value.get('time', '')
                match = re.match(r'^\+?(\d{4}-\d{2}-\d{2})T', raw)
                if match:
                    try:
                        normalized = date.fromisoformat(match.group(1)).isoformat()
                    except ValueError:
                        # Wikidata allows year or month precision with zeroed
                        # lower-order fields; keep the claim, but don't invent a day.
                        if partial[2] is None:
                            partial = (None, property_id, statement)
                        continue
                    return normalized, property_id, statement
            except (KeyError, TypeError, AttributeError):
                continue
    return partial


def _series_position(statement):
    for qualifier in (statement.get('qualifiers') or {}).get('P1545', []):
        try:
            value = qualifier['datavalue']['value']
            return str(value.get('text') if isinstance(value, dict) else value)
        except (KeyError, TypeError):
            continue
    return None


def _make_entity_record(qid, payload, related_payloads):
    payload = payload or {}
    label = _label(payload) or qid
    entity_type, media_type = _classify_entity(payload, related_payloads)
    release_date, release_property, release_statement = _wikidata_release_date(payload)
    return {
        'qid': qid, 'title': label, 'description': _description(payload),
        'aliases': _aliases(payload), 'entity_type': entity_type,
        'media_type': media_type, 'release_date': release_date,
        'release_property': release_property, 'release_statement': release_statement,
        'source_url': f'https://www.wikidata.org/entity/{qid}',
    }


def import_wikidata_entity(db, qid, watch_entity_id=None, entity_payload=None,
                           related_payloads=None, max_related=40):
    """Import one Wikidata entity and its explicit, supported relationships.

    Claims are saved as sourced graph edges. This function never creates watches or
    queues downloads; watch policy can make that decision in a later roadmap step.
    """
    qid = str(qid or '').strip().upper()
    if not _QID.fullmatch(qid):
        raise ValueError('Enter a valid Wikidata item ID such as Q123')
    root_payload = entity_payload or fetch_wikidata_entity(qid)
    if root_payload.get('id') != qid:
        raise ValueError(f'Wikidata entity payload does not match {qid}')
    relation_claims = _claim_ids(root_payload, _RELATIONSHIP_PROPERTIES)
    type_claims = _claim_ids(root_payload, ('P31', 'P279'))
    related_ids = list(dict.fromkeys(target for _, _, target in relation_claims if target != qid))
    related_ids = related_ids[:max(0, min(int(max_related), 100))]
    lookup_ids = list(dict.fromkeys(
        target for _, _, target in relation_claims + type_claims if target != qid))
    lookup_ids = lookup_ids[:max(0, min(int(max_related) + len(type_claims), 120))]
    target_payloads = dict(related_payloads or {})
    missing = [target for target in lookup_ids if target not in target_payloads]
    if missing:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(missing))) as pool:
            futures = {pool.submit(fetch_wikidata_entity, target): target for target in missing}
            for future, target in [(future, futures[future]) for future in futures]:
                try:
                    target_payloads[target] = future.result()
                except Exception as exc:
                    target_payloads[target] = None
                    queue_graph_review(db, 'source_unavailable',
                                       f'Wikidata could not resolve {target}',
                                       {'provider': 'Wikidata', 'qid': target, 'error': str(exc)},
                                       dedupe_key=f'wikidata-unresolved:{qid}:{target}')

    root_record = _make_entity_record(qid, root_payload, target_payloads)
    external_id = {'provider': 'wikidata', 'external_id': qid,
                   'source_url': root_record['source_url']}
    if watch_entity_id is not None:
        current = db.execute('SELECT title,media_type FROM media_entities WHERE id=?',
                             (int(watch_entity_id),)).fetchone()
        if not current:
            raise ValueError(f'Unknown watched media entity: {watch_entity_id}')
        existing_external = db.execute('''SELECT entity_id FROM media_external_ids
            WHERE provider='wikidata' AND external_id=?''', (qid,)).fetchone()
        if existing_external and int(existing_external['entity_id']) != int(watch_entity_id):
            other_id = int(existing_external['entity_id'])
            root_id = other_id
            queue_graph_review(db, 'suspected_duplicate', 'Wikidata item may duplicate a watched entity',
                               {'watched_entity_id': int(watch_entity_id), 'wikidata_entity_id': other_id,
                                'qid': qid}, entity_id=int(watch_entity_id),
                               dedupe_key=f'wikidata-watch-duplicate:{watch_entity_id}:{qid}')
            add_media_relationship(db, int(watch_entity_id), other_id, 'possible_duplicate_of',
                                   confidence=0.5, inferred=True, status='candidate',
                                   provider='Wikidata', provider_entity_id=qid,
                                   raw_relationship_type='identity-match',
                                   source_url=root_record['source_url'],
                                   evidence={'qid': qid, 'reason': 'external ID already linked'},
                                   is_explicit=False)
        else:
            root_id = upsert_media_entity(
                db, root_record['entity_type'], root_record['title'],
                media_type=root_record['media_type'] if root_record['media_type'] != 'unknown' else current['media_type'],
                canonical_url=root_record['source_url'], release_date=root_record['release_date'],
                metadata={'wikidata_description': root_record['description'], 'wikidata_qid': qid},
                aliases=root_record['aliases'], external_ids=[external_id],
                entity_id=int(watch_entity_id), provider='Wikidata')
    else:
        root_id = upsert_media_entity(
            db, root_record['entity_type'], root_record['title'], root_record['media_type'],
            root_record['source_url'], root_record['release_date'],
            metadata={'wikidata_description': root_record['description'], 'wikidata_qid': qid},
            aliases=root_record['aliases'], external_ids=[external_id], provider='Wikidata')

    entity_ids = {qid: root_id}
    for target_id in related_ids:
        record = _make_entity_record(target_id, target_payloads.get(target_id) or {}, target_payloads)
        target_external_id = {'provider': 'wikidata', 'external_id': target_id,
                              'source_url': record['source_url']}
        entity_ids[target_id] = upsert_media_entity(
            db, record['entity_type'], record['title'], record['media_type'],
            record['source_url'], record['release_date'],
            metadata={'wikidata_description': record['description'], 'wikidata_qid': target_id,
                      'unresolved': target_payloads.get(target_id) is None},
            aliases=record['aliases'], external_ids=[target_external_id], provider='Wikidata')

    relationships = []
    seen_claims = set()
    for property_id, statement, target_qid in relation_claims:
        if target_qid not in entity_ids:
            continue
        relation_type = _RELATIONSHIP_PROPERTIES[property_id]
        claim_identity = (property_id, target_qid)
        if claim_identity in seen_claims:
            continue
        seen_claims.add(claim_identity)
        related_record = _make_entity_record(target_qid, target_payloads.get(target_qid) or {}, target_payloads)
        evidence = {'property_id': property_id,
                    'property_label': {
                        'P179': 'part of series', 'P361': 'part of', 'P155': 'follows',
                        'P156': 'followed by', 'P1365': 'replaces', 'P1366': 'replaced by',
                        'P144': 'based on', 'P527': 'has part', 'P50': 'author',
                    }.get(property_id, property_id)}
        series_position = _series_position(statement)
        if series_position is not None:
            evidence['series_position'] = series_position
        relation_id = add_media_relationship(
            db, root_id, entity_ids[target_qid], relation_type, confidence=0.9,
            inferred=False, status='accepted', provider='Wikidata',
            provider_entity_id=target_qid, raw_relationship_type=property_id,
            source_url=root_record['source_url'], evidence=evidence,
            metadata={'series_position': series_position} if series_position else {},
            is_explicit=True)
        relationships.append({
            'id': relation_id, 'relationship_type': relation_type,
            'target_qid': target_qid, 'target_entity_id': entity_ids[target_qid],
            'target_title': related_record['title'],
            'target_type': related_record['entity_type'],
            'confidence': 0.9, 'source_url': root_record['source_url'],
            'series_position': series_position,
            'target_release_date': related_record['release_date'],
        })

    if root_record['release_statement']:
        statement = root_record['release_statement']
        provider_release_id = str(statement.get('id') or
                                  f"{root_record['release_property']}:{root_record['release_date']}")
        db.execute('''INSERT INTO media_releases
            (entity_id,release_type,release_date,status,provider,provider_release_id,
             source_url,observed_at,metadata_json)
            VALUES (?,'publication',?,NULL,'Wikidata',?,?,?,?)
            ON CONFLICT(entity_id,provider,provider_release_id,release_type) DO UPDATE SET
                release_date=excluded.release_date,source_url=excluded.source_url,
                observed_at=excluded.observed_at,metadata_json=excluded.metadata_json''',
                   (root_id, root_record['release_date'], provider_release_id,
                    root_record['source_url'], datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    json.dumps({'property_id': root_record['release_property'],
                                'raw_time': statement.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('time'),
                                'precision': statement.get('mainsnak', {}).get('datavalue', {}).get('value', {}).get('precision')},
                               sort_keys=True)))

    return {'root_entity_id': root_id, 'qid': qid, 'title': root_record['title'],
            'entity_type': root_record['entity_type'], 'media_type': root_record['media_type'],
            'source_url': root_record['source_url'], 'relationships': relationships,
            'related_entities': len(entity_ids) - 1,
            'unresolved_entities': sum(target_payloads.get(value) is None for value in related_ids)}
