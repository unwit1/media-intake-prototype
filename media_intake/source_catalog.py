"""Shared source identities, so every entry point names a source the same way.

The GUI, CLI, watched items, the queue and external download manager hand-offs
all need to say which source something came from. This module owns that
vocabulary: one record per source, with its canonical ``source_key``, the
domains and names that resolve to it, and the identities it must NOT be confused
with. Records describe how Media Intake recognises a source. They are not
statements about current availability and never grant permission to acquire a
work.

Matching is deliberately strict. A domain resolves only when it equals a listed
domain or is a subdomain of one (``www.yts.bz``), never on a similar-looking
name. Names resolve on an exact case-insensitive/punctuation-insensitive match.
Names listed under ``distinct_identities`` never resolve.
"""
import json
import re
import urllib.parse
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name('data') / 'source_catalog.json'
SCHEMA_VERSION = 1
_REQUIRED = ('id', 'name', 'source_key', 'site_type', 'coverage', 'categories', 'domains', 'aliases',
             'distinct_identities', 'identity_caveat', 'adapters')


def slug(value):
    """Case-, space- and punctuation-insensitive form used to compare names.

    ``The Pirate Bay``, ``ThePirateBay`` and ``the-pirate-bay`` are one name;
    ``Nyaa`` and ``NyaaTorrents`` stay different."""
    return re.sub(r'[^a-z0-9]+', '', str(value or '').casefold())


def normalize_host(value):
    """Host from a URL, ``host:port`` or bare domain; lower-case, no ``www.``, no trailing dot."""
    text = str(value or '').strip()
    if not text:
        return ''
    if '://' not in text and not text.startswith('//'):
        text = '//' + text
    try:
        host = urllib.parse.urlsplit(text).hostname or ''
    except ValueError:
        return ''
    host = host.casefold().rstrip('.')
    return host[4:] if host.startswith('www.') else host


def _validate(document):
    if not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION:
        raise ValueError(f'source catalog must have schema_version {SCHEMA_VERSION}')
    sources = document.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('source catalog needs a non-empty sources list')
    ids, keys, domains, names = set(), set(), {}, {}
    for record in sources:
        missing = [field for field in _REQUIRED if field not in record]
        if missing:
            raise ValueError(f"source {record.get('id')!r} is missing {', '.join(missing)}")
        for field, seen in (('id', ids), ('source_key', keys)):
            if record[field] in seen:
                raise ValueError(f'duplicate source {field}: {record[field]}')
            seen.add(record[field])
        for domain in record['domains']:
            if domain != normalize_host(domain):
                raise ValueError(f'{record["id"]}: domain {domain!r} must be a lower-case bare domain')
            if domain in domains:
                raise ValueError(f'domain {domain} belongs to both {domains[domain]} and {record["id"]}')
            domains[domain] = record['id']
        for name in (record['id'], record['name'], *record['aliases']):
            owner = names.setdefault(slug(name), record['id'])
            if owner != record['id']:
                raise ValueError(f'name {name!r} belongs to both {owner} and {record["id"]}')
    for record in sources:
        for name in record['distinct_identities']:
            if slug(name) in names:
                raise ValueError(f'{record["id"]} lists {name!r} as distinct but it is also a known name')
    return sources


@lru_cache(maxsize=1)
def _index():
    with open(CATALOG_PATH, encoding='utf-8') as stream:
        sources = _validate(json.load(stream))
    by_name, by_domain, distinct = {}, {}, {}
    for record in sources:
        for name in (record['id'], record['name'], *record['aliases']):
            by_name[slug(name)] = record
        for domain in record['domains']:
            by_domain[domain] = record
        for name in record['distinct_identities']:
            distinct[slug(name)] = record
    return tuple(sources), by_name, by_domain, distinct


def reload():
    """Drop the cached catalog (tests that point CATALOG_PATH elsewhere)."""
    _index.cache_clear()


def records():
    return list(_index()[0])


def find_by_host(value):
    """Record whose domain equals, or is a parent of, the host in ``value`` (URL or domain)."""
    host = normalize_host(value)
    if not host:
        return None
    by_domain = _index()[2]
    while host:
        if host in by_domain:
            return by_domain[host]
        _, _, host = host.partition('.')
    return None


def find_by_name(value):
    """Record whose id, name or documented alias equals ``value`` (exact, case/punctuation-insensitive)."""
    return _index()[1].get(slug(value))


def lookup(value):
    """Resolve a catalog id, name, alias, domain or URL to its record, or None.

    A name that the catalog lists as a distinct identity (for example ``YIFY``
    next to YTS) is never resolved, even when a similar name exists."""
    text = str(value or '').strip()
    if not text:
        return None
    if slug(text) in _index()[3]:
        return None
    return find_by_name(text) or find_by_host(text)


def distinct_from(value):
    """Record that ``value`` is documented as NOT being, or None."""
    return _index()[3].get(slug(value))


def source_key(value):
    """Canonical Media Intake source key for ``value``, or None when the catalog does not know it."""
    record = lookup(value)
    return record['source_key'] if record else None


def resolve_adapters(record):
    """Existing Media Intake adapters named by ``record`` as {role: callable-or-adapter-id}."""
    import importlib
    resolved = {}
    for role, target in (record.get('adapters') or {}).items():
        if role == 'file_host':
            from .file_hosts import FILE_HOSTS
            adapter = next((item for item in FILE_HOSTS.values() if item.id == target), None)
            if adapter is None:
                raise LookupError(f'{record["id"]}: no file host adapter {target!r}')
            resolved[role] = adapter
        else:
            module, _, name = target.rpartition('.')
            resolved[role] = getattr(importlib.import_module(module), name)
    return resolved
