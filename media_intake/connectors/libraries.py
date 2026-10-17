"""Direct-file libraries: Z-Library (books), MagzDB (magazines) and Vimm's Lair (game images).

Search rows point at the item's page. The real file address is asked for when the
queue item starts (``resolve_*``), because these sites issue short-lived or
per-account download links. The transfer engine then downloads with resume,
retries and validation like any other direct file.
"""
import re
import urllib.parse

from ..core import EXTENSIONS
from .base import SearchPage, SourceError, human_size, json_body, parse_size, request
from .html import parse_html

ZLIB_FORMATS = {'epub', 'pdf', 'mobi', 'azw3', 'txt', 'cbz', 'cbr', 'djvu', 'fb2'}


def _row(spec, category, *, title, author, file_format, details_url, source_id, size=None, extra=None):
    file_format = '.' + str(file_format or '').lstrip('.').casefold()
    row = {
        'title': title or 'Untitled', 'author': author or 'Unknown', 'category': category,
        'kind': {'Light novels': 'lightnovel', 'Comics': 'comic', 'Manga': 'manga', 'Games': 'game'}.get(
            category, 'book'),
        'url': details_url, 'format': file_format, 'details_url': details_url, 'source': details_url,
        'source_key': spec.key, 'provider': spec.label, 'source_role': 'download', 'downloadable': True,
        'transfer': 'http', 'resolve_connector': spec.key, 'source_id': str(source_id), 'size': size,
        'quality': file_format.lstrip('.').upper(),
        'display_source': ' · '.join(part for part in ('Download', spec.label, file_format.lstrip('.').upper(),
                                                        human_size(size)) if part),
    }
    row.update(extra or {})
    return row


# --------------------------------------------------------------------------- Z-Library
def zlibrary_cookies(spec, config, opener=None):
    """Use saved remix_userid/remix_userkey, or sign in with email and password to get them."""
    user_id, user_key = str(config.get('user_id') or '').strip(), str(config.get('user_key') or '').strip()
    if user_id and user_key:
        return f'remix_userid={user_id}; remix_userkey={user_key}'
    email, password = str(config.get('username') or '').strip(), config.get('password') or ''
    if not email or not password:
        raise SourceError(spec.label, 'searching needs a Z-Library account.', 'config',
                          'Enter your Z-Library email and password (or user ID and key) and your personal '
                          'domain in Settings → Sources.')
    body, _url, _headers = request(spec.label, f"{config['base']}/eapi/user/login",
                                   data={'email': email, 'password': password}, opener=opener)
    data = json_body(spec.label, body)
    user = data.get('user') or {}
    if not data.get('success') or not user.get('remix_userkey'):
        raise SourceError(spec.label, f"sign-in failed: {data.get('error') or 'wrong email or password'}.",
                          'auth', 'Check the account in Settings → Sources.')
    config['user_id'], config['user_key'] = str(user.get('id')), user['remix_userkey']
    return f"remix_userid={user.get('id')}; remix_userkey={user['remix_userkey']}"


def search_zlibrary(spec, query, category, page=1, config=None, opener=None):
    config = config if config is not None else {}
    cookie = zlibrary_cookies(spec, config, opener)
    form = [('message', query), ('limit', '50'), ('page', str(page))]
    form += [('extensions[]', ext) for ext in sorted(ZLIB_FORMATS - {'djvu', 'fb2'})]
    body, _url, _headers = request(spec.label, f"{config['base']}/eapi/book/search",
                                   data=urllib.parse.urlencode(form).encode('utf-8'), opener=opener,
                                   headers={'Cookie': cookie,
                                            'Content-Type': 'application/x-www-form-urlencoded'})
    data = json_body(spec.label, body)
    if not data.get('success'):
        raise _zlib_error(spec, data)
    rows = []
    for book in data.get('books') or []:
        extension = str(book.get('extension') or '').casefold()
        if extension not in ZLIB_FORMATS or '.' + extension not in EXTENSIONS:
            continue  # DjVu and FB2 cannot be validated or opened by the library tools
        details = urllib.parse.urljoin(config['base'] + '/', str(book.get('href') or f"/book/{book.get('id')}"))
        rows.append(_row(spec, category, title=book.get('title'), author=book.get('author'),
                         file_format=extension, details_url=details, source_id=book.get('id'),
                         size=_int(book.get('filesize')),
                         extra={'zlib_hash': book.get('hash'), 'year': _int(book.get('year')),
                                'publisher': book.get('publisher') or '', 'language': book.get('language') or '',
                                'cover_url': book.get('cover') or '', 'isbn': book.get('identifier') or ''}))
    pagination = data.get('pagination') or {}
    total_pages = _int(pagination.get('total_pages')) or page
    return SearchPage(rows, page, page < total_pages, 'direct')


def _zlib_error(spec, data):
    message = str(data.get('error') or data.get('message') or 'unknown error')
    if 'limit' in message.casefold():
        return SourceError(spec.label, 'your daily download limit has been reached.', 'limit',
                           'Z-Library resets it after 24 hours; the queue item can be retried then.')
    if 'auth' in message.casefold() or 'login' in message.casefold():
        return SourceError(spec.label, 'the saved sign-in is no longer valid.', 'auth',
                           'Re-enter your account in Settings → Sources.')
    return SourceError(spec.label, f'it reported: {message}.', 'invalid')


def resolve_zlibrary(spec, item, config=None, opener=None):
    config = config if config is not None else {}
    cookie = zlibrary_cookies(spec, config, opener)
    url = f"{config['base']}/eapi/book/{item['source_id']}/{item.get('zlib_hash')}/file"
    body, _url, _headers = request(spec.label, url, opener=opener, headers={'Cookie': cookie})
    data = json_body(spec.label, body)
    file = data.get('file') or {}
    if not data.get('success') or not file.get('downloadLink'):
        raise _zlib_error(spec, data if not data.get('success') else {'error': 'no download link was offered'})
    if file.get('allowDownload') is False:
        raise SourceError(spec.label, 'this book is not available for download.', 'unsupported',
                          'Choose another edition.')
    extension = '.' + str(file.get('extension') or item['format']).lstrip('.').casefold()
    if extension not in EXTENSIONS:
        raise SourceError(spec.label, f'the file is a {extension.lstrip(".").upper()}, which the library cannot '
                          'check or open.', 'unsupported', 'Choose an EPUB, PDF, MOBI or AZW3 edition.')
    return {'url': file['downloadLink'], 'format': extension}


# --------------------------------------------------------------------------- MagzDB
MAGZ_FORMATS = {'.pdf', '.djvu', '.epub', '.cbz', '.cbr', '.zip', '.rar'}


def search_magzdb(spec, query, category, page=1, config=None, opener=None):
    """MagzDB lists magazine titles; issue rows are read from each title page."""
    base = config['base']
    body, _url, _headers = request(spec.label, f"{base}/makelist?{urllib.parse.urlencode({'t': query})}",
                                   opener=opener)
    tree = parse_html(body)
    journals = []
    for link in tree.find_all('a'):
        match = re.match(r'^/j/(\d+)', link.get('href'))
        if match and link.text():
            journals.append((match.group(1), link.text(), urllib.parse.urljoin(base + '/', link.get('href'))))
    rows = []
    for journal_id, name, url in list(dict.fromkeys(journals))[:5]:
        journal_body, _url, _headers = request(spec.label, url, opener=opener)
        for link in parse_html(journal_body).find_all('a'):
            match = re.match(r'^/num/(\d+)', link.get('href'))
            if not match:
                continue
            issue = link.text() or match.group(1)
            rows.append(_row(spec, category, title=f'{name} {issue}'.strip(), author=name, file_format='.pdf',
                             details_url=urllib.parse.urljoin(base + '/', link.get('href')),
                             source_id=match.group(1),
                             extra={'series': name, 'magazine': name, 'format_guessed': True,
                                    'kind': 'book', 'quality': 'Magazine issue'}))
    if not rows and b'/j/' not in body and b'makelist' not in body:
        raise SourceError(spec.label, 'the search page was not a MagzDB listing.', 'invalid',
                          'The site may be offline or the address may have changed.')
    return SearchPage(rows, page, False, 'direct')


def resolve_magzdb(spec, item, config=None, opener=None):
    body, final_url, _headers = request(spec.label, item['details_url'], opener=opener)
    candidates = []
    for link in parse_html(body).find_all('a'):
        href = urllib.parse.urljoin(final_url, link.get('href'))
        path = urllib.parse.urlsplit(href).path.casefold()
        ext = next((ext for ext in MAGZ_FORMATS if path.endswith(ext)), '')
        if ext == '.djvu':
            continue  # not a format the library can validate or open
        if ext or '/file/' in path or 'download' in link.text().casefold():
            candidates.append((0 if ext == '.pdf' else 1, href, ext or '.pdf'))
    if not candidates:
        raise SourceError(spec.label, 'the issue page has no downloadable file.', 'unsupported',
                          'Some issues are listed without a file; choose another issue.')
    candidates.sort()
    _rank, url, ext = candidates[0]
    return {'url': url, 'format': ext, 'format_guessed': True}


# --------------------------------------------------------------------------- Vimm's Lair
VIMM_SYSTEMS = {
    'nes': 'NES', 'snes': 'SNES', 'n64': 'N64', 'gamecube': 'GameCube', 'wii': 'Wii', 'gb': 'GB',
    'gbc': 'GBC', 'gba': 'GBA', 'ds': 'DS', 'ps1': 'PS1', 'ps2': 'PS2', 'psp': 'PSP', 'genesis': 'Genesis',
    'saturn': 'Saturn', 'dreamcast': 'Dreamcast', 'xbox': 'Xbox',
}


def search_vimm(spec, query, category, page=1, config=None, opener=None):
    if category not in ('Games', None):
        return SearchPage([], page, False, 'direct')
    params = {'p': 'list', 'q': query}
    system = (config or {}).get('system')
    if system:
        params['system'] = system
    base = config['base']
    body, _url, _headers = request(spec.label, f'{base}/vault/?{urllib.parse.urlencode(params)}', opener=opener)
    tree = parse_html(body)
    rows = []
    for tr in tree.find_all('tr'):
        cells = list(tr.find_all('td'))
        link = next((a for a in tr.find_all('a') if re.match(r'^/vault/\d+$', a.get('href'))), None)
        if link is None or not cells:
            continue
        media_id = link.get('href').rsplit('/', 1)[-1]
        texts = [cell.text() for cell in cells if cell.text()]
        platform = ''
        for text in texts:
            if text in VIMM_SYSTEMS.values() or text.casefold() in VIMM_SYSTEMS:
                platform = VIMM_SYSTEMS.get(text.casefold(), text)
        region = next((img.get('title') for img in tr.find_all('img') if img.get('title')), '')
        rows.append(_row(spec, 'Games', title=link.text(), author='Unknown', file_format='.zip',
                         details_url=urllib.parse.urljoin(base + '/', link.get('href')), source_id=media_id,
                         extra={'platform': platform or system or '', 'region': region, 'format_guessed': True,
                                'kind': 'game', 'quality': ' · '.join(part for part in (platform, region) if part)
                                or 'Game image'}))
    if not rows and b'vault' not in body:
        raise SourceError(spec.label, 'the page was not a Vimm\'s Lair listing.', 'invalid',
                          'Check the address in Settings → Sources.')
    return SearchPage(rows, page, False, 'direct')


def resolve_vimm(spec, item, config=None, opener=None):
    body, final_url, _headers = request(spec.label, item['details_url'], opener=opener)
    tree = parse_html(body)
    form = tree.find('form', id='dl_form') or next(
        (form for form in tree.find_all('form') if form.find('input', name='mediaId')), None)
    if form is None:
        text = tree.text().casefold()
        if 'unavailable' in text or 'not available' in text:
            raise SourceError(spec.label, 'downloads of this title are disabled on the site.', 'unsupported',
                              'Vimm\'s Lair removes downloads at publishers\' request; choose another title.')
        raise SourceError(spec.label, 'the game page has no download form.', 'invalid',
                          'The site layout may have changed.')
    media = form.find('input', name='mediaId')
    action = urllib.parse.urljoin(final_url, form.get('action') or '/')
    url = f"{action.split('?')[0]}?{urllib.parse.urlencode({'mediaId': media.get('value')})}"
    size_cell = tree.find('td', id='dl_size')
    resolved = {'url': url, 'request_headers': {'Referer': final_url}, 'format_guessed': True}
    if size_cell is not None and parse_size(size_cell.text()):
        resolved['size_hint'] = parse_size(size_cell.text())
    return resolved


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None

