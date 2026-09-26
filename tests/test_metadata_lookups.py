import json
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from media_intake.core import connect
from media_intake.sources import search_openlibrary_metadata
from media_intake.watched import (add_from_goodreads_metadata, add_watch,
                                  _process_openlibrary_author_collection,
                                  search_book_metadata,
                                  search_goodreads, search_goodreads_with_fallback)


class MetadataLookupTest(unittest.TestCase):
    def test_openlibrary_metadata_maps_author_and_book_hits(self):
        author_page = json.dumps({'docs': [
            {'key': 'OL1394865A', 'name': 'Brandon Sanderson', 'work_count': 100},
        ]}).encode()
        book_page = json.dumps({'docs': [
            {'key': '/works/OL123W', 'title': 'Mistborn',
             'author_name': ['Brandon Sanderson'], 'first_publish_year': 2006},
        ]}).encode()
        with patch('media_intake.sources.http_get', side_effect=[
                (author_page, 'https://openlibrary.org/search/authors.json'),
                (book_page, 'https://openlibrary.org/search.json')]):
            results = search_openlibrary_metadata('Brandon Sanderson')

        self.assertEqual([(row['kind'], row['title']) for row in results], [
            ('author', 'Brandon Sanderson'), ('book', 'Mistborn'),
        ])
        self.assertEqual(results[0]['source_url'], 'https://openlibrary.org/authors/OL1394865A')
        self.assertEqual(results[1]['source_url'], 'https://openlibrary.org/works/OL123W')

    def test_goodreads_block_falls_back_to_openlibrary(self):
        fallback = [{'provider': 'Open Library', 'kind': 'author', 'title': 'Brandon Sanderson',
                     'source_url': 'https://openlibrary.org/authors/OL1394865A'}]
        with patch('media_intake.watched.search_goodreads', side_effect=ValueError('HTTP 302')):
            with patch('media_intake.watched.search_openlibrary_metadata', return_value=fallback):
                results = search_goodreads_with_fallback('Brandon Sanderson')
        self.assertEqual(results, fallback)

    def test_empty_goodreads_response_is_reported_as_blocked_not_no_matches(self):
        with patch('media_intake.core.http_get', return_value=(b'', 'https://www.goodreads.com/search')):
            with self.assertRaisesRegex(ValueError, 'empty search response'):
                search_goodreads('Brandon Sanderson')

    def test_book_lookup_aggregates_metadata_providers(self):
        openlibrary = [{'provider': 'Open Library', 'kind': 'author', 'title': 'Brandon Sanderson',
                        'source_url': 'https://openlibrary.org/authors/OL1394865A'}]
        novelarchive = [{'provider': 'NovelArchive', 'title': 'Example serial',
                         'url': 'https://novelarchive.cc/novel?id=7',
                         'metadata': {'catalog_id': '7'}}]
        lnori = [{'provider': 'LNORI', 'title': 'Example light novel',
                  'url': 'https://lnori.com/series/8'}]
        ranobedb = [{'provider': 'RanobeDB', 'title': 'Another light novel',
                     'url': 'https://ranobedb.org/series/9'}]
        with patch('media_intake.watched.search_goodreads', return_value=[]), \
                patch('media_intake.watched.search_openlibrary_metadata', return_value=openlibrary), \
                patch('media_intake.watched.search_novelarchive', return_value=novelarchive), \
                patch('media_intake.watched.search_lnori', return_value=lnori), \
                patch('media_intake.watched.search_ranobedb', return_value=ranobedb):
            results = search_book_metadata('example')

        kinds = {(row['provider'], row['kind']) for row in results}
        self.assertIn(('Open Library', 'author'), kinds)
        self.assertIn(('NovelArchive', 'webfiction'), kinds)
        self.assertIn(('LNORI', 'lightnovel'), kinds)
        self.assertIn(('RanobeDB', 'lightnovel'), kinds)
        self.assertTrue(all(row['source_url'] for row in results))

    def test_series_selection_watches_every_listed_book(self):
        series = {
            'provider': 'Goodreads', 'kind': 'series', 'title': 'The Stormlight Archive',
            'source_url': 'https://www.goodreads.com/series/49075',
            'books': [
                {'title': 'The Way of Kings', 'author': 'Brandon Sanderson',
                 'source_url': 'https://www.goodreads.com/book/show/7235533'},
                {'title': 'Words of Radiance', 'author': 'Brandon Sanderson',
                 'source_url': 'https://www.goodreads.com/book/show/17332218'},
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            with closing(connect(temp)) as db:
                add_from_goodreads_metadata(db, series, series['source_url'],
                                            sources={'openlibrary': True})
                rows = [dict(row) for row in db.execute(
                    'SELECT kind,title,parent_id FROM watched_items ORDER BY id')]

        self.assertEqual([row['title'] for row in rows], [
            'The Stormlight Archive', 'The Way of Kings', 'Words of Radiance',
        ])
        self.assertEqual([row['kind'] for row in rows], ['series', 'book', 'book'])
        self.assertIsNone(rows[0]['parent_id'])
        self.assertEqual([row['parent_id'] for row in rows[1:]], [1, 1])

    def test_openlibrary_author_watch_schedules_future_works(self):
        payload = json.dumps({'entries': [
            {'key': '/works/OL11W', 'title': 'Older title', 'first_publish_date': '2001'},
            {'key': '/works/OL12W', 'title': 'Upcoming title', 'first_publish_date': '2099'},
        ]}).encode()
        with tempfile.TemporaryDirectory() as temp:
            with closing(connect(temp)) as db:
                watch_id = add_watch(
                    db, 'author', 'Example Author', source_url='https://openlibrary.org/authors/OL123A',
                    metadata={'catalog_provider': 'Open Library', 'openlibrary_baseline_pending': True})
                watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
                with patch('media_intake.watched.http_get', return_value=(
                        payload, 'https://openlibrary.org/authors/OL123A/works.json')):
                    _process_openlibrary_author_collection(db, watch)
                rows = [dict(row) for row in db.execute(
                    'SELECT title,release_date,parent_id FROM watched_items ORDER BY id')]

        self.assertEqual([row['title'] for row in rows], ['Example Author', 'Upcoming title'])
        self.assertEqual(rows[1]['release_date'], '2099-01-01')
        self.assertEqual(rows[1]['parent_id'], watch_id)


if __name__ == '__main__':
    unittest.main()
