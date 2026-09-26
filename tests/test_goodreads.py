import datetime as dt
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from media_intake.core import connect
from media_intake.watched import (add_watch, fetch_goodreads, parse_goodreads_html,
                                  parse_goodreads_search_html,
                                  process_due_watches, search_goodreads)


AUTHOR_URL = 'https://www.goodreads.com/author/show/4.Sample_Author'
AUTHOR_HTML = '''<!doctype html><html><head>
<meta property="og:title" content="Sample Author | Goodreads">
</head><body><h1>Sample Author</h1>
<a href="/book/show/101-old-book">Old Book</a>
<a href="/book/show/102-future-book">Future Book</a>
</body></html>'''


class GoodreadsTest(unittest.TestCase):
    def test_import_saved_author_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / 'author.html'
            page.write_text(AUTHOR_HTML, encoding='utf-8')
            result = parse_goodreads_html(AUTHOR_URL, page)
        self.assertEqual(result['kind'], 'author')
        self.assertEqual(result['title'], 'Sample Author')
        self.assertEqual({item['title'] for item in result['books']}, {'Old Book', 'Future Book'})

    def test_import_saved_search_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / 'search.html'
            page.write_text('<a href="/author/show/4.Sample_Author">Sample Author</a>', encoding='utf-8')
            results = parse_goodreads_search_html(
                'Sample Author', page, 'https://www.goodreads.com/search?q=Sample+Author')
        self.assertEqual(results, [{'kind': 'author', 'title': 'Sample Author',
                                    'source_url': 'https://www.goodreads.com/author/show/4.Sample_Author'}])

    def test_goodreads_redirect_loop_is_explained(self):
        error = HTTPError(AUTHOR_URL, 302, 'redirect loop', {}, None)
        with patch('media_intake.core.http_get', side_effect=error):
            with self.assertRaisesRegex(ValueError, r'repeatedly \(HTTP 302\)'):
                fetch_goodreads(AUTHOR_URL)
        with patch('media_intake.core.http_get', side_effect=error):
            with self.assertRaisesRegex(ValueError, 'Goodreads search failed.*HTTP 302'):
                search_goodreads('Sample Author')

    def test_manual_author_add_establishes_a_baseline(self):
        today = dt.date.today()
        tomorrow = (today + dt.timedelta(days=2)).isoformat()
        page = {'kind': 'author', 'title': 'Sample Author', 'books': [
            {'title': 'Old Book', 'author': 'Sample Author',
             'source_url': 'https://www.goodreads.com/book/show/101-old-book',
             'release_date': '2001-01-01'},
            {'title': 'Future Book', 'author': 'Sample Author',
             'source_url': 'https://www.goodreads.com/book/show/102-future-book',
             'release_date': tomorrow},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                watch_id = add_watch(db, 'author', 'Sample Author', 'Unknown', AUTHOR_URL,
                                     metadata={'known_books': [], 'goodreads_baseline_pending': True})
                with patch('media_intake.watched.fetch_goodreads', return_value=page):
                    self.assertEqual(process_due_watches(db, str(Path(tmp) / 'library'), force=True), 1)
                watches = list(db.execute('SELECT * FROM watched_items ORDER BY id'))
                self.assertEqual(len(watches), 2)
                self.assertEqual(watches[0]['id'], watch_id)
                self.assertEqual(watches[1]['title'], 'Future Book')
                self.assertEqual(watches[1]['state'], 'upcoming')
                self.assertEqual(watches[1]['release_date'], tomorrow)


if __name__ == '__main__':
    unittest.main()
