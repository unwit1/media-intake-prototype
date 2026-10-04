"""Metadata adapters for general video and game catalogs.

These providers return catalog records only. They do not expose download URLs or
bypass the local rights-evidence checks used by download connectors.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .core import open_url

_TMDB_BASE = 'https://api.themoviedb.org/3'
_TVMAZE_BASE = 'https://api.tvmaze.com'
_STEAM_BASE = 'https://store.steampowered.com/api'
_IGDB_BASE = 'https://api.igdb.com/v4'
_TWITCH_TOKEN = 'https://id.twitch.tv/oauth2/token'
_igdb_tokens = {}


def _json_response(url, allowed_hosts, max_bytes=3_000_000, headers=None):
    request = urllib.request.Request(
        url, headers={'User-Agent': 'MediaIntakePrototype/0.1', 'Accept': 'application/json',
                      **(headers or {})})
    with open_url(request, timeout=25) as response:
        raw = response.read(max_bytes + 1)
        final_url = response.geturl()
    if len(raw) > max_bytes:
        raise ValueError('Catalog response exceeded the configured size limit')
    host = (urllib.parse.urlsplit(final_url).hostname or '').casefold()
    if host not in {value.casefold() for value in allowed_hosts}:
        raise ValueError('Catalog redirected to an unexpected host')
    try:
        result = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Catalog returned invalid JSON') from exc
    return result


def _query_url(base, path, params):
    return base.rstrip('/') + '/' + path.lstrip('/') + '?' + urllib.parse.urlencode(params)


def _safe_date(value):
    value = str(value or '').strip()
    if len(value) >= 10:
        value = value[:10]
        try:
            return datetime.strptime(value, '%Y-%m-%d').date().isoformat()
        except ValueError:
            return None
    return None


def _normalized_video(title, provider, provider_id, source_url, subtype,
                      release_date=None, status=None, metadata=None, external_id=None):
    external_provider = provider.casefold()
    if external_provider == 'tmdb':
        external_provider = 'tmdb_movie' if subtype == 'movie' else 'tmdb_tv'
    ids = [{'provider': external_provider, 'external_id': str(provider_id),
            'source_url': source_url}]
    if external_id:
        ids.append(external_id)
    catalog = dict(metadata or {})
    catalog.update({'catalog_provider': provider, 'external_ids': ids,
                    'video_subtype': subtype})
    return {
        'title': str(title or '').strip(), 'author': 'Unknown', 'kind': 'video',
        'entity_type': 'series' if subtype in {'tv_series', 'web_series', 'miniseries'} else 'work',
        'video_subtype': subtype, 'provider': provider,
        'source_url': source_url, 'release_date': release_date,
        'publication_status': status, 'format': 'catalog',
        'catalog_metadata': catalog, 'external_ids': ids,
        'metadata': catalog, 'url': source_url, 'discovery_only': True,
    }


def search_tvmaze(query, limit=12):
    """Search TVMaze's public show catalog and return normalized TV series rows."""
    query = str(query or '').strip()
    if not query or len(query) > 150:
        raise ValueError('Enter a TV search up to 150 characters')
    limit = max(1, min(int(limit), 30))
    url = _query_url(_TVMAZE_BASE, 'search/shows', {'q': query})
    payload = _json_response(url, {'api.tvmaze.com'}, max_bytes=5_000_000)
    results = []
    for match in payload[:limit] if isinstance(payload, list) else []:
        show = match.get('show') if isinstance(match, dict) else None
        if not isinstance(show, dict) or not show.get('id') or not show.get('name'):
            continue
        channel = show.get('webChannel') or show.get('network') or {}
        subtype = 'web_series' if show.get('webChannel') else (
            'miniseries' if str(show.get('type') or '').casefold() == 'miniseries' else 'tv_series')
        status = str(show.get('status') or '')
        first_aired = _safe_date(show.get('premiered'))
        source_url = show.get('url') or f"https://www.tvmaze.com/shows/{show['id']}"
        metadata = {
            'tvmaze_id': str(show['id']), 'video_subtype': subtype,
            'premiere_date': first_aired, 'ended': show.get('ended'),
            'genres': show.get('genres') or [], 'network': channel.get('name'),
            'official_site': show.get('officialSite'), 'status': status,
            'schedule': show.get('schedule') or {}, 'country': (show.get('network') or {}).get('country'),
        }
        row = _normalized_video(show['name'], 'TVMaze', show['id'], source_url,
                                subtype, release_date=first_aired,
                                status=status, metadata=metadata)
        externals = show.get('externals') or {}
        for provider, key in (('imdb', 'imdb'), ('thetvdb', 'thetvdb')):
            if externals.get(key):
                row['external_ids'].append({'provider': provider, 'external_id': str(externals[key])})
        results.append(row)
    return results


def search_tmdb(query, api_key, media_type='movie', limit=12):
    """Search TMDB movies or television; the API key is never persisted here."""
    query = str(query or '').strip()
    api_key = str(api_key or '').strip()
    media_type = str(media_type or '').casefold()
    if media_type not in {'movie', 'tv'}:
        raise ValueError('TMDB media type must be movie or tv')
    if not query or len(query) > 150:
        raise ValueError('Enter a video search up to 150 characters')
    if not api_key:
        return []
    limit = max(1, min(int(limit), 30))
    path = 'search/movie' if media_type == 'movie' else 'search/tv'
    payload = _json_response(_query_url(_TMDB_BASE, path, {
        'query': query, 'include_adult': 'false', 'language': 'en-US',
        'page': 1, 'api_key': api_key,
    }), {'api.themoviedb.org'})
    results = []
    for row in payload.get('results', [])[:limit]:
        title = row.get('title') if media_type == 'movie' else row.get('name')
        provider_id = row.get('id')
        if not title or not provider_id:
            continue
        subtype = 'movie' if media_type == 'movie' else 'tv_series'
        date_value = row.get('release_date') if media_type == 'movie' else row.get('first_air_date')
        status = 'released' if _safe_date(date_value) and _safe_date(date_value) <= datetime.now().date().isoformat() else 'upcoming'
        source_url = f'https://www.themoviedb.org/{media_type}/{provider_id}'
        metadata = {
            'tmdb_id': str(provider_id), 'video_subtype': subtype,
            'overview': row.get('overview') or '', 'original_title': row.get('original_title') or row.get('original_name'),
            'original_language': row.get('original_language'), 'genre_ids': row.get('genre_ids') or [],
            'vote_average': row.get('vote_average'), 'vote_count': row.get('vote_count'),
            'popularity': row.get('popularity'), 'poster_path': row.get('poster_path'),
            'backdrop_path': row.get('backdrop_path'),
        }
        results.append(_normalized_video(title, 'TMDB', provider_id, source_url, subtype,
                                         _safe_date(date_value), status, metadata))
    return results


def fetch_tmdb_media(media_id, media_type, api_key):
    media_id = int(media_id)
    media_type = str(media_type or '').casefold()
    api_key = str(api_key or '').strip()
    if media_id <= 0 or media_type not in {'movie', 'tv'} or not api_key:
        raise ValueError('TMDB ID, media type, and API key are required')
    payload = _json_response(_query_url(_TMDB_BASE, f'{media_type}/{media_id}', {
        'api_key': api_key, 'language': 'en-US'}), {'api.themoviedb.org'})
    if int(payload.get('id') or 0) != media_id:
        raise ValueError('TMDB returned a different media record')
    return payload


def fetch_tmdb_collection(collection_id, api_key):
    collection_id = int(collection_id)
    api_key = str(api_key or '').strip()
    if collection_id <= 0 or not api_key:
        raise ValueError('TMDB collection ID and API key are required')
    payload = _json_response(_query_url(_TMDB_BASE, f'collection/{collection_id}', {
        'api_key': api_key, 'language': 'en-US'}),
        {'api.themoviedb.org'}, max_bytes=4_000_000)
    if int(payload.get('id') or 0) != collection_id:
        raise ValueError('TMDB returned a different collection')
    return payload


def fetch_tmdb_release_dates(movie_id, api_key):
    """Fetch a movie's country-specific theatrical, digital, and physical dates."""
    movie_id = int(movie_id)
    api_key = str(api_key or '').strip()
    if movie_id <= 0 or not api_key:
        raise ValueError('TMDB movie ID and API key are required')
    payload = _json_response(_query_url(_TMDB_BASE, f'movie/{movie_id}/release_dates', {
        'api_key': api_key}), {'api.themoviedb.org'}, max_bytes=5_000_000)
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        raise ValueError('TMDB returned an unexpected movie release-date response')
    return payload['results'][:300]


def fetch_tmdb_season(tv_id, season_number, api_key):
    """Fetch one TMDB TV season with its episode metadata."""
    tv_id = int(tv_id)
    season_number = int(season_number)
    api_key = str(api_key or '').strip()
    if tv_id <= 0 or season_number < 0 or not api_key:
        raise ValueError('TMDB TV ID, non-negative season number, and API key are required')
    payload = _json_response(_query_url(
        _TMDB_BASE, f'tv/{tv_id}/season/{season_number}',
        {'api_key': api_key, 'language': 'en-US'}),
        {'api.themoviedb.org'}, max_bytes=8_000_000)
    if not isinstance(payload, dict):
        raise ValueError('TMDB returned a malformed season')
    returned_number = payload.get('season_number')
    if (not isinstance(payload, dict) or int(payload.get('id') or 0) <= 0 or
            (returned_number is not None and int(returned_number) != season_number) or
            not isinstance(payload.get('episodes'), list)):
        raise ValueError('TMDB returned a different or malformed season')
    return payload


def fetch_tvmaze_show(show_id, include_episodes=False):
    show_id = int(show_id)
    if show_id <= 0:
        raise ValueError('TVMaze show ID must be positive')
    url = f'{_TVMAZE_BASE}/shows/{show_id}'
    if include_episodes:
        url += '?embed[]=episodes&embed[]=seasons'
    payload = _json_response(url, {'api.tvmaze.com'}, max_bytes=8_000_000)
    if not isinstance(payload, dict) or int(payload.get('id') or 0) != show_id:
        raise ValueError('TVMaze returned a different show')
    return payload


def list_tvmaze_seasons(show_id):
    show_id = int(show_id)
    payload = _json_response(f'{_TVMAZE_BASE}/shows/{show_id}/seasons',
                             {'api.tvmaze.com'}, max_bytes=2_000_000)
    if not isinstance(payload, list):
        raise ValueError('TVMaze returned an unexpected season list')
    return payload[:100]


def list_tvmaze_episodes(show_id, include_specials=True, limit=2500):
    show_id = int(show_id)
    path = f'{_TVMAZE_BASE}/shows/{show_id}/episodes'
    if include_specials:
        path += '?specials=1'
    payload = _json_response(path, {'api.tvmaze.com'}, max_bytes=12_000_000)
    if not isinstance(payload, list):
        raise ValueError('TVMaze returned an unexpected episode list')
    return [row for row in payload[:max(1, min(int(limit), 3000))]
            if isinstance(row, dict) and row.get('id')]


def fetch_youtube_playlist_items(playlist_id, api_key, max_items=5000):
    """Fetch a bounded, complete public playlist snapshot from YouTube Data API."""
    playlist_id = str(playlist_id or '').strip()
    api_key = str(api_key or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{5,100}', playlist_id):
        raise ValueError('YouTube playlist ID is malformed')
    if not api_key:
        raise ValueError('YouTube Data API key is not configured')
    max_items = max(50, min(int(max_items), 10000))
    items = []
    page_token = None
    seen_page_tokens = set()
    while len(items) < max_items:
        params = {'part': 'snippet,contentDetails,status', 'playlistId': playlist_id,
                  'maxResults': min(50, max_items - len(items)), 'key': api_key}
        if page_token:
            params['pageToken'] = page_token
        payload = _json_response(_query_url('https://www.googleapis.com/youtube/v3',
                                            'playlistItems', params),
                                 {'www.googleapis.com'}, max_bytes=5_000_000)
        page = payload.get('items') if isinstance(payload, dict) else None
        if not isinstance(page, list):
            raise ValueError('YouTube returned an unexpected playlist response')
        for row in page:
            if not isinstance(row, dict) or not row.get('id'):
                continue
            snippet = row.get('snippet') if isinstance(row.get('snippet'), dict) else {}
            content = row.get('contentDetails') if isinstance(row.get('contentDetails'), dict) else {}
            resource = snippet.get('resourceId') if isinstance(snippet.get('resourceId'), dict) else {}
            video_id = str(resource.get('videoId') or content.get('videoId') or '').strip()
            channel_id = str(snippet.get('videoOwnerChannelId') or '').strip()
            title = str(snippet.get('title') or '').strip()
            metadata = {
                'playlist_item_id': str(row['id']), 'video_id': video_id or None,
                'playlist_id': playlist_id, 'position': snippet.get('position'),
                'channel_id': channel_id or None,
                'channel_title': str(snippet.get('videoOwnerChannelTitle') or '').strip() or None,
                'description': str(snippet.get('description') or '')[:4000],
                'privacy_status': ((row.get('status') or {}).get('privacyStatus')
                                   if isinstance(row.get('status'), dict) else None),
                'published_at': (content.get('videoPublishedAt') or
                                 snippet.get('publishedAt')),
                'thumbnail_url': ((snippet.get('thumbnails') or {}).get('high') or
                                  (snippet.get('thumbnails') or {}).get('default') or {}).get('url'),
            }
            items.append(metadata | {'title': title})
        next_token = str(payload.get('nextPageToken') or '').strip()
        if not next_token:
            page_token = None
            break
        if next_token in seen_page_tokens:
            raise ValueError('YouTube repeated a playlist pagination token')
        seen_page_tokens.add(next_token)
        page_token = next_token

    complete = page_token is None
    schedule_error = None
    try:
        _enrich_youtube_schedule(items, api_key, limit=100)
    except Exception as exc:
        # A video-details outage must not make a valid playlist snapshot partial:
        # removal tracking relies only on the playlistItems endpoint.
        schedule_error = str(exc)[:500]
    chapters = []
    for item in items:
        video_id = item.get('video_id')
        if not video_id:
            continue
        chapters.append({
            'title': item.get('title') or 'YouTube video',
            'url': f'https://www.youtube.com/watch?v={urllib.parse.quote(video_id, safe="")}',
            'published': item.get('scheduled_start_at') or item.get('published_at'),
            'provider_metadata': {key: value for key, value in item.items()
                                  if key != 'title'},
        })
    return {
        'title': f'YouTube playlist {playlist_id}', 'author': 'YouTube',
        'chapters': chapters, 'youtube_playlist_id': playlist_id,
        'youtube_playlist_items': items, 'snapshot_complete': complete,
        'youtube_schedule_error': schedule_error,
    }


def _enrich_youtube_schedule(items, api_key, limit=100):
    """Attach public schedule, duration, and broadcast metadata to playlist entries."""
    limit = max(0, min(int(limit), 500))
    video_ids = list(dict.fromkeys(
        str(item.get('video_id') or '').strip() for item in items
        if isinstance(item, dict) and item.get('video_id')))[:limit]
    by_video_id = {str(item.get('video_id')): item for item in items
                   if isinstance(item, dict) and item.get('video_id')}
    for offset in range(0, len(video_ids), 50):
        batch = video_ids[offset:offset + 50]
        payload = _json_response(_query_url('https://www.googleapis.com/youtube/v3', 'videos', {
            'part': 'snippet,contentDetails,liveStreamingDetails',
            'id': ','.join(batch), 'key': api_key,
        }), {'www.googleapis.com'}, max_bytes=3_000_000)
        rows = payload.get('items') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError('YouTube returned an unexpected video schedule response')
        for row in rows:
            if not isinstance(row, dict):
                continue
            video_id = str(row.get('id') or '')
            snippet = row.get('snippet') if isinstance(row.get('snippet'), dict) else {}
            content_details = (row.get('contentDetails')
                               if isinstance(row.get('contentDetails'), dict) else {})
            live_details = row.get('liveStreamingDetails')
            if not video_id or video_id not in by_video_id:
                continue
            item = by_video_id[video_id]
            duration = str(content_details.get('duration') or '').strip()
            if duration and len(duration) <= 40:
                item['duration'] = duration
            broadcast = str(snippet.get('liveBroadcastContent') or '').strip().casefold()
            if broadcast in {'live', 'upcoming', 'none'}:
                item['live_broadcast_content'] = broadcast
            if not isinstance(live_details, dict):
                continue
            for source_key, metadata_key in (
                    ('scheduledStartTime', 'scheduled_start_at'),
                    ('actualStartTime', 'actual_start_at'),
                    ('actualEndTime', 'actual_end_at')):
                value = str(live_details.get(source_key) or '').strip()
                if value and len(value) <= 50:
                    item[metadata_key] = value


def _igdb_token(client_id, client_secret):
    client_id, client_secret = str(client_id or '').strip(), str(client_secret or '').strip()
    if not client_id or not client_secret:
        return None
    cached = _igdb_tokens.get(client_id)
    if cached and cached[1] > time.monotonic() + 30:
        return cached[0]
    params = urllib.parse.urlencode({'client_id': client_id,
                                     'client_secret': client_secret,
                                     'grant_type': 'client_credentials'})
    request = urllib.request.Request(_TWITCH_TOKEN, data=params.encode('utf-8'), method='POST',
                                     headers={'Accept': 'application/json',
                                              'Content-Type': 'application/x-www-form-urlencoded',
                                              'User-Agent': 'MediaIntakePrototype/0.1'})
    with open_url(request, timeout=25) as response:
        raw, final_url = response.read(250_001), response.geturl()
    if len(raw) > 250_000 or urllib.parse.urlsplit(final_url).hostname != 'id.twitch.tv':
        raise ValueError('Twitch returned an unexpected token response')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('Twitch returned invalid JSON') from exc
    token, expires = payload.get('access_token'), int(payload.get('expires_in') or 0)
    if not token or expires <= 0:
        raise ValueError('Twitch did not return an IGDB access token')
    _igdb_tokens[client_id] = (token, time.monotonic() + expires)
    return token


def search_igdb_games(query, client_id, client_secret, limit=20):
    """Search IGDB through Twitch application credentials, when configured."""
    query = str(query or '').strip()
    if not query or len(query) > 150:
        raise ValueError('Enter a game search up to 150 characters')
    client_id, client_secret = str(client_id or '').strip(), str(client_secret or '').strip()
    if not client_id or not client_secret:
        return []
    token = _igdb_token(client_id, client_secret)
    safe_query = query.replace('\\', ' ').replace('"', ' ').replace(';', ' ').replace('\n', ' ').replace('\r', ' ')
    body = ('search "' + safe_query + '"; fields id,name,slug,url,summary,category,first_release_date,'
            'parent_game.id,parent_game.name,version_parent.id,version_parent.name,'
            'franchises.id,franchises.name,collection.id,collection.name,genres.name,'
            'platforms.name,release_dates.id,release_dates.date,release_dates.platform.name,'
            'release_dates.release_region.name,release_dates.status.name; '
            f'limit {max(1, min(int(limit), 30))};')
    request = urllib.request.Request(
        _IGDB_BASE + '/games', data=body.encode('utf-8'), method='POST',
        headers={'Client-ID': client_id, 'Authorization': 'Bearer ' + token,
                 'Accept': 'application/json', 'Content-Type': 'text/plain; charset=utf-8',
                 'User-Agent': 'MediaIntakePrototype/0.1'})
    with open_url(request, timeout=30) as response:
        raw, final_url = response.read(5_000_001), response.geturl()
    if len(raw) > 5_000_000:
        raise ValueError('IGDB response exceeded the configured size limit')
    if urllib.parse.urlsplit(final_url).hostname != 'api.igdb.com':
        raise ValueError('IGDB redirected to an unexpected host')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('IGDB returned invalid JSON') from exc
    results = []
    categories = {0: 'main_game', 1: 'dlc', 2: 'expansion', 3: 'bundle',
                  4: 'standalone_expansion', 8: 'remake', 9: 'remaster',
                  11: 'port', 13: 'pack'}
    for row in payload if isinstance(payload, list) else []:
        if not row.get('id') or not row.get('name'):
            continue
        try:
            release = datetime.fromtimestamp(int(row['first_release_date']), timezone.utc).date().isoformat()
        except (KeyError, TypeError, ValueError, OSError):
            release = None
        game_type = categories.get(row.get('category'), 'game')
        entity_type = 'collection' if game_type in {'bundle', 'pack'} else (
            'release' if game_type in {'dlc', 'expansion', 'port', 'remaster'} else 'work')
        metadata = {
            'igdb_id': str(row['id']), 'game_subtype': game_type,
            'igdb_category': row.get('category'), 'summary': row.get('summary') or '',
            'franchises': [value.get('name') for value in row.get('franchises') or [] if isinstance(value, dict)],
            'collection': (row.get('collection') or {}).get('name') if isinstance(row.get('collection'), dict) else None,
            'parent_game': (row.get('parent_game') or {}).get('name') if isinstance(row.get('parent_game'), dict) else None,
            'version_parent': (row.get('version_parent') or {}).get('name') if isinstance(row.get('version_parent'), dict) else None,
            'genres': [value.get('name') for value in row.get('genres') or [] if isinstance(value, dict)],
            'platforms': [value.get('name') for value in row.get('platforms') or [] if isinstance(value, dict)],
            'release_dates': row.get('release_dates') or [], 'game_url': row.get('url'),
            'entity_type': entity_type,
        }
        results.append({
            'title': row['name'], 'author': 'Unknown', 'kind': 'game',
            'entity_type': entity_type, 'game_subtype': game_type,
            'provider': 'IGDB', 'source_url': row.get('url') or f"https://www.igdb.com/games/{row.get('slug', row['id'])}",
            'release_date': release, 'publication_status': 'released' if release and release <= datetime.now().date().isoformat() else 'upcoming',
            'format': 'catalog', 'catalog_metadata': metadata, 'metadata': metadata,
            'external_ids': [{'provider': 'igdb', 'external_id': str(row['id']),
                              'source_url': row.get('url')}], 'discovery_only': True,
        })
    return results


def fetch_igdb_game(game_id, client_id, client_secret):
    game_id = int(game_id)
    if game_id <= 0:
        raise ValueError('IGDB game ID must be positive')
    token = _igdb_token(client_id, client_secret)
    if not token:
        raise ValueError('IGDB client credentials are not configured')
    body = ('fields id,name,slug,url,summary,category,first_release_date,status.name,'
            'parent_game.id,parent_game.name,version_parent.id,version_parent.name,'
            'franchises.id,franchises.name,collection.id,collection.name,genres.name,'
            'platforms.name,dlcs.id,dlcs.name,dlcs.slug,expansions.id,expansions.name,expansions.slug,'
            'standalone_expansions.id,standalone_expansions.name,standalone_expansions.slug,'
            'remakes.id,remakes.name,remakes.slug,remasters.id,remasters.name,remasters.slug,'
            'ports.id,ports.name,ports.slug,release_dates.id,release_dates.date,'
            'release_dates.platform.name,release_dates.release_region.name,'
            'release_dates.status.name; where id = ' + str(game_id) + '; limit 1;')
    request = urllib.request.Request(
        _IGDB_BASE + '/games', data=body.encode('utf-8'), method='POST',
        headers={'Client-ID': str(client_id), 'Authorization': 'Bearer ' + token,
                 'Accept': 'application/json', 'Content-Type': 'text/plain; charset=utf-8',
                 'User-Agent': 'MediaIntakePrototype/0.1'})
    with open_url(request, timeout=30) as response:
        raw, final_url = response.read(3_000_001), response.geturl()
    if len(raw) > 3_000_000 or urllib.parse.urlsplit(final_url).hostname != 'api.igdb.com':
        raise ValueError('IGDB returned an unexpected response')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError('IGDB returned invalid JSON') from exc
    if not isinstance(payload, list) or not payload:
        raise ValueError('IGDB did not return that game')
    return payload[0]


_STEAM_EXTRA = re.compile(
    r'\b(?:soundtrack|ost|artbook|art book|wallpapers?|season pass|expansion pass|dlc|'
    r'(?:recruit|costume|skin|weapon|booster|starter|supporter|bonus|content|item|outfit|upgrade|collaboration)'
    r'\s+(?:pack|set)|edition\s+upgrade|upgrade\s+(?:pack|bundle)?\s*$|'
    r'pack\s*\d*$)\b', re.I)
# Re-releases bundling games listed on their own ("Castlevania Dominus Collection",
# "Mega Man X Legacy Collection").
_STEAM_COMPILATION = re.compile(r'\b(?:collection|anthology|trilogy|bundle|compilation)\s*(?:\d+)?\s*$', re.I)
# Any named edition after a separator: "Minecraft: Legacy Console Edition",
# "Far Cry 3: Blood Dragon Classic Edition".
_STEAM_NAMED_EDITION = re.compile(r'\s*[:\u2013-]\s*(?:[\w\'\u2019]+\s+){0,2}edition\s*$', re.I)
_STEAM_EDITION = re.compile(
    r'\s*(?:[-:\u2013]\s*)?(?:\(\d{4}\)\s*)?(?:(?:(?:N7\s+)?digital\s+)?(?:deluxe|enhanced|complete|'
    r'definitive|ultimate|gold|premium|standard|special|collector\'?s|anniversary|windows|'
    r'game of the year|goty|royal|legendary)?\s*edition(?:\s+director\'?s\s+cut)?|'
    r'director\'?s\s+cut)\s*(?:\(\d{4}\))?\s*$', re.I)


def steam_base_title(title):
    """Return ``(base title, edition label)`` for a Steam store name.

    Store names carry trademark marks, release years and edition suffixes
    ("Mass Effect\u2122 2 (2010) Edition", "The Witcher: Enhanced Edition") that
    would otherwise show up as extra games beside the catalog's own entry.
    """
    text = re.sub(r'[\u2122\u00ae\u00a9]', '', str(title or '')).strip()
    edition = None
    named = _STEAM_NAMED_EDITION.search(text)
    if named and named.start() > 0:
        edition, text = named.group(0).strip(' -:\u2013'), text[:named.start()]
    match = _STEAM_EDITION.search(text)
    if match and match.start() > 0:
        edition = match.group(0).strip(' -:\u2013') or None
        text = text[:match.start()]
    text = re.sub(r'\s*\(\d{4}\)\s*$', '', text)
    return re.sub(r'\s+', ' ', text).strip(' -:\u2013') or str(title or '').strip(), edition


def search_steam_games(query, limit=15):
    """Search public Steam catalog as a credential-free game metadata source."""
    query = str(query or '').strip()
    if not query or len(query) > 150:
        raise ValueError('Enter a game search up to 150 characters')
    payload = _json_response(_query_url(_STEAM_BASE, 'storesearch/', {
        'term': query, 'l': 'english', 'cc': 'us'}),
        {'store.steampowered.com'}, max_bytes=4_000_000)
    results = []
    for row in payload.get('items', [])[:max(1, min(int(limit), 30))]:
        if not row.get('id') or not row.get('name'):
            continue
        if _STEAM_EXTRA.search(row['name']) or _STEAM_COMPILATION.search(row['name']):
            continue  # soundtracks, packs, add-ons and compilations are not separate games
        app_id = str(row['id'])
        source_url = f'https://store.steampowered.com/app/{app_id}/'
        title, edition = steam_base_title(row['name'])
        metadata = {'steam_app_id': app_id, 'game_subtype': 'base_game',
                    'tiny_image': row.get('tiny_image'), 'price': row.get('price'),
                    'platforms': row.get('platforms') or {}, 'store_title': row['name'],
                    **({'edition': edition} if edition else {})}
        results.append({
            'title': title, 'author': 'Unknown', 'kind': 'game',
            'entity_type': 'work', 'game_subtype': 'base_game', 'provider': 'Steam',
            'source_url': source_url, 'release_date': None, 'format': 'catalog',
            'catalog_metadata': metadata, 'metadata': metadata,
            'external_ids': [{'provider': 'steam', 'external_id': app_id,
                              'source_url': source_url}], 'discovery_only': True,
        })
    return results


def fetch_steam_game(app_id):
    app_id = str(app_id or '').strip()
    if not app_id.isdigit() or int(app_id) <= 0:
        raise ValueError('Steam app ID must be a positive number')
    payload = _json_response(_query_url(_STEAM_BASE, 'appdetails', {
        'appids': app_id, 'l': 'english', 'cc': 'us'}),
        {'store.steampowered.com'}, max_bytes=5_000_000)
    record = payload.get(app_id) or {}
    data = record.get('data') or {}
    if record.get('success') is not True or not data:
        raise ValueError('Steam has no details for that app ID')
    return data
