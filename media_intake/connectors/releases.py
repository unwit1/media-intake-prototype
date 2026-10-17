"""Release-name parsing, magnet links and duplicate grouping for indexer results.

Torrent and Usenet listings name releases like
"Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GROUP". The work title, year,
episode and quality are pulled out so results group under the right work and
people can compare formats without knowing scene naming.
"""
import base64
import re
import urllib.parse

PUBLIC_TRACKERS = (
    'udp://tracker.opentrackr.org:1337/announce',
    'udp://open.stealth.si:80/announce',
    'udp://tracker.torrent.eu.org:451/announce',
    'udp://exodus.desync.com:6969/announce',
)

_RESOLUTION = re.compile(r'\b(2160p|1080p|1080i|720p|576p|480p|4k|uhd)\b', re.I)
_SOURCE = re.compile(r'\b(blu-?ray|bdrip|brrip|bdremux|remux|web-?dl|webrip|web|hdtv|dvdrip|dvd(?:5|9|r)?|'
                     r'hdrip|cam(?:rip)?|telesync|ts|hdcam)\b', re.I)
_CODEC = re.compile(r'\b(x264|x265|h\.?264|h\.?265|hevc|avc|av1|xvid|divx)\b', re.I)
_AUDIO = re.compile(r'\b(aac(?:2\.0)?|ac3|dd(?:p)?5\.1|eac3|dts(?:-hd)?|truehd|atmos|flac|opus|mp3)\b', re.I)
_EPISODE = re.compile(r'\b[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})(?:[-Ee]+(\d{1,3}))?\b|\b(\d{1,2})x(\d{2})\b')
_SEASON = re.compile(r'\b(?:[Ss](\d{1,2})(?![Ee\d])|season[ ._-]?(\d{1,2}))\b', re.I)
_ANIME_EPISODE = re.compile(r'\s-\s(\d{1,4})(?:v\d)?(?=\s|\[|\(|$)')
_YEAR = re.compile(r'(?<![\d])(19[2-9]\d|20[0-4]\d)(?![\d])')
_BRACKETS = re.compile(r'\[[^\]]*\]|\{[^}]*\}')
_EBOOK = re.compile(r'\b(epub|pdf|mobi|azw3|cbz|cbr|djvu|fb2)\b', re.I)
_AUDIOBOOK = re.compile(r'\b(audiobook|m4b|unabridged)\b', re.I)
_GAME_PLATFORM = re.compile(r'\b(pc|windows|switch|nsp|xci|ps[1-5]|psp|ps\s?vita|xbox(?: 360| one)?|wii u?|'
                            r'gamecube|n64|snes|nes|gba|nds|3ds|mac(?:os)?|linux)\b', re.I)
_GAME_FORMATS = re.compile(r'\b(repack|iso|gog|codex|plaza|skidrow|empress|rune|tenoke|elamigos|dodi|fitgirl|'
                           r'multi\d+)\b', re.I)
_SCENE_GROUP = re.compile(r'-([A-Za-z0-9]{2,15})$')
_NOISE = re.compile(r'\b(proper|repack|internal|limited|extended|unrated|dubbed|subbed|multi|dual[ .-]?audio|'
                    r'complete|hdr10?\+?|hdr|dv|dolby ?vision|10bit|8bit|imax|remastered|criterion|yts(?:\.\w+)?|'
                    r'yify|rarbg|eztv|ettv|amzn|nf|dsnp|hmax|atvp|hulu)\b', re.I)


def parse_release(name):
    """Split a release name into its work title and release attributes."""
    raw = ' '.join(str(name or '').split())
    text = raw
    group_match = _SCENE_GROUP.search(text.replace(' ', '.')) if ' ' not in text.strip() else None
    release_group = ''
    leading = re.match(r'^\s*\[([^\]]+)\]', text)
    if leading:
        release_group = leading.group(1).strip()
    elif group_match:
        release_group = group_match.group(1)
    text = _BRACKETS.sub(' ', text)
    text = re.sub(r'\.(?=\S)', ' ', text) if text.count('.') >= 2 or '_' in text else text
    text = text.replace('_', ' ')
    info = {'release_name': raw, 'release_group': release_group}
    cut = len(text)

    def mark(match):
        nonlocal cut
        if match:
            cut = min(cut, match.start())
        return match

    episode = mark(_EPISODE.search(text))
    if episode:
        if episode.group(1):
            info['season'], info['episode'] = int(episode.group(1)), int(episode.group(2))
            if episode.group(3):
                info['episode_end'] = int(episode.group(3))
        else:
            info['season'], info['episode'] = int(episode.group(4)), int(episode.group(5))
    else:
        season = mark(_SEASON.search(text))
        if season:
            info['season'] = int(season.group(1) or season.group(2))
            info['season_pack'] = True
        else:
            anime = _ANIME_EPISODE.search(text)
            if anime and leading:
                mark(anime)
                info['episode'] = int(anime.group(1))
    years = list(_YEAR.finditer(text))
    if years:
        # The last plausible year before quality tags is the release year ("2001 A Space Odyssey 1968").
        candidates = [year for year in years if year.start() > 0] or years
        year = candidates[-1] if len(candidates) > 1 else candidates[0]
        if year.start() > 0:
            mark(year)
            info['year'] = int(year.group(1))
    for key, pattern in (('resolution', _RESOLUTION), ('video_source', _SOURCE), ('codec', _CODEC),
                         ('audio', _AUDIO)):
        found = pattern.search(text)
        if found:
            mark(found)
            info[key] = found.group(1)
        else:
            found = pattern.search(raw.replace('.', ' '))  # bracketed tags: "Sintel (2010) [1080p]"
            if found:
                info[key] = found.group(1)
    for pattern in (_EBOOK, _AUDIOBOOK, _NOISE, _GAME_FORMATS):
        found = pattern.search(text)
        if found and found.start() > 0:
            mark(found)
    ebook = _EBOOK.search(raw)
    if ebook:
        info['file_format'] = ebook.group(1).lower()
    if _AUDIOBOOK.search(raw):
        info['audiobook'] = True
    platform = _GAME_PLATFORM.search(raw)
    if platform:
        info['platform'] = platform.group(1)
    title = text[:cut]
    title = re.sub(r'[\(\[]\s*$', '', title)
    title = re.sub(r'\s+[-–:|]\s*$', '', title.strip())
    title = re.sub(r'\s{2,}', ' ', title).strip(' -._([')
    info['title'] = title or raw
    if info['title'].isupper() and len(info['title']) > 4:
        info['title'] = info['title'].title()
    return info


def quality_label(info, fallback=''):
    """Short human description: '1080p BluRay x265', 'EPUB', 'S01E04 · 720p WEB'."""
    parts = []
    if info.get('season') is not None and info.get('episode') is not None:
        parts.append(f"S{info['season']:02d}E{info['episode']:02d}")
    elif info.get('season') is not None:
        parts.append(f"Season {info['season']}")
    elif info.get('episode') is not None:
        parts.append(f"Episode {info['episode']}")
    quality = ' '.join(str(info[key]) for key in ('resolution', 'video_source', 'codec') if info.get(key))
    if quality:
        parts.append(quality)
    if info.get('file_format'):
        parts.append(info['file_format'].upper())
    if info.get('audiobook'):
        parts.append('Audiobook')
    if info.get('platform'):
        parts.append(info['platform'])
    return ' · '.join(parts) or fallback


def video_subtype(info):
    return 'tv_series' if info.get('season') is not None or info.get('episode') is not None else 'movie'


def normalize_info_hash(value):
    value = str(value or '').strip()
    if re.fullmatch(r'[0-9a-fA-F]{40}', value):
        return value.lower()
    if re.fullmatch(r'[A-Z2-7]{32}', value.upper()):
        return base64.b32decode(value.upper()).hex()
    return ''


def info_hash_from_magnet(magnet):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(magnet or '')).query)
    for value in query.get('xt', []):
        if value.lower().startswith('urn:btih:'):
            return normalize_info_hash(value[9:])
    return ''


def magnet_link(info_hash, name, trackers=None):
    info_hash = normalize_info_hash(info_hash)
    trackers = PUBLIC_TRACKERS if trackers is None else trackers
    if not info_hash:
        raise ValueError('Missing BitTorrent info hash')
    # The xt value stays readable (urn:btih:...): clients and the queue's validation look for it as-is.
    query = f'xt=urn:btih:{info_hash}&dn=' + urllib.parse.quote(str(name or info_hash)[:200], safe='')
    query += ''.join('&tr=' + urllib.parse.quote(tracker, safe='') for tracker in trackers)
    return 'magnet:?' + query


def release_identity(row):
    """A key that is equal for the same release found on different sites."""
    info_hash = row.get('info_hash') or info_hash_from_magnet(row.get('magnet') or row.get('url'))
    if info_hash:
        return 'btih:' + normalize_info_hash(info_hash)
    if row.get('nzb_guid'):
        return 'nzb:' + str(row['nzb_guid']).casefold()
    if row.get('source_id'):
        return f"{row.get('source_key')}:{row['source_id']}"
    return 'url:' + str(row.get('url') or '').casefold()


def dedupe_releases(rows):
    """Merge rows that are the same release (same info hash, NZB or item id).

    The kept row lists every site that carries it in ``mirrors`` and keeps the
    highest seeder count, so a release on four torrent sites shows once.
    """
    kept, by_identity = [], {}
    for row in rows:
        identity = release_identity(row)
        current = by_identity.get(identity)
        if current is None:
            row = dict(row)
            row['mirrors'] = [_mirror(row)]
            by_identity[identity] = row
            kept.append(row)
            continue
        current['mirrors'].append(_mirror(row))
        if (row.get('seeders') or 0) > (current.get('seeders') or 0):
            current['seeders'] = row['seeders']
            current['peers'] = row.get('peers', current.get('peers'))
        for key in ('size', 'year', 'details_url', 'published', 'imdb_id'):
            if not current.get(key) and row.get(key):
                current[key] = row[key]
    for row in kept:
        names = list(dict.fromkeys(mirror['provider'] for mirror in row['mirrors']))
        row['mirror_providers'] = names
        if len(names) > 1:
            row['display_source'] = f"{row.get('display_source') or row.get('provider')} (+{len(names) - 1} more)"
    return kept


def _mirror(row):
    return {'provider': row.get('provider'), 'source_key': row.get('source_key'),
            'url': row.get('url'), 'details_url': row.get('details_url'), 'seeders': row.get('seeders')}
