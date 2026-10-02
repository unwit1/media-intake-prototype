"""Direct file connectors for public and explicitly approved archive sources."""
import json
from html.parser import HTMLParser
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

from .core import (AUDIO_FORMATS, BOOK_FORMATS, COMIC_FORMATS, EXTENSIONS,
                   GAME_FORMATS, GAME_PACKAGE_FORMATS, MAX_FILE_BYTES,
                   SOFTWARE_FORMATS, VIDEO_FORMATS, http_get, open_url,
                   validate_item)


INTERNET_ARCHIVE = 'https://archive.org'
INTERNET_ARCHIVE_MEDIA_TYPES = {
    'Books': 'texts', 'Light novels': 'texts', 'Web serials': 'texts',
    'Fanfiction': 'texts', 'Comics': 'texts', 'Manga': 'texts',
    'Videos': 'movies', 'Anime': 'movies', 'Games': 'software',
}
INTERNET_ARCHIVE_FORMATS = {
    'Books': BOOK_FORMATS | AUDIO_FORMATS,
    'Light novels': BOOK_FORMATS | AUDIO_FORMATS,
    'Web serials': BOOK_FORMATS,
    'Fanfiction': BOOK_FORMATS,
    'Comics': COMIC_FORMATS | {'.pdf'},
    'Manga': COMIC_FORMATS | {'.pdf'},
    'Videos': VIDEO_FORMATS,
    'Anime': VIDEO_FORMATS,
    'Games': GAME_PACKAGE_FORMATS,
}
_IA_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_IA_BOOK_ORDER = {'.epub': 0, '.pdf': 1, '.azw3': 2, '.mobi': 3, '.txt': 4}
_GENERAL_DOWNLOAD_SITES = {
    'meawfy.com': {
        'hosts': {'meawfy.com', 'www.meawfy.com'},
        'base_url': 'https://meawfy.com', 'path_prefix': '',
    },
    'softarchive.download': {
        'hosts': {'softarchive.download', 'www.softarchive.download'},
        'base_url': 'https://softarchive.download/full', 'path_prefix': '/full',
        'search_param': 's', 'provider': 'SoftArchive',
    },
    'animetosho.org': {
        'hosts': {'animetosho.org', 'www.animetosho.org', 'ipv4.animetosho.org'},
        'base_url': 'https://animetosho.org', 'path_prefix': '',
        'search_param': 'q', 'provider': 'Anime Tosho',
    },
    'tokyoinsider.com': {
        'hosts': {'tokyoinsider.com', 'www.tokyoinsider.com'},
        'base_url': 'https://www.tokyoinsider.com', 'path_prefix': '',
        'search_param': 's', 'provider': 'Tokyo Insider',
    },
    'animeout.com': {
        'hosts': {'animeout.com', 'www.animeout.com', 'ddl.animeout.com',
                  'nimbus.animeout.com'},
        'base_url': 'https://ddl.animeout.com', 'path_prefix': '',
        'search_param': 's', 'provider': 'AnimeOut',
    },
    'noobsubs.com': {
        'hosts': {'noobsubs.com', 'www.noobsubs.com'},
        'base_url': 'https://www.noobsubs.com', 'path_prefix': '',
        'search_param': 's', 'provider': 'NoobSubs',
    },
    'chauthanh.info': {
        'hosts': {'chauthanh.info', 'www.chauthanh.info'},
        'base_url': 'https://chauthanh.info', 'path_prefix': '',
        'search_param': 'q', 'provider': 'Chauthanh',
    },
    'mediafiretrend.com': {
        'hosts': {'mediafiretrend.com', 'www.mediafiretrend.com'},
        'base_url': 'https://mediafiretrend.com', 'path_prefix': '',
        'search_path': '/', 'search_param': 'q', 'provider': 'MediaFireTrend',
        'browser_search': 'https://mediafiretrend.com/?q={query}',
    },
    'warezomen.com': {
        'hosts': {'warezomen.com', 'www.warezomen.com'},
        'base_url': 'https://warezomen.com', 'path_prefix': '',
        'search_path': '/search/', 'search_param': 'q',
        'search_static_params': {'m': '1'}, 'provider': 'WarezOmen',
        'browser_search': 'https://warezomen.com/search/?q={query}&m=1',
    },
    'myabandonware.com': {
        'hosts': {'myabandonware.com', 'www.myabandonware.com'},
        'base_url': 'https://www.myabandonware.com', 'path_prefix': '',
        'result_path_prefix': '/game/', 'search_path': '/search/',
        'search_method': 'POST', 'search_param': 'searchValue',
        'provider': 'MyAbandonware', 'browser_search': 'https://www.myabandonware.com/search/',
    },
    'oldgamesdownload.com': {
        'hosts': {'oldgamesdownload.com', 'www.oldgamesdownload.com'},
        'base_url': 'https://oldgamesdownload.com', 'path_prefix': '',
        'result_path_prefix': '/game/', 'search_path': '/',
        'search_param': 's', 'provider': 'OldGamesDownload',
        'browser_search': 'https://oldgamesdownload.com/?s={query}',
    },
    'abandonwaregames.net': {
        'hosts': {'abandonwaregames.net', 'www.abandonwaregames.net'},
        'base_url': 'https://abandonwaregames.net', 'path_prefix': '',
        'result_path_prefix': '/game/', 'search_path': '/',
        'search_param': 's', 'provider': 'AbandonwareGames',
        'browser_search': 'https://abandonwaregames.net/?s={query}',
    },
    'gog-rev.com': {
        'hosts': {'gog-rev.com', 'www.gog-rev.com'},
        'base_url': 'https://gog-rev.com', 'path_prefix': '',
        'search_path': '/search', 'search_param': 'q',
        'provider': 'GOG Revived', 'browser_only': True,
        'browser_search': 'https://gog-rev.com/search?q={query}',
    },
    'ankergames.net': {
        'hosts': {'ankergames.net', 'www.ankergames.net'},
        'base_url': 'https://ankergames.net', 'path_prefix': '',
        'search_path': '/search', 'search_param': 'query',
        'provider': 'AnkerGames', 'browser_search': 'https://ankergames.net/search?query={query}',
    },
}

GENERAL_DOWNLOAD_ADAPTERS = (
    ('mediafiretrend.com', 'MediaFireTrend', frozenset({
        'Books', 'Comics', 'Manga', 'Anime', 'Videos', 'Games', 'Software',
        'Light novels', 'Web serials', 'Fanfiction'})),
    ('xdcc.eu', 'XDCC.eu', frozenset({
        'Books', 'Comics', 'Manga', 'Anime', 'Videos', 'Games', 'Software',
        'Light novels', 'Web serials', 'Fanfiction'})),
    ('warezomen.com', 'WarezOmen', frozenset({
        'Books', 'Comics', 'Manga', 'Anime', 'Videos', 'Games', 'Software',
        'Light novels', 'Web serials', 'Fanfiction'})),
    ('myabandonware.com', 'MyAbandonware', frozenset({'Games'})),
    ('oldgamesdownload.com', 'OldGamesDownload', frozenset({'Games'})),
    ('abandonwaregames.net', 'AbandonwareGames', frozenset({'Games'})),
    ('gog-rev.com', 'GOG Revived', frozenset({'Games'})),
    ('ankergames.net', 'AnkerGames', frozenset({'Games'})),
)

GOOGLE_CSE_ID = '006516753008110874046:1ugcdt3vo7z'


def formats_for_category(category):
    """Return the supported download formats for a dashboard media category."""
    if category == 'Software':
        return set(SOFTWARE_FORMATS)
    try:
        return set(INTERNET_ARCHIVE_FORMATS[category])
    except KeyError as exc:
        raise ValueError(f'Unknown media category: {category}') from exc


FOSSHUB_CATALOG = 'https://www.fosshub.com/software-catalog.json'
SOURCEFORGE_BASE = 'https://sourceforge.net'
GITHUB_API = 'https://api.github.com'
_OSI_GITHUB_LICENSES = frozenset({
    '0BSD', 'AFL-3.0', 'AGPL-3.0', 'Apache-2.0', 'Artistic-2.0',
    'BSD-2-Clause', 'BSD-3-Clause', 'BSD-3-Clause-Clear', 'BSD-4-Clause',
    'BSL-1.0', 'CC0-1.0', 'ECL-2.0', 'EPL-1.0', 'EPL-2.0', 'EUPL-1.1',
    'EUPL-1.2', 'GPL-2.0', 'GPL-3.0', 'ISC', 'LGPL-2.1', 'LGPL-3.0',
    'MIT', 'MPL-2.0', 'OFL-1.1', 'OpenSSL', 'PostgreSQL', 'Unlicense',
    'WTFPL', 'Zlib',
})


def _software_extension(filename):
    name = str(filename or '').split('?', 1)[0].casefold()
    return next((extension for extension in sorted(SOFTWARE_FORMATS,
                                                   key=len, reverse=True)
                 if name.endswith(extension)), '')


def _service_json(url, label, allowed_hosts, max_bytes=4_000_000):
    raw, final_url = http_get(url, max_bytes=max_bytes)
    parts = urllib.parse.urlsplit(final_url)
    if (parts.scheme != 'https' or parts.hostname not in allowed_hosts or
            parts.port not in (None, 443) or parts.username or parts.password):
        raise ValueError(f'{label} redirected outside its approved host')
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} returned invalid JSON') from exc
    if not isinstance(value, (dict, list)):
        raise ValueError(f'{label} returned an unexpected response')
    return value


def search_fosshub_foss(query, limit=20):
    """Search FOSShub's public catalog for entries explicitly described as open source.

    FOSShub's catalog is metadata: current listing pages direct users to publisher
    downloads rather than exposing stable binary URLs.
    """
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    limit = max(1, min(int(limit), 50))
    catalog = _service_json(FOSSHUB_CATALOG, 'FOSShub catalog',
                            {'www.fosshub.com'})
    entries = catalog.get('items') if isinstance(catalog, dict) else None
    if not isinstance(entries, list):
        raise ValueError('FOSShub catalog did not return software entries')
    terms = {term for term in re.findall(r'[a-z0-9]+', query.casefold()) if len(term) > 1}
    matches = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get('name') or '').strip()
        description = str(entry.get('description') or '').strip()
        if not title or not re.search(r'\bopen[ -]?source\b|\bfoss\b', description, re.I):
            continue
        searchable = title + ' ' + description
        found = {term for term in re.findall(r'[a-z0-9]+', searchable.casefold())
                 if len(term) > 1}
        if terms and not terms.intersection(found):
            continue
        product_url = str(entry.get('url') or '')
        parts = urllib.parse.urlsplit(product_url)
        if (parts.scheme != 'https' or parts.hostname != 'www.fosshub.com' or
                parts.port not in (None, 443) or parts.username or parts.password):
            continue
        matches.append({
            'title': title[:300], 'author': 'Unknown',
            'url': product_url, 'source_url': product_url,
            'source': 'https://www.fosshub.com/', 'provider': 'FOSShub',
            'source_key': 'fosshub.com', 'format': 'catalog',
            'source_role': 'metadata', 'discovery_only': True,
            'kind': 'software',
            'catalog_metadata': {
                'description': description[:1200],
                'category': str(entry.get('category') or ''),
                'operating_system': entry.get('operatingSystem') or [],
                'software_version': str(entry.get('softwareVersion') or ''),
                'official_website': str(entry.get('officialWebsite') or ''),
                'open_source_evidence': 'FOSShub catalog description',
            },
        })
    matches.sort(key=lambda item: (
        0 if item['title'].casefold() == query.casefold() else
        1 if item['title'].casefold().startswith(query.casefold()) else 2,
        item['title'].casefold()))
    return matches[:limit]


def _sourceforge_file_url(project, filename):
    filename = str(filename or '').replace('\\', '/')
    parts = [part for part in filename.split('/') if part]
    if (not parts or any(part in {'.', '..'} or any(ord(char) < 32 for char in part)
                         for part in parts)):
        return ''
    path = '/'.join(urllib.parse.quote(part, safe='') for part in parts)
    return f'{SOURCEFORGE_BASE}/projects/{project}/files/{path}/download'


def _sourceforge_release_items(project, project_title, limit=12):
    url = f'{SOURCEFORGE_BASE}/projects/{urllib.parse.quote(project, safe="")}/best_release.json'
    payload = _service_json(url, f'SourceForge release for {project}',
                            {'sourceforge.net'}, max_bytes=1_000_000)
    records = []
    release = payload.get('release')
    if isinstance(release, dict):
        records.append(('default', release))
    platform_releases = payload.get('platform_releases')
    if isinstance(platform_releases, dict):
        records.extend((str(platform), record) for platform, record in platform_releases.items()
                       if isinstance(record, dict))
    results, seen = [], set()
    project_url = f'{SOURCEFORGE_BASE}/projects/{urllib.parse.quote(project, safe="")}/'
    for platform, record in records:
        filename = str(record.get('filename') or '')
        extension = _software_extension(filename)
        download_url = _sourceforge_file_url(project, filename)
        try:
            size = int(record.get('bytes') or 0)
        except (TypeError, ValueError):
            size = 0
        if (not extension or not download_url or size > MAX_FILE_BYTES or
                download_url in seen):
            continue
        seen.add(download_url)
        item = validate_item({
            'title': str(project_title or project)[:240],
            'author': 'Unknown', 'url': download_url, 'format': extension,
            'source': project_url, 'source_url': project_url,
            'provider': 'SourceForge', 'source_key': 'sourceforge.net',
            'source_role': 'download', 'site': 'sourceforge.net',
            'kind': 'software', 'software_project': project,
            'software_package': Path(filename).name,
            'software_platform': platform,
            'software_version': str(record.get('date') or ''),
            'software_size': size or None,
            'software_sha256': str(record.get('sha256sum') or ''),
            'software_license': 'OSI-approved-open-source',
        })
        results.append(item)
        if len(results) >= limit:
            break
    return results


def search_sourceforge_foss(query, limit=20):
    """Search SourceForge's OSI-approved project results and resolve release files."""
    from .sources import Anchors

    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    limit = max(1, min(int(limit), 50))
    search_url = SOURCEFORGE_BASE + '/directory/?' + urllib.parse.urlencode({
        'q': query, 'license': 'osi-approved-open-source',
    })
    raw, final_url = http_get(search_url, max_bytes=4_000_000)
    final_parts = urllib.parse.urlsplit(final_url)
    if (final_parts.scheme != 'https' or final_parts.hostname != 'sourceforge.net' or
            final_parts.port not in (None, 443) or final_parts.username or final_parts.password):
        raise ValueError('SourceForge search redirected outside sourceforge.net')
    parser = Anchors()
    parser.feed(raw.decode('utf-8', errors='replace'))
    projects, seen = [], set()
    for link in parser.links:
        url = urllib.parse.urljoin(final_url, link.get('href') or '')
        parts = urllib.parse.urlsplit(url)
        match = re.fullmatch(r'/projects/([A-Za-z0-9][A-Za-z0-9-]{0,99})/?', parts.path)
        title = str(link.get('text') or '').strip()
        if (parts.scheme != 'https' or parts.hostname != 'sourceforge.net' or
                parts.port not in (None, 443) or parts.username or parts.password or
                not match or not title or title.casefold() in {'see project', 'project home'}):
            continue
        project = match.group(1).casefold()
        if project in seen:
            continue
        seen.add(project)
        projects.append((project, title))
        if len(projects) >= limit:
            break
    results = []
    for project, title in projects:
        try:
            results.extend(_sourceforge_release_items(project, title, limit=limit - len(results)))
        except Exception:
            continue
        if len(results) >= limit:
            break
    return results[:limit]


def search_github_foss(query, limit=20, repo_limit=12):
    """Find GitHub projects with an identified OSI license and release assets."""
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    limit = max(1, min(int(limit), 50))
    repo_limit = max(1, min(int(repo_limit), 25))
    search_url = GITHUB_API + '/search/repositories?' + urllib.parse.urlencode({
        'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': str(repo_limit),
    })
    response = _service_json(search_url, 'GitHub repository search',
                             {'api.github.com'}, max_bytes=4_000_000)
    repositories = response.get('items') if isinstance(response, dict) else None
    if not isinstance(repositories, list):
        raise ValueError('GitHub search did not return repository records')
    results, seen = [], set()
    for repo in repositories:
        if not isinstance(repo, dict) or repo.get('archived') or repo.get('disabled') or repo.get('fork'):
            continue
        license_info = repo.get('license')
        spdx = str((license_info or {}).get('spdx_id') or '') if isinstance(license_info, dict) else ''
        if spdx not in _OSI_GITHUB_LICENSES:
            continue
        name = str(repo.get('full_name') or '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}', name):
            continue
        releases_url = GITHUB_API + '/repos/' + name + '/releases?' + urllib.parse.urlencode({'per_page': '5'})
        releases = _service_json(releases_url, f'GitHub releases for {name}',
                                  {'api.github.com'}, max_bytes=2_000_000)
        if not isinstance(releases, list):
            continue
        repo_title = str(repo.get('name') or name.rsplit('/', 1)[-1])
        repo_url = str(repo.get('html_url') or f'https://github.com/{name}')
        for release in releases:
            if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
                continue
            assets = release.get('assets')
            if not isinstance(assets, list):
                continue
            tag = str(release.get('tag_name') or release.get('name') or '')[:80]
            release_start = len(results)
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                filename = str(asset.get('name') or '')
                extension = _software_extension(filename)
                url = str(asset.get('browser_download_url') or '')
                parts = urllib.parse.urlsplit(url)
                try:
                    size = int(asset.get('size') or 0)
                except (TypeError, ValueError):
                    size = 0
                if (not extension or parts.scheme != 'https' or parts.hostname != 'github.com' or
                        parts.port not in (None, 443) or parts.username or parts.password or
                        size > MAX_FILE_BYTES or url in seen):
                    continue
                seen.add(url)
                results.append(validate_item({
                    'title': repo_title[:300],
                    'author': str((repo.get('owner') or {}).get('login') or 'Unknown')[:300],
                    'url': url, 'format': extension, 'source': str(release.get('html_url') or repo_url),
                    'source_url': repo_url, 'provider': 'GitHub (FOSS)',
                    'source_key': 'github_foss', 'source_role': 'download',
                    'site': 'github.com', 'kind': 'software',
                    'software_project': name, 'software_package': filename,
                    'software_version': tag, 'software_size': size or None,
                    'software_license': spdx,
                    'software_sha256': str(asset.get('digest') or ''),
                    'published_at': str(release.get('published_at') or ''),
                }))
                if len(results) >= limit:
                    return results
            if len(results) > release_start:
                break
    return results


def _archive_host(url):
    parts = urllib.parse.urlsplit(str(url or ''))
    return (parts.scheme == 'https' and parts.hostname in {'archive.org', 'www.archive.org'}
            and parts.port in (None, 443) and not parts.username and not parts.password)


def _json_response(url, label, max_bytes=4_000_000):
    raw, final_url = http_get(url, max_bytes=max_bytes)
    if not _archive_host(final_url):
        raise ValueError(f'{label} redirected outside archive.org')
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{label} returned invalid JSON') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{label} returned an unexpected response')
    return value


def _archive_author(value):
    if isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip()]
        return ', '.join(dict.fromkeys(values[:4])) or 'Unknown'
    value = str(value or '').strip()
    return value[:300] or 'Unknown'


def _archive_file_is_usable(file, allowed_formats):
    if not isinstance(file, dict) or file.get('private') is True:
        return False
    if str(file.get('private') or '').casefold() == 'true':
        return False
    name = str(file.get('name') or '').strip()
    if not name or len(name) > 800 or any(ord(char) < 32 for char in name):
        return False
    extension = Path(name).suffix.casefold()
    description = str(file.get('format') or '').casefold()
    if (extension not in allowed_formats or 'encrypt' in description or
            'encrypt' in name.casefold() or 'lcp' in name.casefold()):
        return False
    try:
        size = int(file.get('size') or 0)
    except (TypeError, ValueError):
        size = 0
    return size <= MAX_FILE_BYTES


def search_internet_archive(query, category='Books', limit=10):
    """Find direct, non-private archive files for one media category.

    Restricted lending files and encrypted formats are omitted. The local rights
    evidence approval is enforced by the caller before this function is reached
    and again by the queue downloader.
    """
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    if category not in INTERNET_ARCHIVE_MEDIA_TYPES:
        raise ValueError(f'Internet Archive does not support the {category!r} category')
    limit = max(1, min(int(limit), 25))
    term = '"' + query.replace('\\', '\\\\').replace('"', '\\"') + '"'
    search_query = f'title:({term}) AND mediatype:{INTERNET_ARCHIVE_MEDIA_TYPES[category]}'
    params = urllib.parse.urlencode([
        ('q', search_query), ('fl[]', 'identifier'), ('fl[]', 'title'),
        ('fl[]', 'creator'), ('fl[]', 'mediatype'), ('rows', str(limit)),
        ('page', '1'), ('output', 'json'),
    ])
    search_url = INTERNET_ARCHIVE + '/advancedsearch.php?' + params
    search = _json_response(search_url, 'Internet Archive search')
    response = search.get('response')
    docs = response.get('docs') if isinstance(response, dict) else None
    if not isinstance(docs, list):
        raise ValueError('Internet Archive search did not return item records')

    allowed = INTERNET_ARCHIVE_FORMATS[category]
    results, seen = [], set()
    for doc in docs[:limit]:
        if not isinstance(doc, dict):
            continue
        identifier = str(doc.get('identifier') or '').strip()
        if not _IA_IDENTIFIER.fullmatch(identifier) or identifier in seen:
            continue
        seen.add(identifier)
        metadata_url = INTERNET_ARCHIVE + '/metadata/' + urllib.parse.quote(identifier, safe='')
        metadata = _json_response(metadata_url, f'Internet Archive item {identifier}')
        item_metadata = metadata.get('metadata')
        files = metadata.get('files')
        if not isinstance(item_metadata, dict) or not isinstance(files, list):
            continue
        restricted = str(item_metadata.get('access-restricted-item') or '').casefold()
        if restricted in {'true', '1', 'yes'}:
            continue
        candidates = [file for file in files if _archive_file_is_usable(file, allowed)]
        if category in {'Books', 'Light novels', 'Web serials', 'Fanfiction'}:
            candidates.sort(key=lambda file: (
                _IA_BOOK_ORDER.get(Path(str(file.get('name') or '')).suffix.casefold(), 50),
                str(file.get('name') or '').casefold()))
        else:
            candidates.sort(key=lambda file: str(file.get('name') or '').casefold())
        if not candidates:
            continue
        filename = str(candidates[0]['name'])
        file_url = (INTERNET_ARCHIVE + '/download/' + urllib.parse.quote(identifier, safe='') +
                    '/' + urllib.parse.quote(filename, safe=''))
        title = str(doc.get('title') or item_metadata.get('title') or identifier).strip()
        author = _archive_author(doc.get('creator') or item_metadata.get('creator'))
        result = validate_item({
            'title': title[:300], 'author': author,
            'url': file_url, 'format': Path(filename).suffix.casefold(),
            'source': INTERNET_ARCHIVE + '/details/' + urllib.parse.quote(identifier, safe=''),
            'provider': 'Internet Archive', 'source_key': 'internet_archive',
            'source_role': 'download', 'site': 'internet_archive',
            'archive_identifier': identifier, 'archive_filename': filename,
        })
        if file_url not in {item.get('url') for item in results}:
            results.append(result)
    return results


def _general_site_host(url, source_key):
    parts = urllib.parse.urlsplit(str(url or ''))
    profile = _GENERAL_DOWNLOAD_SITES.get(source_key) or {}
    return (parts.scheme == 'https' and parts.hostname in profile.get('hosts', set()) and
            parts.port in (None, 443) and not parts.username and not parts.password)


def _post_site_form(url, fields, max_bytes=4_000_000):
    payload = urllib.parse.urlencode(fields).encode('utf-8')
    request = urllib.request.Request(
        url, data=payload, method='POST',
        headers={'User-Agent': 'MediaIntakePrototype/0.1 (personal library intake)',
                 'Content-Type': 'application/x-www-form-urlencoded'})
    with open_url(request, timeout=30) as response:
        size = response.headers.get('Content-Length')
        if size and int(size) > max_bytes:
            raise ValueError('Response exceeds size limit')
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError('Response exceeds size limit')
        return data, response.geturl()


def _site_search_request(query, source_key, base_url=None):
    profile = _GENERAL_DOWNLOAD_SITES[source_key]
    base_url = str(base_url or profile['base_url']).strip()
    base_parts = urllib.parse.urlsplit(base_url)
    search_path = profile.get('search_path')
    if search_path is not None:
        search_url = urllib.parse.urlunsplit((base_parts.scheme, base_parts.netloc,
                                              search_path, '', ''))
    else:
        search_url = urllib.parse.urlunsplit((base_parts.scheme, base_parts.netloc,
                                              base_parts.path, base_parts.query, ''))
    fields = dict(profile.get('search_static_params') or {})
    fields[profile.get('search_param') or 's'] = query
    if str(profile.get('search_method') or 'GET').upper() == 'POST':
        return _post_site_form(search_url, fields), search_url
    parts = urllib.parse.urlsplit(search_url)
    old_params = [(key, value) for key, value in urllib.parse.parse_qsl(
        parts.query, keep_blank_values=True)
        if key.casefold() not in {'s', 'q', 'query', 'search', 'searchvalue'}]
    params = old_params + list(fields.items())
    search_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                          urllib.parse.urlencode(params), ''))
    return http_get(search_url, max_bytes=4_000_000), search_url


def _site_search_result(query, source_key, category=None, access_note=''):
    profile = _GENERAL_DOWNLOAD_SITES[source_key]
    template = profile.get('browser_search') or profile['base_url']
    url = template.format(query=urllib.parse.quote_plus(str(query or '').strip()))
    result = {
        'title': f"Open {profile.get('provider') or source_key} search for {str(query or '').strip()}",
        'author': profile.get('provider') or source_key,
        'format': 'link', 'url': url, 'source': profile['base_url'],
        'source_url': url, 'provider': profile.get('provider') or source_key,
        'source_key': source_key, 'source_role': 'metadata',
        'discovery_only': True, 'kind': 'game' if category == 'Games' else None,
    }
    if category:
        result['category'] = category
    if access_note:
        result['catalog_metadata'] = {'note': access_note}
    return result


def _search_general_download_site(query, source_key, base_url=None, limit=20):
    """Search a site's HTML listing and return same-site detail-page references."""
    from .sources import Anchors

    profile = _GENERAL_DOWNLOAD_SITES.get(source_key)
    if not profile:
        raise ValueError('Unknown general-purpose download site')
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    base_url = str(base_url or profile['base_url']).strip()
    parts = urllib.parse.urlsplit(base_url)
    base_path = parts.path.rstrip('/')
    base_prefix = profile.get('base_path_prefix', profile['path_prefix'])
    if (not _general_site_host(base_url, source_key) or
            (base_prefix and
             not (base_path == base_prefix or
                  base_path.startswith(base_prefix + '/')))):
        expected = profile['base_url']
        raise ValueError(f'{source_key} base must use {expected}')
    (raw, final_url), search_url = _site_search_request(query, source_key, base_url)
    if not _general_site_host(final_url, source_key):
        raise ValueError(f'{source_key} search redirected outside its site')
    parser = Anchors()
    parser.feed(raw.decode('utf-8', errors='replace'))
    query_terms = {term for term in re.findall(r'[a-z0-9]+', query.casefold()) if len(term) > 1}
    results, seen = [], set()
    for link in parser.links:
        url = urllib.parse.urljoin(final_url, link.get('href') or '')
        target = urllib.parse.urlsplit(url)
        path = target.path.rstrip('/')
        title = str(link.get('text') or '').strip()
        result_prefix = profile.get('result_path_prefix', profile['path_prefix'])
        in_scope = (path.startswith(result_prefix.rstrip('/') + '/')
                    if result_prefix else path not in {'', '/'})
        if (not _general_site_host(url, source_key) or
                target.hostname not in profile['hosts'] or not in_scope or path == base_path or
                Path(path).suffix or not title or url in seen):
            continue
        title_terms = set(re.findall(r'[a-z0-9]+', title.casefold()))
        if query_terms and not (query_terms & title_terms):
            continue
        seen.add(url)
        results.append({
            'provider': profile.get('provider') or
                        ('Meawfy' if source_key == 'meawfy.com' else 'SoftArchive'),
            'source_key': source_key,
            'title': title[:300], 'author': 'Unknown', 'format': 'link',
            'url': url, 'source': final_url, 'source_url': url,
            'detail_url': url, 'source_role': 'metadata', 'discovery_only': True,
        })
        if len(results) >= max(1, min(int(limit), 50)):
            break
    return results


def search_site_downloads(query, source_key, limit=20, category=None):
    """Search a registered source; return a browser link when its HTTP page is gated."""
    if source_key not in _GENERAL_DOWNLOAD_SITES:
        raise ValueError('Unknown general-purpose download site')
    profile = _GENERAL_DOWNLOAD_SITES[source_key]
    if profile.get('browser_only'):
        return [_site_search_result(query, source_key, category,
                                    'This site loads search results in its browser interface.')]
    try:
        return _search_general_download_site(query, source_key, limit=limit)
    except urllib.error.HTTPError as exc:
        if exc.code not in {401, 403, 429, 503}:
            raise
        return [_site_search_result(
            query, source_key, category,
            f'The site returned HTTP {exc.code} to the app. Open its search page in your browser.')]


def resolve_site_downloads(item, source_key, allowed_formats=None):
    return _resolve_general_download_site(item, source_key, allowed_formats)


class _XdccTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.anchor = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tr':
            self.row = []
        elif tag in {'td', 'th'} and self.row is not None:
            self.cell = {'text': '', 'href': ''}
        elif tag == 'a' and self.cell is not None:
            self.cell['href'] = attrs.get('href') or ''
            self.anchor = True

    def handle_data(self, data):
        if self.cell is not None:
            self.cell['text'] += data

    def handle_endtag(self, tag):
        if tag in {'td', 'th'} and self.cell is not None and self.row is not None:
            self.cell['text'] = ' '.join(self.cell['text'].split())
            self.row.append(self.cell)
            self.cell = None
            self.anchor = False
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def _xdcc_size_bytes(value):
    match = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)i?B?\s*',
                         str(value or ''), re.I)
    if not match:
        return 0
    power = {'': 0, 'K': 1, 'M': 2, 'G': 3, 'T': 4}[match.group(2).upper()]
    return int(float(match.group(1)) * (1024 ** power))


def search_xdcc_eu(query, limit=30, allowed_formats=None, category=None):
    """Turn XDCC.eu result rows into queueable IRC pack records."""
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    limit = max(1, min(int(limit), 100))
    params = urllib.parse.urlencode({'searchkey': query})
    search_url = 'https://www.xdcc.eu/search.php?' + params
    raw, final_url = http_get(search_url, max_bytes=8_000_000)
    parts = urllib.parse.urlsplit(final_url)
    if (parts.scheme != 'https' or parts.hostname not in {'xdcc.eu', 'www.xdcc.eu'} or
            parts.port not in (None, 443) or parts.username or parts.password):
        raise ValueError('XDCC.eu search redirected outside xdcc.eu')
    parser = _XdccTable()
    parser.feed(raw.decode('utf-8', errors='replace'))
    headers, results, seen = None, [], set()
    for row in parser.rows:
        values = [cell['text'] for cell in row]
        if not headers and {value.casefold() for value in values} >= {
                'network', 'channel', 'bot', 'name'}:
            headers = {}
            for index, value in enumerate(values):
                match = re.search(r'[a-z]+', value.casefold())
                if match:
                    headers[match.group(0)] = index
            continue
        if (not headers or not {'network', 'channel', 'bot', 'slot', 'size', 'name'} <= set(headers)
                or len(values) <= max(headers.values())):
            continue
        get = lambda key: row[headers[key]]
        filename = get('name')['text']
        extension = Path(filename).suffix.casefold()
        if (extension not in EXTENSIONS or
                (allowed_formats is not None and extension not in set(allowed_formats))):
            continue
        terms = {term for term in re.findall(r'[a-z0-9]+', query.casefold()) if len(term) > 1}
        title_terms = set(re.findall(r'[a-z0-9]+', filename.casefold()))
        if terms and not terms.intersection(title_terms):
            continue
        network, channel, bot, slot = (get('network')['text'], get('channel'),
                                       get('bot')['text'], get('slot')['text'])
        channel_url = urllib.parse.urlsplit(channel.get('href') or '')
        if (channel_url.scheme not in {'irc', 'ircs'} or not channel_url.hostname or
                not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', channel_url.hostname) or
                channel_url.username or channel_url.password):
            continue
        channel_name = urllib.parse.unquote(channel_url.path.strip('/').split('/', 1)[0])
        if not re.fullmatch(r'[A-Za-z0-9_\-\[\]{}^|`]+', channel_name):
            continue
        pack_match = re.search(r'\d{1,8}', slot)
        if not pack_match or not re.fullmatch(r'[\w\-\[\]{}^|`]+', bot):
            continue
        size = _xdcc_size_bytes(get('size')['text'])
        if size > MAX_FILE_BYTES:
            continue
        server = channel_url.hostname.casefold()
        channel_value = '#' + channel_name
        port = channel_url.port or (6697 if channel_url.scheme == 'ircs' else 6667)
        tls = channel_url.scheme == 'ircs'
        pack = pack_match.group(0)
        config = {
            'server': server, 'port': port, 'tls': tls, 'nick': 'MediaIntake',
            'channel': channel_value, 'bot': bot, 'pack': pack,
            'network': network, 'source_name': 'XDCC.eu', 'source_key': 'xdcc.eu',
        }
        stable_url = ('irc+xdcc://' + server + '/' + urllib.parse.quote(channel_value, safe='') +
                      '/' + urllib.parse.quote(bot.lower(), safe='') + '/' + pack)
        if stable_url in seen:
            continue
        seen.add(stable_url)
        title = Path(filename).stem.replace('.', ' ').replace('_', ' ').strip()[:300]
        item = {
            'title': title or filename[:300], 'author': 'Unknown',
            'format': extension, 'url': stable_url, 'source': final_url,
            'source_url': final_url, 'provider': 'XDCC.eu', 'source_key': 'xdcc.eu',
            'source_role': 'download', 'downloadable': True, 'site': 'xdcc.eu',
            'filename': filename, 'filesize': size or None, 'kind': 'game' if category == 'Games' else None,
            'irc': config,
        }
        if category:
            item['category'] = category
        results.append(item)
        if len(results) >= limit:
            break
    return results


def search_registered_download_site(query, source_key, limit=20, category=None,
                                    allowed_formats=None):
    if source_key == 'xdcc.eu':
        return search_xdcc_eu(query, limit=limit,
                              allowed_formats=allowed_formats, category=category)
    return search_site_downloads(query, source_key, limit=limit, category=category)


def search_google_cse_browser(query, category=None):
    """Return the supplied FMHY Google CSE query as a browser-only search result."""
    query = str(query or '').strip()
    if not query or len(query) > 150 or any(ord(char) < 32 for char in query):
        raise ValueError('Enter a search up to 150 characters')
    url = 'https://cse.google.com/cse?' + urllib.parse.urlencode({
        'cx': GOOGLE_CSE_ID, 'q': query,
    }) + '#gsc.tab=0&gsc.q=' + urllib.parse.quote(query)
    result = {
        'title': f'Open Google CSE search for {query}', 'author': 'FMHY search',
        'format': 'link', 'url': url, 'source': 'https://cse.google.com/',
        'source_url': url, 'provider': 'Google CSE (FMHY)',
        'source_role': 'metadata', 'discovery_only': True,
    }
    if category:
        result.update(category=category, kind='game' if category == 'Games' else None)
    return [result]


def search_softarchive(query, base_url='https://softarchive.download/full', limit=20):
    """Read matching archive entries from SoftArchive's /full listing."""
    return _search_general_download_site(query, 'softarchive.download', base_url, limit)


def search_meawfy(query, base_url='https://meawfy.com', limit=20):
    """Read matching entry pages from Meawfy's public site search."""
    return _search_general_download_site(query, 'meawfy.com', base_url, limit)


def _resolve_general_download_site(item, source_key, allowed_formats=None):
    """Resolve direct file links from a general-purpose source detail page."""
    from .sources import Anchors
    from .file_hosts import resolve_hosted_file

    profile = _GENERAL_DOWNLOAD_SITES.get(source_key)
    if not profile:
        raise ValueError('Unknown general-purpose download site')
    detail_url = str((item or {}).get('detail_url') or (item or {}).get('url') or '')
    if not _general_site_host(detail_url, source_key):
        raise ValueError(f'{source_key} detail page must use its HTTPS host')
    raw, final_url = http_get(detail_url, max_bytes=4_000_000)
    if not _general_site_host(final_url, source_key):
        raise ValueError(f'{source_key} detail page redirected outside its site')
    parser = Anchors()
    parser.feed(raw.decode('utf-8', errors='replace'))
    allowed_formats = set(allowed_formats or EXTENSIONS)
    results, seen = [], set()
    for link in parser.links:
        url = urllib.parse.urljoin(final_url, link.get('href') or '')
        target = urllib.parse.urlsplit(url)
        extension = Path(target.path).suffix.casefold()
        if (target.scheme != 'https' or not target.hostname or target.username or target.password or
                target.port not in (None, 443) or url in seen):
            continue
        seen.add(url)
        provider = profile.get('provider') or (
            'Meawfy' if source_key == 'meawfy.com' else 'SoftArchive')
        try:
            hosted = resolve_hosted_file(url, allowed_formats & EXTENSIONS)
        except (ValueError, OSError) as exc:
            item.setdefault('connector_warnings', []).append(str(exc))
            continue
        if hosted is not None:
            for file in hosted:
                results.append(validate_item({
                    'title': str(item.get('title') or file.filename)[:300],
                    'author': str(item.get('author') or 'Unknown')[:300],
                    'url': file.url, 'format': file.format, 'source': final_url,
                    'provider': provider, 'source_key': source_key,
                    'source_role': 'download', 'site': source_key,
                    'file_host': file.host, 'file_host_share_url': file.share_url,
                    'source_filename': file.filename, 'expected_size': file.size,
                    'expected_sha256': file.sha256,
                }))
            continue
        if extension not in allowed_formats or extension not in EXTENSIONS:
            continue
        results.append(validate_item({
            'title': str((item or {}).get('title') or
                         ('Meawfy file' if source_key == 'meawfy.com' else 'SoftArchive file'))[:300],
            'author': str((item or {}).get('author') or 'Unknown')[:300],
            'url': url, 'format': extension, 'source': final_url,
            'provider': provider, 'source_key': source_key,
            'source_role': 'download', 'site': source_key,
        }))
    return results


def resolve_softarchive_downloads(item, allowed_formats=None):
    """Resolve direct media-file links from a SoftArchive detail page."""
    return _resolve_general_download_site(item, 'softarchive.download', allowed_formats)


def resolve_meawfy_downloads(item, allowed_formats=None):
    """Resolve direct media-file links from a Meawfy detail page."""
    return _resolve_general_download_site(item, 'meawfy.com', allowed_formats)


def search_anime_tosho(query, base_url='https://animetosho.org', limit=20):
    """Search Anime Tosho listings for direct anime video links."""
    return _search_general_download_site(query, 'animetosho.org', base_url, limit)


def resolve_anime_tosho_downloads(item, allowed_formats=None):
    return _resolve_general_download_site(item, 'animetosho.org', allowed_formats)


def search_tokyo_insider(query, base_url='https://www.tokyoinsider.com', limit=20):
    """Search Tokyo Insider listings for direct anime video links."""
    return _search_general_download_site(query, 'tokyoinsider.com', base_url, limit)


def resolve_tokyo_insider_downloads(item, allowed_formats=None):
    return _resolve_general_download_site(item, 'tokyoinsider.com', allowed_formats)


def search_animeout(query, base_url='https://ddl.animeout.com', limit=20):
    """Search AnimeOut listings for direct anime video links."""
    return _search_general_download_site(query, 'animeout.com', base_url, limit)


def resolve_animeout_downloads(item, allowed_formats=None):
    return _resolve_general_download_site(item, 'animeout.com', allowed_formats)


def search_noobsubs(query, base_url='https://www.noobsubs.com', limit=20):
    """Search NoobSubs listings for direct anime video links."""
    return _search_general_download_site(query, 'noobsubs.com', base_url, limit)


def resolve_noobsubs_downloads(item, allowed_formats=None):
    return _resolve_general_download_site(item, 'noobsubs.com', allowed_formats)


def search_chauthanh(query, base_url='https://chauthanh.info', limit=20):
    """Search Chauthanh listings for direct anime video links."""
    return _search_general_download_site(query, 'chauthanh.info', base_url, limit)


def resolve_chauthanh_downloads(item, allowed_formats=None):
    return _resolve_general_download_site(item, 'chauthanh.info', allowed_formats)


ANIME_DOWNLOAD_ADAPTERS = (
    ('animetosho.org', 'Anime Tosho', search_anime_tosho,
     resolve_anime_tosho_downloads),
    ('tokyoinsider.com', 'Tokyo Insider', search_tokyo_insider,
     resolve_tokyo_insider_downloads),
    ('animeout.com', 'AnimeOut', search_animeout, resolve_animeout_downloads),
    ('noobsubs.com', 'NoobSubs', search_noobsubs, resolve_noobsubs_downloads),
    ('chauthanh.info', 'Chauthanh', search_chauthanh,
     resolve_chauthanh_downloads),
)
