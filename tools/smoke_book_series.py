"""Optional real public-domain multi-series smoke test in a dedicated test root.

Runs six small Gutenberg EPUB transfers sequentially. Never touches the user's
normal library and never signs in, sends email, or delivers to a device.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_intake.book_batch import add_book_series_batch
from media_intake.core import connect, enqueue, download

SERIES = [
    ('Alice in Wonderland', 'Lewis Carroll', [(11, "Alice's Adventures in Wonderland"), (12, 'Through the Looking-Glass')]),
    ('Sherlock Holmes', 'Arthur Conan Doyle', [(1661, 'The Adventures of Sherlock Holmes'), (2852, 'The Hound of the Baskervilles')]),
    ('Oz', 'L. Frank Baum', [(55, 'The Wonderful Wizard of Oz'), (54, 'The Marvelous Land of Oz')]),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default='build/book-series-live-smoke',
                        help='Dedicated test directory; must not already contain a non-smoke database')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / 'book-series-smoke.json'
    if (root / 'intake.sqlite3').exists() and not marker.exists():
        raise ValueError('Use a dedicated smoke directory, not an existing library')
    marker.write_text(json.dumps({'purpose': 'isolated public-domain test library'}), encoding='utf-8')
    records = []
    for title, author, works in SERIES:
        records.append({'kind': 'series', 'title': title, 'author': author,
            'provider': 'Gutenberg smoke test', 'source_url': f'https://www.gutenberg.org/ebooks/{works[0][0]}#smoke-series',
            'books': [{'title': name, 'author': author, 'source_url': f'https://www.gutenberg.org/ebooks/{identifier}',
                       'release_date': '1920-01-01', 'series_index': number}
                      for number, (identifier, name) in enumerate(works, 1)]})
    started = time.monotonic()
    db = connect(root)
    try:
        report = {'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'root': str(root), 'batch': add_book_series_batch(db, records), 'downloads': []}
        for _, author, works in SERIES:
            for identifier, title in works:
                source = f'https://www.gutenberg.org/ebooks/{identifier}'
                watch = db.execute('SELECT * FROM watched_items WHERE kind=\'book\' AND source_url=?', (source,)).fetchone()
                row = enqueue(db, {'title': title, 'author': author, 'source': source,
                    'url': f'https://www.gutenberg.org/ebooks/{identifier}.epub3.images', 'format': '.epub'})
                try:
                    result = download(db, row['id'], root, destination=root / 'library')
                    db.execute("UPDATE watched_items SET state='downloaded',file_path=?,last_error=NULL WHERE id=?", (result['file_path'], watch['id']))
                    db.commit()
                    report['downloads'].append({'title': title, 'status': 'downloaded',
                        'file_path': result['file_path'], 'sha256': result['sha256']})
                except Exception as exc:
                    report['downloads'].append({'title': title, 'status': 'failed', 'error': str(exc)})
                time.sleep(1)
        report['duplicate_batch'] = add_book_series_batch(db, records)
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        marker.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'report': str(marker), 'series': report['batch']['added'],
                          'downloads': len([row for row in report['downloads'] if row['status'] == 'downloaded']),
                          'failures': [row for row in report['downloads'] if row['status'] == 'failed'],
                          'duplicate_books_added': report['duplicate_batch']['books_added']}, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()
