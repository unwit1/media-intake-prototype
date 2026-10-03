"""Validated identifiers and conservative title keys for book work matching."""
import unicodedata
import re


_ISBN_TOKEN = re.compile(r'(?<![A-Z0-9])[0-9][0-9Xx -]{8,20}[0-9Xx](?![A-Z0-9])',
                         re.I)
_FORMAT_SUFFIX = re.compile(
    r'\b(?:(?:mass market|trade) )?(?:paperback|hardback|hardcover|kindle|'
    r'e book|ebook|audio book|audiobook|large print)(?: edition)?$')


def book_work_title_key(value):
    """Normalize a title and remove a final, explicit physical/digital format label.

    This is intentionally narrower than general edition detection. It allows
    catalogs that omit ISBNs to merge obvious format variants while preserving
    revised, annotated, collected, and other potentially distinct-edition labels.
    """
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    inverted_article = re.match(
        r'^(?P<body>.+?),\s*(?P<article>the|a|an)(?P<tail>.*)$', text, flags=re.I)
    if inverted_article:
        tail_words = ' '.join(re.findall(r'[a-z0-9]+', inverted_article.group('tail')))
        if not tail_words or _FORMAT_SUFFIX.match(tail_words):
            text = (f"{inverted_article.group('article')} "
                    f"{inverted_article.group('body')}"
                    f"{inverted_article.group('tail')}")
    words = re.findall(r'[a-z0-9]+', text)
    normalized = ' '.join(words)
    while True:
        stripped = _FORMAT_SUFFIX.sub('', normalized).strip()
        if stripped == normalized:
            return normalized
        normalized = stripped


def normalize_isbn(value):
    """Return a checksum-validated ISBN-13 key for an ISBN-10 or ISBN-13 value."""
    text = str(value or '').strip()
    for token in _ISBN_TOKEN.findall(text):
        digits = re.sub(r'[^0-9X]', '', token.upper())
        if len(digits) == 10:
            if not digits[:9].isdigit():
                continue
            total = sum(int(digit) * (10 - index)
                        for index, digit in enumerate(digits[:9]))
            check = (11 - total % 11) % 11
            expected = 'X' if check == 10 else str(check)
            if digits[-1] != expected:
                continue
            prefix = '978' + digits[:9]
            checksum = sum(int(digit) * (1 if index % 2 == 0 else 3)
                           for index, digit in enumerate(prefix))
            return prefix + str((10 - checksum % 10) % 10)
        if len(digits) == 13 and digits.isdigit():
            checksum = sum(int(digit) * (1 if index % 2 == 0 else 3)
                           for index, digit in enumerate(digits[:12]))
            if (10 - checksum % 10) % 10 == int(digits[-1]):
                return digits
    return None


def collect_isbns(*values):
    """Normalize ISBN fields that may be strings, arrays, or Schema.org values."""
    found = []

    def visit(value, explicit_isbn=True):
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item, explicit_isbn)
            return
        if isinstance(value, dict):
            property_name = str(value.get('propertyID') or value.get('propertyId') or
                                 value.get('name') or '').casefold()
            if 'isbn' in property_name:
                visit(value.get('value') or value.get('@value'), True)
            for key, item in value.items():
                if 'isbn' in str(key).casefold():
                    visit(item, True)
            return
        if explicit_isbn:
            normalized = normalize_isbn(value)
            if normalized and normalized not in found:
                found.append(normalized)

    for value in values:
        visit(value)
    return found


def book_isbns(book):
    """Read the incoming ISBN and its retained provider-variant evidence."""
    book = book if isinstance(book, dict) else {}
    found = []

    def merge(catalog):
        if not isinstance(catalog, dict):
            return
        for value in collect_isbns(catalog.get('isbns'), catalog.get('isbn'),
                                   catalog.get('isbn_10'), catalog.get('isbn_13')):
            if value not in found:
                found.append(value)

    merge(book.get('catalog_metadata') or book.get('metadata'))
    for key in ('metadata_variants', 'catalog_metadata_variants'):
        variants = book.get(key) or []
        if not isinstance(variants, list):
            variants = [variants]
        for variant in variants:
            if isinstance(variant, dict):
                merge(variant.get('catalog_metadata') or variant.get('metadata'))
    return found
