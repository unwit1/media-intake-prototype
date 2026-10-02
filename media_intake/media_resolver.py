"""Provider-independent entry points for resolving and refreshing media graphs."""
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
import re
import urllib.parse

from .book_identifiers import book_work_title_key, collect_isbns
from .comics import anilist_video_subtype, fetch_anilist_media
from .core import http_get
from .discovery import (CATEGORIES, _promote_matching_series_results,
                        search_media_category)
from .media_graph import (add_media_relationship, capture_graph_snapshot,
                          effective_watch_policy, get_media_graph, list_media_changes,
                          continuation_watch_policy_key, queue_graph_review,
                          relationship_watch_action,
                          upsert_media_entity, upsert_media_release)
from .media_catalogs import english_release_date, ranobedb_series
from .media_providers import (fetch_igdb_game, fetch_steam_game,
                              fetch_tmdb_collection, fetch_tmdb_media,
                              fetch_tvmaze_show, list_tvmaze_episodes,
                              list_tvmaze_seasons)
from .sources import search_google_books_metadata, search_openlibrary_metadata
from .wikidata import import_wikidata_entity


def resolve(query, category=None, source_options=None, credentials=None,
            download_priority=None):
    """Resolve one query through one media family or auto-detect across families."""
    category = str(category or 'Auto-detect')
    if category.casefold() != 'auto-detect':
        return search_media_category(query, category, source_options, credentials,
                                     download_priority)
    categories = [name for name in CATEGORIES if name != 'Fanfiction']
    if str(query or '').strip().startswith(('http://', 'https://')):
        categories.append('Fanfiction')
    results, errors, searched = [], [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        future_by_category = {
            pool.submit(search_media_category, query, name, source_options, credentials,
                        (download_priority or {}).get(name)): name
            for name in categories
        }
        for future, name in [(future, future_by_category[future]) for future in future_by_category]:
            try:
                response = future.result()
            except Exception as exc:
                errors.append(f'{name}: {exc}')
                searched.append(name)
                continue
            results.extend(response.get('results') or [])
            errors.extend(response.get('errors') or [])
            searched.extend(f'{name} / {provider}' for provider in response.get('searched') or [])
    results.sort(key=lambda row: (row.get('category', ''),
                                  {'issue': 0, 'series': 1, 'collection': 2}.get(row.get('comic_entity'), 0),
                                  row.get('title', '').casefold(),
                                  str(row.get('release_date') or '')))
    results = _promote_matching_series_results(results, query)
    return {'category': 'Auto-detect', 'results': results[:1000],
            'errors': list(dict.fromkeys(errors)), 'searched': searched}


def refresh_graph(db, entity_id, max_depth=3, source_options=None):
    """Refresh relationships from supported provider IDs in a bounded neighborhood.

    This is graph-only: it never creates watched rows or queues file searches.
    Provider credentials may be passed for adapters such as TMDB; credentials are
    not persisted by this function. Each newly reached entity is refreshed once,
    up to ``max_depth`` relationship hops and 100 entities, to avoid runaway
    traversal through very large shared-universe catalogs.
    """
    entity_id = int(entity_id)
    if not db.execute('SELECT 1 FROM media_entities WHERE id=?', (entity_id,)).fetchone():
        raise ValueError(f'Unknown media entity ID: {entity_id}')
    max_depth = max(0, min(int(max_depth), 8))
    max_entities = 100
    options = dict(source_options or {})
    tmdb_key = str(options.get('tmdb_api_key') or
                   os.environ.get('TMDB_API_KEY', '')).strip()
    imported = []
    errors = []
    skipped = []
    pending = [(entity_id, 0)]
    visited_entities = set()
    processed_sources = set()
    truncated = False
    while pending:
        current_id, depth = pending.pop(0)
        if current_id in visited_entities:
            continue
        if len(visited_entities) >= max_entities:
            truncated = True
            break
        visited_entities.add(current_id)
        row = db.execute('SELECT * FROM media_entities WHERE id=?', (current_id,)).fetchone()
        if not row:
            continue
        current = dict(row)
        external_ids = [dict(value) for value in db.execute(
            'SELECT provider,external_id,source_url FROM media_external_ids '
            'WHERE entity_id=? ORDER BY provider,external_id', (current_id,))]
        for external in external_ids:
            provider = str(external.get('provider') or '').casefold()
            external_id = str(external.get('external_id') or '')
            source_key = (provider, external_id)
            if not external_id or source_key in processed_sources:
                continue
            processed_sources.add(source_key)
            try:
                if provider == 'wikidata':
                    result = import_wikidata_entity(
                        db, external_id, watch_entity_id=current_id)
                    if int(result.get('root_entity_id') or 0) != current_id:
                        raise ValueError('Wikidata ID resolves to a different graph entity')
                    imported.append({'provider': 'Wikidata', 'external_id': external_id,
                                     'relationships': len(result.get('relationships') or [])})
                elif provider == 'anilist':
                    count = _refresh_anilist_graph(db, current, external_id)
                    imported.append({'provider': 'AniList', 'external_id': external_id,
                                     'relationships': count})
                elif provider in {'tmdb_movie', 'tmdb_tv'}:
                    if not tmdb_key:
                        errors.append(f'TMDB {external_id}: configure a TMDB API key to refresh this source')
                        continue
                    count = _refresh_tmdb_graph(
                        db, current, external_id,
                        'movie' if provider == 'tmdb_movie' else 'tv', tmdb_key)
                    imported.append({'provider': 'TMDB', 'external_id': external_id,
                                     'relationships': count})
                elif provider == 'tvmaze':
                    count = _refresh_tvmaze_graph(db, current, external_id)
                    imported.append({'provider': 'TVMaze', 'external_id': external_id,
                                     'relationships': count})
                elif provider == 'igdb':
                    client_id = str(options.get('igdb_client_id') or
                                    os.environ.get('IGDB_CLIENT_ID', '')).strip()
                    client_secret = str(options.get('igdb_client_secret') or
                                        os.environ.get('IGDB_CLIENT_SECRET', '')).strip()
                    if not client_id or not client_secret:
                        errors.append(f'IGDB {external_id}: configure IGDB credentials to refresh this source')
                        continue
                    count = _refresh_igdb_graph(
                        db, current, external_id, client_id, client_secret)
                    imported.append({'provider': 'IGDB', 'external_id': external_id,
                                     'relationships': count})
                elif provider == 'steam':
                    count, provider_errors = _refresh_steam_graph(db, current, external_id)
                    imported.append({'provider': 'Steam', 'external_id': external_id,
                                     'relationships': count})
                    errors.extend(f'Steam {external_id} {message}'
                                  for message in provider_errors)
                elif provider == 'goodreads' and current.get('entity_type') == 'person':
                    count, provider_errors = _refresh_goodreads_author_graph(
                        db, current, external.get('source_url'))
                    imported.append({'provider': 'Goodreads', 'external_id': external_id,
                                     'relationships': count})
                    errors.extend(f'Goodreads {external_id}: {error}'
                                  for error in provider_errors)
                elif provider == 'goodreads' and current.get('entity_type') == 'series':
                    count, provider_errors = _refresh_goodreads_series_graph(
                        db, current, external.get('source_url'))
                    imported.append({'provider': 'Goodreads', 'external_id': external_id,
                                     'relationships': count})
                    errors.extend(f'Goodreads {external_id}: {error}'
                                  for error in provider_errors)
                elif provider == 'googlebooks' and current.get('entity_type') == 'series':
                    count = _refresh_google_books_series_graph(db, current)
                    imported.append({'provider': 'Google Books', 'external_id': external_id,
                                     'relationships': count})
                elif provider == 'openlibrary' and current.get('entity_type') == 'person':
                    count, provider_errors = _refresh_openlibrary_author_graph(
                        db, current, external_id)
                    imported.append({'provider': 'Open Library', 'external_id': external_id,
                                     'relationships': count})
                    errors.extend(f'Open Library {external_id}: {error}'
                                  for error in provider_errors)
                elif provider == 'openlibrary' and current.get('entity_type') == 'series':
                    count = _refresh_openlibrary_series_graph(db, current)
                    imported.append({'provider': 'Open Library', 'external_id': external_id,
                                     'relationships': count})
                elif provider == 'ranobedb' and current.get('entity_type') == 'series':
                    count = _refresh_ranobedb_series_graph(db, current, external_id)
                    imported.append({'provider': 'RanobeDB', 'external_id': external_id,
                                     'relationships': count})
                elif provider not in {'media-intake-episode'}:
                    skipped.append({'provider': provider, 'external_id': external_id})
            except Exception as exc:
                errors.append(f'{provider or "Catalog"} {external_id}: {exc}')

        if depth >= max_depth:
            continue
        neighbors = db.execute('''SELECT CASE WHEN source_entity_id=?
                THEN target_entity_id ELSE source_entity_id END AS entity_id
            FROM media_relationships WHERE source_entity_id=? OR target_entity_id=?
            ORDER BY entity_id''', (current_id, current_id, current_id)).fetchall()
        for neighbor in neighbors:
            neighbor_id = int(neighbor['entity_id'])
            if neighbor_id in visited_entities or any(item[0] == neighbor_id for item in pending):
                continue
            if len(visited_entities) + len(pending) >= max_entities:
                truncated = True
                break
            pending.append((neighbor_id, depth + 1))

    diff = capture_graph_snapshot(db, entity_id, max_depth=max_depth,
                                  max_entities=max_entities)
    db.commit()
    return {'entity_id': entity_id, 'refreshed': imported, 'errors': errors,
            'skipped': skipped, 'refresh_truncated': truncated,
            'entities_visited': len(visited_entities),
            'diff': diff, 'graph': get_media_graph(
                db, entity_id, max_depth=max_depth, max_entities=max_entities),
            'changes': list_media_changes(db, entity_id)}


def _catalog_relation_status(db, root, relationship_type, confidence, policy_key=None):
    metadata = json.loads(root.get('metadata_json') or '{}')
    scope = str(metadata.get('entity_type') or root.get('entity_type') or 'work')
    policy = effective_watch_policy(
        db, root.get('media_type'), [(scope, str(root['id']))])
    action = relationship_watch_action(policy, policy_key or relationship_type, confidence)
    return ('ignored' if action == 'ignore' else
            'candidate' if action == 'discover' else 'accepted')


def _book_relationship_external_ids(db, root, title, author, provider,
                                    provider_id, source_url, isbns):
    """Attach ISBNs only when their existing graph work has compatible identity."""
    external_ids = [{'provider': provider, 'external_id': str(provider_id),
                     'source_url': source_url}]
    isbn_values = collect_isbns(isbns)
    if not isbn_values:
        return external_ids

    def creator_key(value):
        if isinstance(value, (list, tuple)):
            value = ', '.join(str(part) for part in value if part)
        return _normalized_catalog_name(value)

    incoming_title = book_work_title_key(title)
    incoming_author = creator_key(author)
    unknown_authors = {'', 'unknown', 'none', 'anonymous'}
    matches, rejected, unseen = set(), [], []
    for isbn in isbn_values:
        row = db.execute('''SELECT e.id,e.entity_type,e.media_type,e.title,e.metadata_json
            FROM media_external_ids x JOIN media_entities e ON e.id=x.entity_id
            WHERE x.provider='isbn' AND x.external_id=? LIMIT 1''', (isbn,)).fetchone()
        if not row:
            unseen.append(isbn)
            continue
        try:
            existing_metadata = json.loads(row['metadata_json'] or '{}')
        except (TypeError, ValueError):
            existing_metadata = {}
        existing_author = creator_key(existing_metadata.get('author') or
                                      existing_metadata.get('authors'))
        authors_compatible = (incoming_author in unknown_authors or
                              existing_author in unknown_authors or
                              incoming_author == existing_author)
        if (row['entity_type'] == 'work' and row['media_type'] == 'book' and
                incoming_title and book_work_title_key(row['title']) == incoming_title and
                authors_compatible):
            matches.add(int(row['id']))
        else:
            rejected.append({'isbn': isbn, 'entity_id': int(row['id']),
                             'title': row['title'], 'author': existing_metadata.get('author')})

    if len(matches) > 1:
        rejected.append({'reason': 'Incoming ISBNs are already attached to different works',
                         'entity_ids': sorted(matches)})
        matches.clear()
        unseen.clear()
    if rejected:
        queue_graph_review(
            db, 'catalog_identity_conflict',
            f'Review ISBN identity for {title}',
            {'provider': provider, 'provider_id': str(provider_id),
             'source_url': source_url, 'incoming_author': author,
             'incoming_isbns': isbn_values, 'conflicts': rejected},
            entity_id=int(root['id']),
            dedupe_key=f'catalog-isbn-identity:{provider}:{provider_id}:{root["id"]}')

    accepted = set(unseen)
    if len(matches) == 1:
        accepted.update(isbn for isbn in isbn_values
                        if not any(item.get('isbn') == isbn for item in rejected
                                   if isinstance(item, dict)))
    external_ids.extend({'provider': 'isbn', 'external_id': isbn,
                         'source_url': source_url}
                        for isbn in sorted(accepted))
    return external_ids


def _link_catalog_relationship(db, root, *, entity_type, media_type, title,
                               provider, provider_id, source_url, metadata,
                               relationship, raw_relationship_type,
                               relationship_from_child=False, confidence=0.9,
                               inverse_relationship=None, policy_key=None,
                               relationship_metadata=None):
    """Upsert one provider node and attach explicit relationship evidence."""
    title = str(title or '').strip()
    provider_id = str(provider_id or '').strip()
    if not title or not provider_id:
        return None
    external_ids = [{'provider': provider, 'external_id': provider_id,
                     'source_url': source_url}]
    if media_type == 'book' and entity_type == 'work':
        external_ids = _book_relationship_external_ids(
            db, root, title, metadata.get('author'), provider, provider_id,
            source_url, metadata.get('isbns') or metadata.get('isbn'))
    try:
        child_id = upsert_media_entity(
            db, entity_type, title, media_type=media_type,
            canonical_url=source_url, release_date=metadata.get('release_date'),
            status=metadata.get('status'), metadata=metadata,
            external_ids=external_ids, provider=provider)
    except ValueError as exc:
        queue_graph_review(
            db, 'catalog_identity_conflict',
            f'Review {provider} relationship target: {title}',
            {'provider': provider, 'provider_id': provider_id,
             'source_url': source_url, 'error': str(exc)}, entity_id=int(root['id']),
            dedupe_key=f'catalog-refresh-conflict:{provider}:{provider_id}:{root["id"]}')
        return None
    if int(child_id) == int(root['id']):
        return None
    status = _catalog_relation_status(db, root, relationship, confidence, policy_key)
    source_id, target_id = ((child_id, int(root['id'])) if relationship_from_child
                            else (int(root['id']), child_id))
    evidence = dict(metadata or {})
    evidence['catalog_title'] = title
    add_media_relationship(
        db, source_id, target_id, relationship, confidence=confidence,
        inferred=False, status=status, provider=provider,
        provider_entity_id=provider_id,
        raw_relationship_type=raw_relationship_type,
        source_url=root.get('canonical_url'), evidence=evidence,
        metadata=relationship_metadata,
        is_explicit=True)
    if inverse_relationship:
        inverse_source, inverse_target = ((int(root['id']), child_id)
                                          if relationship_from_child else
                                          (child_id, int(root['id'])))
        add_media_relationship(
            db, inverse_source, inverse_target, inverse_relationship,
            confidence=confidence, inferred=False, status=status,
            provider=provider, provider_entity_id=provider_id,
            raw_relationship_type=raw_relationship_type,
            source_url=root.get('canonical_url'), evidence=evidence,
            metadata=relationship_metadata,
            is_explicit=True)
    return child_id


def _refresh_anilist_graph(db, root, external_id):
    details = fetch_anilist_media(external_id)
    if str(details.get('id') or '') != str(external_id):
        raise ValueError('AniList returned a different media record')
    relation_map = {
        'SEQUEL': ('sequel_to', 'direct_sequel_series', True),
        'PREQUEL': ('prequel_to', 'direct_prequel_series', True),
        'SPIN_OFF': ('spin_off_of', 'spin_off_of', True),
        'SIDE_STORY': ('parallel_story', 'parallel_story', False),
        'CHARACTER': ('character_spin_off', 'character_spin_off', False),
        'REMAKE': ('remake_of', 'remake_of', True),
        'PARENT': ('part_of', 'same_universe', False),
        'ADAPTATION': ('adaptation_of', 'adaptation_of', False),
        'SUMMARY': ('collection_of', 'collection_of', False),
        'COMPILATION': ('collection_of', 'collection_of', False),
        'ALTERNATIVE': ('alternate_continuity_of', 'alternate_continuity_of', False),
    }
    count = 0
    for edge in ((details.get('relations') or {}).get('edges') or [])[:100]:
        relation_type = str(edge.get('relationType') or '').upper()
        mapped = relation_map.get(relation_type)
        node = edge.get('node') or {}
        node_id = str(node.get('id') or '').strip()
        node_title_data = node.get('title') or {}
        node_title = (node_title_data.get('english') or node_title_data.get('romaji') or
                      node_title_data.get('native'))
        if not mapped or not node_id or node_id == str(external_id) or not node_title:
            continue
        relation, policy_key, from_child = mapped
        anime = str(node.get('type') or '').upper() == 'ANIME'
        format_value = str(node.get('format') or '').upper()
        is_series = format_value in {'TV', 'TV_SHORT', 'ONA'}
        release_date = _anilist_release_date(node.get('startDate'))
        if relation_type in {'SEQUEL', 'PREQUEL'}:
            policy_key = continuation_watch_policy_key(
                relation, 'series' if is_series else 'work', release_date)
        media_type = 'video' if anime else 'manga'
        source_url = node.get('siteUrl') or f'https://anilist.co/{"anime" if anime else "manga"}/{node_id}'
        subtype = anilist_video_subtype(format_value) if anime else None
        node_entity = _link_catalog_relationship(
            db, root, entity_type='series' if is_series else 'work',
            media_type=media_type, title=node_title, provider='anilist',
            provider_id=node_id, source_url=source_url,
            metadata={'entity_type': 'series' if is_series else 'work',
                      'video_subtype': subtype, 'release_date': release_date,
                      'status': str(node.get('status') or '').casefold(),
                      'relation_type': relation_type},
            relationship=relation, raw_relationship_type=relation_type,
            relationship_from_child=from_child, confidence=0.88,
            policy_key=policy_key)
        if node_entity:
            count += 1
    return count


def _anilist_release_date(value):
    value = value if isinstance(value, dict) else {}
    try:
        return dt.date(int(value['year']), int(value['month']), int(value['day'])).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _refresh_tmdb_graph(db, root, external_id, media_type, api_key):
    details = fetch_tmdb_media(external_id, media_type, api_key)
    count = 0
    if media_type == 'movie':
        collection = details.get('belongs_to_collection')
        if not isinstance(collection, dict) or not collection.get('id') or not collection.get('name'):
            return count
        collection_id = str(collection['id'])
        collection_url = f'https://www.themoviedb.org/collection/{collection_id}'
        collection_entity = _link_catalog_relationship(
            db, root, entity_type='collection', media_type='video',
            title=collection['name'], provider='tmdb_collection',
            provider_id=collection_id, source_url=collection_url,
            metadata={'video_subtype': 'collection', 'tmdb_id': collection_id},
            relationship='part_of', raw_relationship_type='belongs_to_collection',
            confidence=0.9)
        if not collection_entity:
            return count
        count += 1
        collection_data = fetch_tmdb_collection(collection_id, api_key)
        for part in (collection_data.get('parts') or [])[:100]:
            part_id = str(part.get('id') or '')
            part_title = str(part.get('title') or '').strip()
            if not part_id or not part_title:
                continue
            part_url = f'https://www.themoviedb.org/movie/{part_id}'
            child = _link_catalog_relationship(
                db, {'id': collection_entity,
                     'metadata_json': '{"entity_type":"collection"}',
                     'entity_type': 'collection', 'media_type': 'video',
                     'canonical_url': collection_url},
                entity_type='work', media_type='video', title=part_title,
                provider='tmdb_movie', provider_id=part_id, source_url=part_url,
                metadata={'video_subtype': 'movie',
                          'release_date': part.get('release_date'),
                          'overview': part.get('overview') or ''},
                relationship='contains', raw_relationship_type='parts',
                confidence=0.9)
            count += int(child is not None)
    elif media_type == 'tv':
        next_episode = details.get('next_episode_to_air')
        if isinstance(next_episode, dict) and next_episode.get('id'):
            season = next_episode.get('season_number')
            number = next_episode.get('episode_number')
            episode_id = str(next_episode['id'])
            episode_url = (f'https://www.themoviedb.org/tv/{external_id}/episode/'
                           f'{season}/{number}')
            release_date = _exact_date(next_episode.get('air_date'))
            child = _link_catalog_relationship(
                db, root, entity_type='work', media_type='video',
                title=str(next_episode.get('name') or f"{root['title']} Episode {number}"),
                provider='tmdb_episode', provider_id=episode_id,
                source_url=episode_url,
                metadata={'video_subtype': 'episode', 'season_number': season,
                          'episode_number': number, 'release_date': release_date,
                          'status': 'upcoming'},
                relationship='episode_of', raw_relationship_type='next_episode_to_air',
                relationship_from_child=True, confidence=0.94,
                inverse_relationship='contains', policy_key='episodes')
            if child:
                upsert_media_release(
                    db, int(root['id']), 'episode', release_date,
                    status='upcoming', provider='TMDB',
                    provider_release_id=f'episode:{episode_id}',
                    source_url=episode_url,
                    metadata={'title': next_episode.get('name'),
                              'season_number': season, 'episode_number': number,
                              'video_subtype': 'episode'})
                count += 1
    return count


def _refresh_tvmaze_graph(db, root, external_id):
    show_id = int(external_id)
    show = fetch_tvmaze_show(show_id)
    if int(show.get('id') or 0) != show_id:
        raise ValueError('TVMaze returned a different show')
    show_title = str(show.get('name') or root.get('title') or 'TV series')
    count = 0
    for season in list_tvmaze_seasons(show_id)[:100]:
        season_id = str(season.get('id') or '')
        if not season_id:
            continue
        number = season.get('number')
        season_label = f'Season {number}' if number is not None else 'Season'
        source_url = f'https://www.tvmaze.com/seasons/{season_id}'
        season_entity = _link_catalog_relationship(
            db, root, entity_type='season', media_type='video',
            title=f'{show_title} {season_label}'.strip(), provider='tvmaze_season',
            provider_id=season_id, source_url=source_url,
            metadata={'season_number': number,
                      'release_date': _exact_date(season.get('premiereDate')),
                      'status': 'released'},
            relationship='contains', inverse_relationship='season_of',
            raw_relationship_type='seasons', confidence=0.9,
            policy_key='new_seasons')
        count += int(season_entity is not None)

    for episode in list_tvmaze_episodes(show_id, include_specials=True)[:2500]:
        episode_id = str(episode.get('id') or '')
        if not episode_id:
            continue
        season_number, episode_number = episode.get('season'), episode.get('number')
        is_special = (str(episode.get('type') or '').casefold() == 'special' or
                      season_number == 0)
        relation = 'special_of' if is_special else 'episode_of'
        policy_key = 'specials' if is_special else 'episodes'
        source_url = episode.get('url') or f'https://www.tvmaze.com/episodes/{episode_id}'
        release_date = _exact_date(episode.get('airdate'))
        status = ('upcoming' if release_date and release_date > dt.date.today().isoformat()
                  else 'released')
        title = str(episode.get('name') or f'{show_title} Episode {episode_id}')
        position = (f'S{int(season_number):02d}E{int(episode_number):02d}'
                    if isinstance(season_number, int) and
                    isinstance(episode_number, int) else f'Episode {episode_id}')
        episode_entity = _link_catalog_relationship(
            db, root, entity_type='work', media_type='video',
            title=f'{show_title} {position} - {title}', provider='tvmaze_episode',
            provider_id=episode_id, source_url=source_url,
            metadata={'video_subtype': 'special' if is_special else 'episode',
                      'season_number': season_number,
                      'episode_number': episode_number,
                      'release_date': release_date, 'status': status,
                      'episode_type': episode.get('type')},
            relationship=relation, relationship_from_child=True,
            inverse_relationship='contains', raw_relationship_type='episodes',
            confidence=0.94, policy_key=policy_key)
        if not episode_entity:
            continue
        upsert_media_release(
            db, int(root['id']), 'episode', release_date, status=status,
            provider='TVMaze', provider_release_id=f'episode:{episode_id}',
            source_url=source_url,
            metadata={'title': title, 'season_number': season_number,
                      'episode_number': episode_number,
                      'video_subtype': 'special' if is_special else 'episode'})
        count += 1
    return count


def _refresh_igdb_graph(db, root, external_id, client_id, client_secret):
    details = fetch_igdb_game(external_id, client_id, client_secret)
    if str(details.get('id') or '') != str(external_id):
        raise ValueError('IGDB returned a different game')
    game_url = details.get('url') or f"https://www.igdb.com/games/{details.get('slug') or external_id}"
    subtype = {1: 'dlc', 2: 'expansion', 3: 'bundle', 4: 'standalone_expansion',
               8: 'remake', 9: 'remaster', 11: 'port'}.get(details.get('category'), 'game')
    count = 0
    for franchise in (details.get('franchises') or [])[:50]:
        if isinstance(franchise, dict) and franchise.get('id') and franchise.get('name'):
            source_url = f"https://www.igdb.com/franchises/{franchise.get('slug') or franchise['id']}"
            target = _link_catalog_relationship(
                db, root, entity_type='franchise', media_type='game',
                title=franchise['name'], provider='igdb_franchise',
                provider_id=franchise['id'], source_url=source_url,
                metadata={'game_subtype': 'franchise'}, relationship='part_of',
                raw_relationship_type='franchises', confidence=0.96)
            count += int(target is not None)
    collection = details.get('collection')
    if isinstance(collection, dict) and collection.get('id') and collection.get('name'):
        source_url = f"https://www.igdb.com/collections/{collection.get('slug') or collection['id']}"
        target = _link_catalog_relationship(
            db, root, entity_type='collection', media_type='game',
            title=collection['name'], provider='igdb_collection',
            provider_id=collection['id'], source_url=source_url,
            metadata={'game_subtype': 'collection'}, relationship='part_of',
            raw_relationship_type='collection', confidence=0.94)
        count += int(target is not None)

    version_parent = details.get('version_parent')
    if isinstance(version_parent, dict) and version_parent.get('id') and version_parent.get('name'):
        relationship = 'remake_of' if subtype == 'remake' else 'edition_of'
        target = _link_catalog_relationship(
            db, root, entity_type='work', media_type='game',
            title=version_parent['name'], provider='igdb',
            provider_id=version_parent['id'], source_url=version_parent.get('url'),
            metadata={'game_subtype': 'base_game'}, relationship=relationship,
            raw_relationship_type='version_parent', confidence=0.9)
        count += int(target is not None)
    parent_game = details.get('parent_game')
    if isinstance(parent_game, dict) and parent_game.get('id') and parent_game.get('name'):
        target = _link_catalog_relationship(
            db, root, entity_type='work', media_type='game',
            title=parent_game['name'], provider='igdb',
            provider_id=parent_game['id'], source_url=parent_game.get('url'),
            metadata={'game_subtype': 'base_game'}, relationship='part_of',
            raw_relationship_type='parent_game', confidence=0.9)
        count += int(target is not None)

    categories = {
        'dlcs': ('dlc', 'game_dlc', 'contains', False),
        'expansions': ('expansion', 'game_expansions', 'contains', False),
        'standalone_expansions': ('standalone_expansion', 'game_expansions', 'contains', False),
        'remakes': ('remake', 'remake_of', 'remake_of', True),
        'remasters': ('remaster', 'remasters_and_ports', 'edition_of', True),
        'ports': ('port', 'remasters_and_ports', 'edition_of', True),
    }
    for field, (child_subtype, policy_key, relation, from_child) in categories.items():
        for child in (details.get(field) or [])[:100]:
            if not isinstance(child, dict) or not child.get('id') or not child.get('name'):
                continue
            child_id = str(child['id'])
            child_url = child.get('url') or f"https://www.igdb.com/games/{child.get('slug') or child_id}"
            child_entity_type = ('release' if child_subtype in
                                 {'dlc', 'expansion', 'remaster', 'port'} else 'work')
            linked = _link_catalog_relationship(
                db, root, entity_type=child_entity_type,
                media_type='game', title=child['name'], provider='igdb',
                provider_id=child_id, source_url=child_url,
                metadata={'game_subtype': child_subtype,
                          'entity_type': child_entity_type},
                relationship=relation, raw_relationship_type=field,
                relationship_from_child=from_child, confidence=0.92,
                policy_key=policy_key)
            count += int(linked is not None)
    return count


def _refresh_steam_graph(db, root, external_id):
    details = fetch_steam_game(external_id)
    if not details.get('name'):
        raise ValueError('Steam returned a game without a title')
    count = 0
    errors = []
    for dlc_id in (details.get('dlc') or [])[:100]:
        if not str(dlc_id).isdigit() or int(dlc_id) <= 0:
            continue
        try:
            dlc = fetch_steam_game(dlc_id)
        except Exception as exc:
            errors.append(f'DLC {dlc_id}: {exc}')
            continue
        if not dlc.get('name'):
            continue
        dlc_url = f'https://store.steampowered.com/app/{int(dlc_id)}/'
        linked = _link_catalog_relationship(
            db, root, entity_type='release', media_type='game',
            title=dlc['name'], provider='steam', provider_id=dlc_id,
            source_url=dlc_url,
            metadata={'game_subtype': 'dlc', 'entity_type': 'release',
                      'parent_game_id': str(external_id)},
            relationship='contains', raw_relationship_type='dlc', confidence=0.92,
            policy_key='game_dlc')
        count += int(linked is not None)
    return count, errors


def _refresh_openlibrary_series_graph(db, root):
    matches = search_openlibrary_metadata(root['title'], limit=30)
    target_name = _normalized_catalog_name(root['title'])
    series = next((item for item in matches
                   if item.get('kind') == 'series' and
                   _normalized_catalog_name(item.get('title')) == target_name), None)
    count = 0
    for member in (series or {}).get('books') or []:
        if _review_suspicious_series_member(db, root, member, 'Open Library'):
            continue
        catalog = member.get('catalog_metadata') or {}
        work_id = str(catalog.get('catalog_id') or '').strip()
        title = str(member.get('title') or '').strip()
        if not work_id or not title:
            continue
        source_url = str(member.get('source_url') or
                         ('https://openlibrary.org' + work_id
                          if work_id.startswith('/works/') else ''))
        position = member.get('series_index')
        linked = _link_catalog_relationship(
            db, root, entity_type='work', media_type='book', title=title,
            provider='openlibrary', provider_id=work_id, source_url=source_url,
            metadata={'release_date': _exact_date(member.get('release_date')),
                      'author': member.get('author'),
                      'series_position': position,
                      'position_source': member.get('series_position_source'),
                      'isbns': catalog.get('isbns') or []},
            relationship='contains', raw_relationship_type='series membership',
            confidence=0.9, policy_key='series_members',
            relationship_metadata=({'series_position': position}
                                   if position is not None else None))
        count += int(linked is not None)
    return count


def _openlibrary_author_works(author_id, author_name, work_count=0):
    """Read a bounded Open Library author bibliography, preserving page errors."""
    author_id = str(author_id or '').strip()
    if not re.fullmatch(r'OL\d+A', author_id, flags=re.I):
        raise ValueError('Open Library author ID is malformed')

    def get_page(offset):
        url = (f'https://openlibrary.org/authors/{author_id}/works.json?' +
               urllib.parse.urlencode({'limit': 100, 'offset': offset}))
        raw, final = http_get(url, max_bytes=4_000_000)
        if urllib.parse.urlsplit(final).hostname != 'openlibrary.org':
            raise ValueError('Open Library author works redirected away from its own site')
        payload = json.loads(raw.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('entries'), list):
            raise ValueError('Open Library returned an unexpected author bibliography')
        return payload['entries'], payload

    entries, first_payload = get_page(0)
    try:
        profile_count = int(work_count or 0)
    except (TypeError, ValueError):
        profile_count = 0
    try:
        reported_count = int(first_payload.get('size') or 0)
    except (TypeError, ValueError):
        reported_count = 0
    total = max(len(entries), reported_count, profile_count)
    offsets = list(range(100, min(total, 5000), 100))
    page_errors = []
    if total > 5000:
        page_errors.append('Bibliography exceeds the 5,000-work refresh cap; later works were not loaded')
    if offsets:
        def safe_page(offset):
            try:
                page_entries, _payload = get_page(offset)
                return page_entries, None
            except Exception as exc:
                return [], str(exc)

        with ThreadPoolExecutor(max_workers=min(8, len(offsets))) as pool:
            for page_entries, error in pool.map(safe_page, offsets):
                entries.extend(page_entries)
                if error:
                    page_errors.append(error)

    works = {}
    for entry in entries:
        key = str(entry.get('key') or '') if isinstance(entry, dict) else ''
        title = str(entry.get('title') or '').strip() if isinstance(entry, dict) else ''
        if not title or not re.fullmatch(r'/works/OL\d+W', key):
            continue
        source_url = 'https://openlibrary.org' + key
        release_date = _exact_date(entry.get('first_publish_date'))
        year = re.fullmatch(r'(\d{4})', str(entry.get('first_publish_date') or '').strip())
        if not release_date and year:
            try:
                release_date = dt.date(int(year.group(1)), 1, 1).isoformat()
            except ValueError:
                pass
        works[source_url] = {
            'title': title, 'author': author_name,
            'source_url': source_url, 'release_date': release_date,
        }
    return list(works.values()), page_errors


def _refresh_openlibrary_author_graph(db, root, author_id):
    """Refresh Open Library author credits without adding watched or queue rows."""
    metadata = json.loads(root.get('metadata_json') or '{}')
    catalog = metadata.get('catalog_metadata') or {}
    if str(author_id).startswith('/'):
        author_id = str(author_id).rsplit('/', 1)[-1]
    works, errors = _openlibrary_author_works(
        author_id, root.get('title'), catalog.get('work_count'))
    count = 0
    for member in works:
        path = urllib.parse.urlsplit(member['source_url']).path
        match = re.search(r'/works/(OL\d+W)$', path, flags=re.I)
        work_id = match.group(1) if match else member['source_url']
        linked = _link_catalog_relationship(
            db, root, entity_type='work', media_type='book',
            title=member['title'], provider='openlibrary', provider_id=work_id,
            source_url=member['source_url'],
            metadata={'release_date': member.get('release_date'),
                      'author': member.get('author')},
            relationship='created_by', relationship_from_child=True,
            raw_relationship_type='author works listing', confidence=0.9,
            policy_key='series_members')
        count += int(linked is not None)
    return count, errors


def _review_suspicious_series_member(db, root, member, provider):
    """Keep packaging-shaped catalog members out of accepted series edges."""
    from .series_continuity import series_member_review_reason

    reason = series_member_review_reason(root.get('title'), member)
    if not reason:
        return False
    title = str(member.get('title') or 'Untitled catalog entry')[:200]
    provider_id = str((member.get('catalog_metadata') or {}).get('catalog_id') or
                      member.get('source_url') or title)
    queue_graph_review(
        db, 'uncertain_series_member',
        f'Review possible member of {root.get("title")}: {title}',
        {'series': root.get('title'), 'candidate': member, 'reason': reason,
         'provider': provider}, entity_id=int(root['id']),
        dedupe_key=f'catalog-series-member:{provider}:{root["id"]}:{provider_id}')
    return True


def _refresh_goodreads_series_graph(db, root, source_url=None):
    """Refresh explicit Goodreads series membership without changing watched rows."""
    from .watched import fetch_goodreads

    source_url = str(source_url or root.get('canonical_url') or '').strip()
    if not source_url:
        raise ValueError('Goodreads series record has no source URL to refresh')
    parsed = fetch_goodreads(source_url)
    if parsed.get('kind') != 'series':
        raise ValueError('Goodreads URL did not resolve to a series page')
    count = 0
    for member in parsed.get('books') or []:
        if not isinstance(member, dict) or not member.get('title') or not member.get('source_url'):
            continue
        if _review_suspicious_series_member(db, root, member, 'Goodreads'):
            continue
        path = urllib.parse.urlsplit(str(member['source_url'])).path
        match = re.search(r'/book/show/(\d+)', path, flags=re.I)
        catalog_id = str((member.get('catalog_metadata') or {}).get('catalog_id') or
                         (match.group(1) if match else member['source_url']))
        position = member.get('series_index')
        linked = _link_catalog_relationship(
            db, root, entity_type='work', media_type='book',
            title=member['title'], provider='goodreads', provider_id=catalog_id,
            source_url=member['source_url'],
            metadata={'release_date': _exact_date(member.get('release_date')),
                      'author': member.get('author'), 'series_position': position,
                      'position_source': member.get('series_position_source'),
                      'isbns': ((member.get('catalog_metadata') or
                                 member.get('metadata') or {}).get('isbns') or [])},
            relationship='contains', raw_relationship_type='series listing',
            confidence=0.9, policy_key='series_members',
            relationship_metadata=({'series_position': position}
                                   if position is not None else None))
        count += int(linked is not None)
    collection_error = str(parsed.get('collection_error') or '').strip()
    return count, ([collection_error] if collection_error else [])


def _refresh_goodreads_author_graph(db, root, source_url=None):
    """Refresh Goodreads author credits in the graph without creating watches."""
    from .watched import fetch_goodreads

    source_url = str(source_url or root.get('canonical_url') or '').strip()
    if not source_url:
        raise ValueError('Goodreads author record has no source URL to refresh')
    parsed = fetch_goodreads(source_url)
    if parsed.get('kind') != 'author':
        raise ValueError('Goodreads URL did not resolve to an author page')
    count = 0
    for member in parsed.get('books') or []:
        if not isinstance(member, dict) or not member.get('title') or not member.get('source_url'):
            continue
        path = urllib.parse.urlsplit(str(member['source_url'])).path
        match = re.search(r'/book/show/(\d+)', path, flags=re.I)
        catalog = member.get('catalog_metadata') or member.get('metadata') or {}
        catalog_id = str(catalog.get('catalog_id') or
                         (match.group(1) if match else member['source_url']))
        linked = _link_catalog_relationship(
            db, root, entity_type='work', media_type='book',
            title=member['title'], provider='goodreads', provider_id=catalog_id,
            source_url=member['source_url'],
            metadata={'release_date': _exact_date(member.get('release_date')),
                      'author': member.get('author') or root.get('title'),
                      'isbns': catalog.get('isbns') or []},
            relationship='created_by', relationship_from_child=True,
            raw_relationship_type='author bibliography', confidence=0.9,
            policy_key='series_members')
        count += int(linked is not None)
    collection_error = str(parsed.get('collection_error') or '').strip()
    return count, ([collection_error] if collection_error else [])


def _refresh_google_books_series_graph(db, root):
    """Refresh Google Books series records without creating watches or searches."""
    matches = search_google_books_metadata(root['title'], limit=40)
    target_name = _normalized_catalog_name(root['title'])
    series = next((item for item in matches
                   if item.get('kind') == 'series' and
                   _normalized_catalog_name(item.get('title')) == target_name), None)
    members = list((series or {}).get('books') or [])
    if not members:
        members = [item for item in matches
                   if item.get('kind') == 'book' and
                   _normalized_catalog_name(item.get('series')) == target_name]
    count = 0
    for member in members:
        if not isinstance(member, dict) or not member.get('title'):
            continue
        catalog = member.get('catalog_metadata') or member.get('metadata') or {}
        provider_id = str(catalog.get('catalog_id') or catalog.get('google_books_id') or '').strip()
        source_url = str(member.get('source_url') or '').strip()
        if not provider_id or not source_url:
            continue
        if _review_suspicious_series_member(db, root, member, 'Google Books'):
            continue
        position = member.get('series_index')
        linked = _link_catalog_relationship(
            db, root, entity_type='work', media_type='book',
            title=member['title'], provider='googlebooks', provider_id=provider_id,
            source_url=source_url,
            metadata={'release_date': _exact_date(member.get('release_date')),
                      'author': member.get('author'), 'series_position': position,
                      'position_source': member.get('series_position_source'),
                      'isbns': catalog.get('isbns') or []},
            relationship='contains', raw_relationship_type='seriesInfo',
            confidence=0.88, policy_key='series_members',
            relationship_metadata=({'series_position': position}
                                   if position is not None else None))
        count += int(linked is not None)
    return count


def _refresh_ranobedb_series_graph(db, root, series_id):
    details = ranobedb_series(series_id)
    if str(details.get('id') or series_id) != str(series_id):
        raise ValueError('RanobeDB returned a different series')
    count = 0
    for volume in (details.get('books') or [])[:2000]:
        if not isinstance(volume, dict) or volume.get('id') is None:
            continue
        book_id = str(volume['id'])
        title = str(volume.get('title') or '').strip()
        if not title:
            continue
        release_date = english_release_date(
            (volume.get('c_release_dates') or {}).get('en'))
        source_url = f'https://ranobedb.org/book/{book_id}'
        linked = _link_catalog_relationship(
            db, root, entity_type='work', media_type='book', title=title,
            provider='ranobedb', provider_id=book_id, source_url=source_url,
            metadata={'release_date': release_date,
                      'english_release_date': release_date,
                      'english_official': True if release_date else None,
                      'series_id': str(series_id)},
            relationship='contains', raw_relationship_type='series.books',
            confidence=0.92, policy_key='series_members')
        if linked:
            upsert_media_release(
                db, linked, 'publication', release_date,
                status=('upcoming' if release_date and
                        release_date > dt.date.today().isoformat() else
                        'released' if release_date else 'unknown'),
                provider='RanobeDB', provider_release_id=book_id,
                source_url=source_url,
                metadata={'english_official': True if release_date else None,
                          'series_id': str(series_id)})
            count += 1
    return count


def _normalized_catalog_name(value):
    words = re.findall(r'[a-z0-9]+', str(value or '').casefold())
    if words and words[0] in {'a', 'an', 'the'}:
        words = words[1:]
    return ' '.join(words)


def _exact_date(value):
    text = str(value or '').strip()[:10]
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def diff_graph(db, entity_id, max_depth=3):
    return capture_graph_snapshot(db, int(entity_id), max_depth=max_depth)


def relationship_candidates(db, entity_id, max_depth=3):
    graph = get_media_graph(db, int(entity_id), max_depth=max_depth)
    return [row for row in graph['relationships'] if row.get('status') == 'candidate']
