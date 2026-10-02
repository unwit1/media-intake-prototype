import datetime as dt
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from media_intake.core import connect
from media_intake.watched import (add_watch, fetch_goodreads, parse_goodreads_html,
                                  parse_goodreads_search_html,
                                  parse_goodreads_page,
                                  process_due_watches, search_goodreads)


AUTHOR_URL = 'https://www.goodreads.com/author/show/4.Sample_Author'
AUTHOR_HTML = '''<!doctype html><html><head>
<meta property="og:title" content="Sample Author | Goodreads">
</head><body><h1>Sample Author</h1>
<a href="/book/show/101-old-book">Old Book</a>
<a href="/book/show/102-future-book">Future Book</a>
</body></html>'''


class GoodreadsTest(unittest.TestCase):
    def test_goodreads_search_reserves_results_for_series_and_author_tabs(self):
        def fake_page_parser(_query, page_url, _page, limit):
            search_type = urllib.parse.parse_qs(
                urllib.parse.urlsplit(page_url).query).get('search_type', ['books'])[0]
            kind = {'books': 'book', 'authors': 'author', 'series': 'series'}[search_type]
            count = 10 if kind == 'book' else 2 if kind == 'series' else 1
            return [{'kind': kind, 'title': f'{search_type.title()} {index}',
                     'source_url': f'https://www.goodreads.com/{search_type}/{index}'}
                    for index in range(min(limit, count))]

        def fake_http_get(url):
            return b'<html>fixture</html>', url

        with patch('media_intake.core.http_get', side_effect=fake_http_get), \
                patch('media_intake.watched._goodreads_results_from_html',
                      side_effect=fake_page_parser) as parse_page:
            results = search_goodreads('Percy Jackson', limit=30)

        self.assertEqual(parse_page.call_count, 3)
        self.assertIn(('series', 'Series 0'),
                      [(item['kind'], item['title']) for item in results])
        self.assertIn(('author', 'Authors 0'),
                      [(item['kind'], item['title']) for item in results])
        self.assertEqual(results[0]['kind'], 'series')

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

    def test_series_book_isbn_is_retained_from_goodreads_jsonld(self):
        content = '''<html><script type="application/ld+json">{
          "@graph": [
            {"@type":"BookSeries","name":"Example Series"},
            {"@type":"Book","name":"Example Novel",
             "author":{"@type":"Person","name":"Example Author"},
             "isbn":"0-306-40615-2",
             "url":"https://www.goodreads.com/book/show/1"}
          ]}
        </script></html>'''
        result = parse_goodreads_page('https://www.goodreads.com/series/123', content)

        self.assertEqual(result['books'][0]['catalog_metadata']['isbns'],
                         ['9780306406157'])

    def test_goodreads_redirect_loop_is_explained(self):
        error = HTTPError(AUTHOR_URL, 302, 'redirect loop', {}, None)
        self.addCleanup(error.close)
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
                with patch('media_intake.watched.fetch_goodreads', return_value=page), \
                     patch('media_intake.watched._search_book', return_value=False):
                    self.assertEqual(process_due_watches(db, str(Path(tmp) / 'library'), force=True), 3)
                watches = list(db.execute('SELECT * FROM watched_items ORDER BY id'))
                author_edges = list(db.execute('''SELECT r.relationship_type,e.provider,
                        e.raw_relationship_type
                    FROM media_relationships r JOIN media_relationship_evidence e
                      ON e.relationship_id=r.id
                    WHERE r.relationship_type='created_by' AND e.provider='goodreads'
                    ORDER BY e.provider_entity_id'''))
                self.assertEqual(len(watches), 3)
                self.assertEqual(watches[0]['id'], watch_id)
                self.assertEqual([row['title'] for row in watches[1:]], ['Old Book', 'Future Book'])
                self.assertEqual(watches[1]['state'], 'searching')
                self.assertEqual(watches[2]['state'], 'upcoming')
                self.assertEqual(watches[2]['release_date'], tomorrow)
                self.assertEqual([tuple(row) for row in author_edges], [
                    ('created_by', 'goodreads', 'author bibliography'),
                    ('created_by', 'goodreads', 'author bibliography'),
                ])


if __name__ == '__main__':
    unittest.main()
