"""Local, editable rights evidence used by source and comic-watch decisions."""
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import urllib.parse


SCHEMA_VERSION = 1
DOWNLOAD_STATUSES = {'public_domain', 'permission_granted'}
WORK_STATUSES = DOWNLOAD_STATUSES | {'restricted', 'not_reviewed'}
DOWNLOAD_SOURCES = (
    # Connectors that can provide a file, a comic chapter, or an updated story.
    'libgen', 'annas_archive', 'opds_feeds', 'json_catalogs', 'irc',
    'mangadex', 'taddy', 'comic_rss_atom', 'webfiction_rss_atom', 'fanficfare',
    'lnori.com', 'novelarchive.cc', 'getcomics.org',
    'public_domain_comic_mirror', 'direct_file_url',
)
METADATA_ONLY_SOURCES = frozenset({
    'openlibrary', 'open library', 'goodreads', 'comicvine', 'comic vine',
    'ranobedb', 'anilist', 'mangaupdates', 'manga updates', 'wikidata',
})


def initial_document():
    observed = '2026-09-26'
    return {
        'schema_version': SCHEMA_VERSION,
        'instructions': (
            'This is the single local source approval file. The sources object lists only connectors '
            'that can provide files or chapters; set approved_for_download to the JSON boolean true '
            'or false for each listed source. Metadata repositories need no download approval. '
            'Per-work approvals are under works.'
        ),
        'sources': {**{
            name: {
                'approved_for_download': False,
                'evidence': [],
                'review_note': 'Not approved. Add evidence and change approved_for_download to true to enable this connector.'
            } for name in DOWNLOAD_SOURCES
        }, **{
            'lnori.com': {
                'approved_for_download': False,
                'evidence': [
                    {
                        'url': 'https://lnori.com/faq',
                        'observed_at': observed,
                        'summary': 'LNORI says light-novel files are sourced from Nyaa, disclaims ownership, and says rights belong to authors, illustrators, and publishers.'
                    },
                    {
                        'url': 'https://lnori.com/terms',
                        'observed_at': observed,
                        'summary': 'Terms effective 2026-01-01 describe a catalog, say referenced works belong to their rights holders, and prohibit redistribution of restricted or licensed content without permission.'
                    }
                ],
                'review_note': 'Evidence does not establish a site-wide public-domain license. Recheck if LNORI publishes an explicit rights statement.'
            },
            'novelarchive.cc': {
                'approved_for_download': False,
                'evidence': [
                    {
                        'url': 'https://novelarchive.cc/',
                        'observed_at': observed,
                        'summary': 'Live catalog showed contemporary serials and current chapter updates. No site-wide public-domain notice or per-work rights marker was found during this review.'
                    }
                ],
                'review_note': 'No rights conclusion is inferred from the absence of a notice. Add affirmative site or work-level evidence before approving automatic downloads.'
            }
        }},
        'works': {}
    }


def _atomic_write(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2) + '\n'
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        Path(temporary).replace(path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _separate_metadata_sources(document):
    """Move obsolete metadata-provider approval rows out of downloadable sources."""
    sources = document.get('sources')
    if not isinstance(sources, dict):
        return False
    moved = {}
    for key in list(sources):
        normalized = str(key or '').strip().casefold().rstrip('.')
        if normalized in METADATA_ONLY_SOURCES:
            moved[key] = sources.pop(key)
    if not moved:
        return False
    repositories = document.setdefault('metadata_repositories', {})
    if not isinstance(repositories, dict):
        repositories = {'legacy_metadata_repositories': repositories}
        document['metadata_repositories'] = repositories
    for key, record in moved.items():
        if key not in repositories:
            repositories[key] = record
        else:
            repositories[f'{key}_legacy_download_setting'] = record
    instructions = str(document.get('instructions') or '')
    if 'single local source approval file.' in instructions and 'Metadata repositories need no download approval.' not in instructions:
        document['instructions'] = initial_document()['instructions']
    return True


def ensure_rights_evidence(path):
    """Create an initial local rights file without overwriting user edits."""
    path = Path(path)
    if not path.exists():
        _atomic_write(path, initial_document())
    else:
        try:
            with path.open('r', encoding='utf-8') as source:
                document = json.load(source)
        except (OSError, json.JSONDecodeError):
            return path
        if (isinstance(document, dict) and document.get('schema_version') == SCHEMA_VERSION
                and isinstance(document.get('sources'), dict)
                and isinstance(document.get('works'), dict)
                and _separate_metadata_sources(document)):
            _atomic_write(path, document)
    return path


def load_rights_evidence(path):
    """Load the single source of truth; malformed edits fail visibly."""
    if not path:
        return {'schema_version': SCHEMA_VERSION, 'sources': {}, 'works': {}}
    path = ensure_rights_evidence(path)
    try:
        with path.open('r', encoding='utf-8') as source:
            document = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'Could not read rights evidence file {path}: {exc}') from exc
    if (not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION
            or not isinstance(document.get('sources'), dict)
            or not isinstance(document.get('works'), dict)):
        raise ValueError(f'Rights evidence file has an unsupported structure: {path}')
    return document


def save_rights_evidence(path, document):
    if (not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION
            or not isinstance(document.get('sources'), dict)
            or not isinstance(document.get('works'), dict)):
        raise ValueError('Rights evidence must contain schema_version 1, sources, and works')
    _atomic_write(path, document)


def source_download_rights(document, source):
    """Return a source record only when its explicit JSON boolean is YES."""
    source = str(source or '').strip().casefold().rstrip('.')
    if source in METADATA_ONLY_SOURCES:
        return None
    record = (document.get('sources') or {}).get(source) or {}
    # This boolean is the single approval switch. Notes are stored for review,
    # but heuristics elsewhere in the app do not replace the explicit choice.
    if record.get('approved_for_download') is True:
        return record
    return None


def source_is_approved(path, source):
    """Check the editable yes/no record; tests and library callers can omit a path."""
    if not path:
        return True
    return source_download_rights(load_rights_evidence(path), source) is not None


def source_key_for_item(item):
    explicit = str(item.get('source_key') or '').strip().casefold()
    if explicit:
        # Old queued catalog rows sometimes carry a metadata provider as their
        # source. A real file reached through one of those rows is governed by
        # the generic direct-file approval, never by metadata-repository rights.
        return 'direct_file_url' if explicit in METADATA_ONLY_SOURCES else explicit
    url = urllib.parse.urlsplit(str(item.get('url') or ''))
    scheme = url.scheme.casefold()
    if scheme == 'irc+xdcc':
        return 'irc'
    if scheme.startswith('fanfic+'):
        return 'fanficfare'
    if scheme.startswith('serial+'):
        return item.get('source_key') or 'webfiction_rss_atom'
    host = (url.hostname or '').casefold()
    if host in {'getcomics.info', 'www.getcomics.info', 'getcomics.org', 'www.getcomics.org'}:
        return 'getcomics.org'
    if host == 'lnori.com':
        return 'lnori.com'
    if host == 'novelarchive.cc':
        return 'novelarchive.cc'
    return 'direct_file_url'


def _safe_http_url(value):
    parts = urllib.parse.urlsplit(str(value or '').strip())
    return parts.scheme in {'http', 'https'} and bool(parts.hostname) and not parts.username and not parts.password


def watch_rights_key(watch):
    """Build a stable work key from provider metadata, falling back to its URL/row ID."""
    metadata = watch.get('metadata') or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    issue = metadata.get('comic_issue') or {}
    comic = metadata.get('comic') or {}
    if issue.get('comicvine_id'):
        return 'comicvine-issue:' + str(issue['comicvine_id'])
    if comic.get('comicvine_id'):
        return 'comicvine-volume:' + str(comic['comicvine_id'])
    if comic.get('mangadex_id'):
        return 'mangadex:' + str(comic['mangadex_id'])
    if comic.get('taddy_id'):
        return 'taddy:' + str(comic['taddy_id'])
    if watch.get('source_url'):
        return 'url:' + str(watch['source_url'])
    return 'watch:' + str(watch.get('id') or '')


def _validated_work_record(status, evidence='', reference='', title='', kind='', updated_at=None):
    status = str(status or '').strip().casefold()
    if status not in WORK_STATUSES:
        raise ValueError('Unknown local rights status')
    evidence = str(evidence or '').strip()[:2000]
    reference = str(reference or '').strip()[:1000]
    if status in DOWNLOAD_STATUSES and not evidence:
        raise ValueError('Add a short evidence note for public-domain or permission status')
    if reference and not _safe_http_url(reference):
        raise ValueError('Evidence reference must be an HTTP or HTTPS URL without credentials')
    return {
        'status': status,
        'approved_for_download': status in DOWNLOAD_STATUSES,
        'evidence': evidence,
        'reference': reference,
        'title': str(title or '').strip()[:300],
        'kind': str(kind or '').strip()[:40],
        'updated_at': updated_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
    }


def set_work_rights(path, watch, status, evidence='', reference=''):
    """Save a per-work rights decision in the shared local evidence document."""
    document = load_rights_evidence(path)
    key = watch_rights_key(watch)
    record = _validated_work_record(status, evidence, reference, watch.get('title'), watch.get('kind'))
    document['works'][key] = record
    save_rights_evidence(path, document)
    return record


def effective_work_rights(document, watch, db=None):
    """Return a watch's exact record, falling back to its parent series record."""
    works = document.get('works') or {}
    key = watch_rights_key(watch)
    record = works.get(key)
    if isinstance(record, dict):
        return {**record, 'rights_work_key': key}
    if db is not None and watch.get('parent_id'):
        parent = db.execute('SELECT * FROM watched_items WHERE id=?', (watch['parent_id'],)).fetchone()
        if parent:
            parent_key = watch_rights_key(dict(parent))
            record = works.get(parent_key)
            if isinstance(record, dict):
                return {**record, 'rights_work_key': parent_key}
    return {}
