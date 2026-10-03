"""Display normalization and Goodreads export parsing for the local library."""
import csv
import html
import re
from collections import Counter
from pathlib import Path


def _author_tokens(value):
    return tuple(sorted(set(re.findall(r"[^\W_]+", str(value or '').casefold(), flags=re.UNICODE))))


def standardize_author(value, preferred_names=()):
    """Use a linked catalog spelling when author name tokens match exactly."""
    name = re.sub(r'\s+', ' ', html.unescape(str(value or '')).strip())
    if not name or name.casefold() in {'unknown', 'n/a', 'none'}:
        return 'Unknown'
    redundant = re.search(r'\s*\[([^\]]+)\]\s*$', name)
    if redundant and _author_tokens(name[:redundant.start()]) == _author_tokens(redundant.group(1)):
        name = name[:redundant.start()].strip()
    name = re.sub(r'\b(\w+)(?:\s+\1\b)+', r'\1', name, flags=re.I)
    signature = _author_tokens(name)
    if not signature:
        return 'Unknown'
    candidates = Counter(
        re.sub(r'\s+', ' ', str(candidate or '')).strip()
        for candidate in preferred_names
        if str(candidate or '').strip().casefold() not in {'unknown', 'n/a', 'none'}
        and _author_tokens(candidate) == signature)
    if candidates:
        return sorted(candidates.items(), key=lambda pair: (-pair[1], pair[0].casefold()))[0][0]
    return name


def standardize_title(value):
    """Remove only clear edition/package labels when no catalog title is linked."""
    title = re.sub(r'\s+', ' ', html.unescape(str(value or '')).strip())
    title = re.sub(
        r'\s+(?:\(?\s*(?:1st|2nd|3rd|\d+(?:st|nd|rd|th))\s+ed(?:ition)?\s*\)?'
        r'|hardcover|paperback|hc|pb)\s*$', '', title, flags=re.I)
    title = re.sub(r'\s*\(\s*(?:US|UK)\s*\)\s*$', '', title, flags=re.I)
    return title.strip()


def parse_goodreads_export(path):
    """Read a Goodreads library CSV export into books and their shelf labels."""
    path = Path(path)
    with path.open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError('The selected file does not contain a Goodreads CSV header.')
        columns = {str(name or '').strip().casefold(): name for name in reader.fieldnames}

        def cell(row, *names):
            for name in names:
                column = columns.get(name.casefold())
                if column is not None:
                    value = str(row.get(column) or '').strip()
                    if value:
                        return value
            return ''

        if 'title' not in columns:
            raise ValueError('The CSV is missing Goodreads’ Title column.')
        books = []
        seen = set()
        shelves = set()
        for row in reader:
            title = standardize_title(cell(row, 'title'))
            if not title:
                continue
            author = cell(row, 'author') or 'Unknown'
            raw_shelves = cell(row, 'bookshelves', 'bookshelves with positions')
            shelf_values = list(dict.fromkeys(
                shelf.strip() for shelf in raw_shelves.split(',') if shelf.strip()))
            exclusive_shelf = cell(row, 'exclusive shelf', 'shelf')
            if exclusive_shelf and exclusive_shelf not in shelf_values:
                shelf_values.append(exclusive_shelf)
            shelves.update(shelf_values)

            book_id = cell(row, 'book id', 'book_id', 'goodreads id')
            book_id = re.sub(r'^=\s*"|"$', '', book_id).strip()
            book_id = book_id if re.fullmatch(r'\d+', book_id) else ''
            source_url = (f'https://www.goodreads.com/book/show/{book_id}'
                          if book_id else '')
            isbn = cell(row, 'isbn13', 'isbn 13') or cell(row, 'isbn')
            isbn = re.sub(r'^=\s*"|"$', '', isbn).strip()
            isbn = re.sub(r'[^0-9Xx]', '', isbn)
            catalog = {'catalog_id': book_id} if book_id else {}
            book = {
                'title': title,
                'author': author,
                'provider': 'Goodreads',
                'source_url': source_url,
                'catalog_metadata': catalog,
                'goodreads_shelves': shelf_values,
            }
            if isbn:
                book['isbn13' if len(isbn) == 13 else 'isbn'] = isbn
            key = ('goodreads:' + book_id if book_id else
                   title.casefold() + '\0' + author.casefold())
            if key in seen:
                continue
            seen.add(key)
            books.append(book)
    if not books:
        raise ValueError('No titled books were found in the selected Goodreads CSV.')
    return {'books': books, 'shelves': sorted(shelves, key=str.casefold)}
