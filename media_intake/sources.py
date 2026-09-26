"""Source discovery adapters. A result page is metadata until a file URL is resolved."""
import json
import re
import urllib.parse
import html as html_module
from html.parser import HTMLParser
from pathlib import Path
from .core import EXTENSIONS, http_get, validate_item

SITE_PROFILES = {
    'annas': {'base': 'https://annas-archive.pk', 'path': '/search', 'query': 'q', 'detail': r'/md5/[a-f0-9]{32}(?:\b|/)'},
    'libgen': {'base': 'https://libgen.gl', 'path': '/index.php', 'query': 'req', 'detail': r'(?:/ads\.php\?md5=|book/index\.php\?md5=|/main/)[a-f0-9]{32}'},
}


class Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.current = {'href': dict(attrs).get('href', ''), 'text': ''}

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.current is not None:
            self.current['text'] = ' '.join(self.current['text'].split())
            self.links.append(self.current)
            self.current = None


def search_url(site, query, base=None):
    profile = SITE_PROFILES[site]
    host = (base or profile['base']).rstrip('/')
    if urllib.parse.urlsplit(host).scheme != 'https':
        raise ValueError('Site base must use HTTPS')
    return host + profile['path'] + '?' + urllib.parse.urlencode({profile['query']: query})


def parse_site_results(site, html, page_url):
    if site == 'libgen':
        rows = parse_libgen_rows(html, page_url)
        if rows: return rows
    parser = Anchors(); parser.feed(html)
    profile = SITE_PROFILES[site]
    seen, results = set(), []
    for link in parser.links:
        url = urllib.parse.urljoin(page_url, link['href'])
        if not re.search(profile['detail'], url, re.I) or url in seen:
            continue
        seen.add(url)
        results.append({'site': site, 'title': link['text'] or 'Untitled result', 'detail_url': url, 'source': page_url})
    return results


def parse_libgen_rows(page, page_url):
    """Match an edition title with its file link within each result row."""
    found, seen = [], set()
    for row in re.findall(r'<tr\b[^>]*>.*?</tr>', page, flags=re.I | re.S):
        parser = Anchors(); parser.feed(row)
        title_link = next((a for a in parser.links if 'edition.php?id=' in a['href'] and a['text']), None)
        file_link = next((a for a in parser.links if re.search(r'ads\.php\?md5=[a-f0-9]{32}', a['href'], re.I)), None)
        if not title_link or not file_link: continue
        detail = urllib.parse.urljoin(page_url, file_link['href'])
        if detail in seen: continue
        seen.add(detail)
        raw_cells = re.findall(r'<td\b[^>]*>(.*?)</td>', row, flags=re.I | re.S)
        cells = [html_module.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', cell))).strip() for cell in raw_cells]
        title_cell = next((i for i, cell in enumerate(raw_cells) if 'edition.php?id=' in cell), -1)
        author = cells[title_cell + 1].strip(' ,') if 0 <= title_cell < len(cells) - 1 else 'Unknown'
        fmt = re.search(r'<td\b[^>]*>\s*(epub|pdf|mobi|azw3)\s*</td>', row, flags=re.I)
        isbn = re.search(r'\b97[89][0-9]{10}\b', row)
        result = {'site':'libgen', 'title':title_link['text'].strip(), 'detail_url':detail,
                  'source':page_url, 'author': author or 'Unknown'}
        if fmt: result['format'] = '.' + fmt.group(1).lower()
        if isbn: result['isbn'] = isbn.group(0)
        found.append(result)
    return found


def search_site(site, query, base=None, html_path=None):
    url = search_url(site, query, base)
    if html_path:
        html = Path(html_path).read_text(encoding='utf-8')
    else:
        raw, url = http_get(url)
        html = raw.decode('utf-8', errors='replace')
        if 'site unavailable' in html.casefold():
            raise ValueError('Source returned Site Unavailable')
    return parse_site_results(site, html, url)


def resolve_detail(site, page_url, title, author='Unknown', ext=None, html_path=None):
    if site not in SITE_PROFILES and site != 'generic':
        raise ValueError('Unknown site')
    raw, final = (None, page_url) if html_path else http_get(page_url)
    html = Path(html_path).read_text(encoding='utf-8') if html_path else raw.decode('utf-8', errors='replace')
    parser = Anchors(); parser.feed(html)
    results, seen = [], set()
    for link in parser.links:
        url = urllib.parse.urljoin(final, link['href'])
        parsed = urllib.parse.urlsplit(url)
        found_ext = Path(parsed.path).suffix.lower()
        if found_ext not in EXTENSIONS:
            # A site may use an opaque download route. The caller supplies its format.
            if ext not in EXTENSIONS or not re.search(r'(?:download|/get(?:\.php)?|/slow_download)', parsed.path, re.I):
                continue
            found_ext = ext
        if parsed.scheme not in ('https', 'http') or url in seen:
            continue
        seen.add(url)
        results.append(validate_item({'title': title, 'author': author, 'url': url,
                                      'format': found_ext, 'source': final, 'site': site,
                                      'link_text': link['text']}))
    return results


def search_openlibrary(query, limit=10):
    """Search work metadata and resolve listed Internet Archive EPUB/PDF files."""
    params = urllib.parse.urlencode({'q': query, 'fields': 'key,title,author_name,ia', 'limit': min(limit, 25)})
    raw, _ = http_get('https://openlibrary.org/search.json?' + params)
    results = []
    for book in json.loads(raw).get('docs', []):
        for identifier in book.get('ia', [])[:2]:
            if not re.fullmatch(r'[\w.-]{1,128}', identifier): continue
            meta_raw, _ = http_get('https://archive.org/metadata/' + urllib.parse.quote(identifier))
            metadata = json.loads(meta_raw)
            if str(metadata.get('metadata', {}).get('access-restricted-item', '')).lower() == 'true':
                continue
            for file in metadata.get('files', []):
                filename = file.get('name', '')
                ext = Path(filename).suffix.lower()
                if (ext not in ('.epub', '.pdf') or str(file.get('private', '')).lower() == 'true'
                        or 'encrypted' in filename.lower() or '_lcp.' in filename.lower()
                        or 'encrypted' in file.get('format', '').lower()):
                    continue
                url = 'https://archive.org/download/' + urllib.parse.quote(identifier) + '/' + urllib.parse.quote(filename)
                results.append(validate_item({'title': book.get('title') or identifier,
                    'author': ', '.join(book.get('author_name', [])[:3]) or 'Unknown',
                    'url': url, 'format': ext, 'source': 'https://archive.org/details/' + identifier,
                    'source_id': identifier, 'site': 'openlibrary'}))
                break
            if results and results[-1].get('source_id') == identifier: break
    return results
