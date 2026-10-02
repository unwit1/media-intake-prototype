"""Normalized source-independent records. Optional hierarchy stays optional."""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
import urllib.parse


def canonicalize_url(url):
    value = urllib.parse.urlsplit(str(url).strip())
    if (value.scheme.lower() not in ('http', 'https') or not value.hostname
            or value.username or value.password or any(ord(c) < 32 for c in str(url))):
        raise ValueError('Expected an HTTP(S) URL without credentials')
    host = value.hostname.encode('idna').decode().lower()
    host = '[' + host + ']' if ':' in host else host
    port = value.port
    if port and not (value.scheme.lower() == 'https' and port == 443 or
                     value.scheme.lower() == 'http' and port == 80):
        host += ':' + str(port)
    # Preserve source IDs, signed URLs, repeated query parameters and query order.
    query = [part for part in value.query.split('&') if part and
             not urllib.parse.unquote_plus(part.split('=', 1)[0]).lower().startswith('utm_') and
             urllib.parse.unquote_plus(part.split('=', 1)[0]).lower() not in ('fbclid', 'gclid')]
    return urllib.parse.urlunsplit((value.scheme.lower(), host, value.path or '/',
                                   '&'.join(query), ''))


def identity(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def fingerprint(value):
    return identity(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')))


def natural_key(value):
    return tuple((0, int(part)) if part.isdigit() else (1, part.casefold())
                 for part in re.split(r'(\d+)', str(value)))


@dataclass
class Provenance:
    source_url: str
    provider: str
    evidence: str = ''
    retrieved_at: str | None = None
    confidence: float = 1.0


@dataclass
class WebcomicAsset:
    source_url: str
    ordinal: int
    media_type: str | None = None
    width: int | None = None
    height: int | None = None
    checksum: str | None = None
    archive_path: str | None = None
    retrieved_at: str | None = None
    provenance: dict = field(default_factory=dict)


@dataclass
class WebcomicVolume:
    source_id: str
    title: str
    ordinal: float | None = None
    number: str | None = None
    provenance: dict = field(default_factory=dict)


@dataclass
class WebcomicChapter:
    source_id: str
    title: str
    number: str | None = None
    ordinal: float | None = None
    volume_id: str | None = None
    organization: str = 'SOURCE_DECLARED'
    confidence: float = 1.0
    evidence: list = field(default_factory=list)
    entry_ids: list = field(default_factory=list)


@dataclass
class WebcomicEntry:
    source_id: str
    source_url: str
    title: str
    ordinal: float | None = None
    number: str | None = None
    published_at: str | None = None
    chapter_id: str | None = None
    content_type: str = 'story'
    assets: list[WebcomicAsset] = field(default_factory=list)
    transcript: str | None = None
    notes: str | None = None
    revision: str | None = None
    provenance: dict = field(default_factory=dict)


@dataclass
class WebcomicSeries:
    canonical_id: str
    title: str
    canonical_source_url: str
    platform: str
    alternate_titles: list = field(default_factory=list)
    creators: list = field(default_factory=list)
    description: str | None = None
    tags: list = field(default_factory=list)
    language: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    homepage: str | None = None
    cover_url: str | None = None
    rating: str | None = None
    publication_schedule: str | None = None
    update_frequency: str | None = None
    external_ids: dict = field(default_factory=dict)
    metadata_provenance: dict = field(default_factory=dict)
    content_provenance: dict = field(default_factory=dict)
    copyright: str | None = None
    license: str | None = None
    last_checked_at: str | None = None
    last_successful_update: str | None = None
    extraction_version: str = '1'


@dataclass
class Snapshot:
    series: WebcomicSeries
    entries: list[WebcomicEntry]
    chapters: list[WebcomicChapter] = field(default_factory=list)
    volumes: list[WebcomicVolume] = field(default_factory=list)
    complete: bool = False
    unchanged: bool = False
    cursor: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def record(value):
    return asdict(value)
