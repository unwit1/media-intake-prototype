"""FMHY reading directory as a discovery fallback, separate from site download adapters."""
import json
import re
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from .core import http_get

PAGE_URL = 'https://fmhy.net/reading'
SEED_PATH = Path(__file__).parent / 'data' / 'fmhy-reading-seed.json'
CONTENT_SECTIONS = {'Ebooks', 'Public Domain', 'PDF Search', 'Special Interest',
                    'Light Novels', 'Manga', 'Comics', 'Audiobooks', 'Newspapers', 'Magazines'}


class ReadingIndex(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section = ''
        self.heading = None
        self.heading_text = []
        self.li_depth = 0
        self.links = []
        self.anchor = None
        self.sites = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h2', 'h3'):
            self.heading = tag
            self.heading_text = []
        elif tag == 'li':
            if self.li_depth == 0: self.links = []
            self.li_depth += 1
        elif tag == 'a' and self.li_depth:
            self.anchor = {'url': attrs.get('href', ''), 'name': ''}

    def handle_data(self, data):
        if self.heading: self.heading_text.append(data)
        if self.anchor is not None: self.anchor['name'] += data

    def handle_endtag(self, tag):
        if tag in ('h2', 'h3') and tag == self.heading:
            title = ' '.join(' '.join(self.heading_text).split()).strip('#')
            if title: self.section = title
            self.heading = None
        elif tag == 'a' and self.anchor is not None:
            self.anchor['name'] = ' '.join(self.anchor['name'].split())
            self.links.append(self.anchor)
            self.anchor = None
        elif tag == 'li' and self.li_depth:
            self.li_depth -= 1
            if self.li_depth == 0 and self.section in CONTENT_SECTIONS:
                self._store_group()

    def _store_group(self):
        links = []
        for link in self.links:
            url = urllib.parse.urljoin(PAGE_URL, link['url'])
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in ('http', 'https') or parsed.hostname in ('fmhy.net', 'www.fmhy.net'):
                continue
            links.append({'name': link['name'], 'url': url})
        if not links: return
        primary = next((link for link in links if link['name'] and not link['name'].isdigit()), links[0])
        if not primary['name'] or primary['name'].isdigit(): return
        mirrors = [link['url'] for link in links if link is not primary and (link['name'].isdigit() or link['name'].casefold() == 'mirror')]
        self.sites.append({'name': primary['name'], 'url': primary['url'], 'section': self.section,
                           'mirrors': mirrors, 'index_url': PAGE_URL})


def parse_reading_page(html):
    parser = ReadingIndex(); parser.feed(html)
    seen, sites = set(), []
    for site in parser.sites:
        key = site['url'].rstrip('/').casefold()
        if key not in seen:
            sites.append(site); seen.add(key)
    return sites


def refresh_directory(output, html_path=None):
    if html_path:
        data = Path(html_path).read_text(encoding='utf-8')
    else:
        raw, _ = http_get(PAGE_URL, max_bytes=5_000_000)
        data = raw.decode('utf-8', errors='replace')
    sites = parse_reading_page(data)
    if not sites: raise ValueError('No reading sites found; the FMHY page layout may have changed')
    result = {'source': PAGE_URL, 'captured_at': datetime.now(timezone.utc).isoformat(), 'sites': sites}
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'count': len(sites), 'output': str(Path(output).resolve())}


def load_directory(path=None):
    return json.loads(Path(path or SEED_PATH).read_text(encoding='utf-8'))


def site_search_link(site, query):
    host = urllib.parse.urlsplit(site['url']).hostname or ''
    return 'https://www.google.com/search?' + urllib.parse.urlencode({'q': 'site:' + host + ' ' + query})


def candidates(query, directory=None, limit=15, section=None):
    source = load_directory(directory)
    found = []
    for site in source['sites']:
        if section and site['section'].casefold() != section.casefold(): continue
        found.append({**site, 'search_url': site_search_link(site, query),
                      'query': query, 'directory_source': source['source']})
    return found[:max(0, limit)]


def fallback_search(query, directory=None, opds_feeds=(), sites=(), limit=15):
    """Try preferred configured sources, then suggest FMHY-listed sites for discovery."""
    from .core import search_opds
    from .sources import search_openlibrary, search_site
    matches, errors = [], []
    terms = query.casefold().split()
    def add_results(name, results):
        for result in results:
            haystack = (result.get('title','') + ' ' + result.get('author','')).casefold()
            if all(term in haystack for term in terms):
                matches.append({'provider': name, **result})
    try: add_results('openlibrary', search_openlibrary(query, limit=min(limit, 10)))
    except Exception as exc: errors.append({'provider': 'openlibrary', 'error': str(exc)})
    for feed in opds_feeds:
        try: add_results('opds:' + feed, search_opds(feed, query, pages=2))
        except Exception as exc: errors.append({'provider': 'opds:' + feed, 'error': str(exc)})
    for site in sites:
        try: add_results(site, search_site(site, query))
        except Exception as exc: errors.append({'provider': site, 'error': str(exc)})
    return {'query': query, 'matches': matches[:limit], 'fallback_sites': [] if matches else candidates(query, directory, limit),
            'errors': errors, 'fallback_is_directory': not bool(matches)}
