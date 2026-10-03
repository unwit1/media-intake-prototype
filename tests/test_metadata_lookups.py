import json
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from media_intake.core import connect, list_items
from media_intake.media_graph import (get_media_graph, list_graph_reviews,
                                      resolve_graph_review)
from media_intake.sources import (search_google_books_metadata,
                                  search_openlibrary_metadata)
from media_intake.series_continuity import (enrich_series_members,
                                            series_member_review_reason)
from media_intake.watched import (add_from_goodreads_metadata, add_watch,
                                  _add_catalog_book_watch,
                                  _search_wikidata_series,
                                  _process_google_books_series_collection,
                                  _process_openlibrary_author_collection,
                                  _process_goodreads_collection,
                                  process_due_watches,
                                  search_book_metadata,
                                  search_goodreads, search_goodreads_with_fallback)


class MetadataLookupTest(unittest.TestCase):
    def test_wikidata_lookup_recognizes_childrens_fantasy_book_series(self):
        entities = [
            {'provider': 'Wikidata', 'qid': 'Q1',
             'title': 'Percy Jackson & the Olympians',
             'description': "children's fantasy adventure series",
             'source_url': 'https://www.wikidata.org/entity/Q1'},
            {'provider': 'Wikidata', 'qid': 'Q2', 'title': 'A television series',
             'description': 'American television series',
             'source_url': 'https://www.wikidata.org/entity/Q2'},
        ]
        members = [{'title': 'The Lightning Thief', 'author': 'Rick Riordan',
                    'source_url': 'https://www.wikidata.org/entity/Q3',
                    'series_index': 1}]
        with patch('media_intake.watched.search_wikidata', return_value=entities), \
                patch('media_intake.watched._wikidata_series_members',
                      return_value=members) as expand:
            results = _search_wikidata_series('Percy Jackson')

        self.assertEqual([row['title'] for row in results],
                         ['Percy Jackson & the Olympians'])
        self.assertEqual(results[0]['kind'], 'series')
        self.assertEqual(results[0]['books'][0]['title'], 'The Lightning Thief')
        expand.assert_called_once_with('Q1', limit=5000)

    def test_google_books_maps_isbns_and_explicit_series_members(self):
        payload = {'items': [
            {'id': 'vol-one', 'volumeInfo': {
                'title': 'Book One', 'authors': ['Example Author'],
                'publishedDate': '2020',
                'industryIdentifiers': [{'type': 'ISBN_10', 'identifier': '0306406152'}],
                'seriesInfo': {'bookDisplayNumber': '1', 'volumeSeries': [{
                    'seriesId': 'SERIES1', 'seriesTitle': 'Example Series'}]},
            }},
            {'id': 'vol-two', 'volumeInfo': {
                'title': 'Book Two', 'authors': ['Example Author'],
                'publishedDate': '2021-03-04',
                'industryIdentifiers': [{'type': 'ISBN_13', 'identifier': '9780306406157'}],
                'seriesInfo': {'bookDisplayNumber': '2', 'volumeSeries': [{
                    'seriesId': 'SERIES1', 'seriesTitle': 'Example Series'}]},
            }},
        ]}
        with patch('media_intake.sources.http_get', return_value=(
                json.dumps(payload).encode(),
                'https://www.googleapis.com/books/v1/volumes')):
            results = search_google_books_metadata('Example Series')

        series = next(item for item in results if item['kind'] == 'series')
        self.assertEqual(series['title'], 'Example Series')
        self.assertEqual([book['series_index'] for book in series['books']], [1, 2])
        self.assertEqual(series['catalog_metadata']['series_id'], 'SERIES1')
        self.assertEqual([book['catalog_metadata']['isbns'] for book in series['books']], [
            ['9780306406157'], ['9780306406157']])
        self.assertIsNone(series['books'][0]['release_date'])
        self.assertEqual(series['books'][1]['release_date'], '2021-03-04')

    def test_openlibrary_metadata_maps_author_and_book_hits(self):
        author_page = json.dumps({'docs': [
            {'key': 'OL1394865A', 'name': 'Brandon Sanderson', 'work_count': 100},
        ]}).encode()
        book_page = json.dumps({'docs': [
            {'key': '/works/OL123W', 'title': 'Mistborn',
             'author_name': ['Brandon Sanderson'], 'first_publish_year': 2006,
             'isbn': ['0306406152', '9780306406157', '9780306406158']},
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
        self.assertEqual(results[1]['catalog_metadata']['isbns'], ['9780306406157'])

    def test_goodreads_block_falls_back_to_openlibrary(self):
        fallback = [{'provider': 'Open Library', 'kind': 'author', 'title': 'Brandon Sanderson',
                     'source_url': 'https://openlibrary.org/authors/OL1394865A'}]
        with patch('media_intake.watched.search_goodreads', side_effect=ValueError('HTTP 302')):
            with patch('media_intake.watched.search_openlibrary_metadata', return_value=fallback), \
                    patch('media_intake.watched.search_google_books_metadata', return_value=[]), \
                    patch('media_intake.watched._search_wikidata_series', return_value=[]):
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
                patch('media_intake.watched.search_google_books_metadata', return_value=[]), \
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

    def test_lookup_window_puts_series_names_first_for_multiple_searches(self):
        series_names = ('Percy Jackson & the Olympians',
                        'The Stormlight Archive', 'The Mistborn Saga')
        for title in series_names:
            with self.subTest(series=title):
                series = {'provider': 'Goodreads', 'kind': 'series', 'title': title,
                          'source_url': f'https://www.goodreads.com/series/{len(title)}',
                          'books': []}
                books = [{'provider': 'Goodreads', 'kind': 'book',
                          'title': title + f' Book {index}',
                          'source_url': f'https://www.goodreads.com/book/show/{index}'}
                         for index in range(1, 12)]
                with patch('media_intake.watched.search_goodreads',
                           return_value=books + [series]), \
                        patch('media_intake.watched.search_openlibrary_metadata',
                              return_value=[]), \
                        patch('media_intake.watched.search_google_books_metadata',
                              return_value=[]), \
                        patch('media_intake.watched.search_novelarchive', return_value=[]), \
                        patch('media_intake.watched.search_lnori', return_value=[]), \
                        patch('media_intake.watched.search_ranobedb', return_value=[]), \
                        patch('media_intake.watched._search_wikidata_series',
                              return_value=[]):
                    results = search_book_metadata(title)

                self.assertEqual(results[0]['kind'], 'series')
                self.assertEqual(results[0]['title'], title)

    def test_series_selection_adds_parent_and_unique_member_watches_in_order(self):
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
                    'SELECT id,kind,title,parent_id,media_entity_id FROM watched_items ORDER BY id')]
                graph = get_media_graph(db, rows[0]['media_entity_id'])

        self.assertEqual([row['title'] for row in rows], [
            'The Stormlight Archive', 'The Way of Kings', 'Words of Radiance'])
        self.assertEqual([row['kind'] for row in rows], ['series', 'book', 'book'])
        self.assertIsNone(rows[0]['parent_id'])
        self.assertEqual([row['parent_id'] for row in rows[1:]], [rows[0]['id'], rows[0]['id']])
        self.assertEqual(len(graph['relationships']), 2)
        self.assertTrue(all(row['relationship_type'] == 'contains'
                            for row in graph['relationships']))
        self.assertEqual({row['title'] for row in graph['entities']}, {
            'The Stormlight Archive', 'The Way of Kings', 'Words of Radiance'})

    def test_stormlight_continuity_adds_short_work_and_holds_horneater_until_dated(self):
        catalog_members = [{
            'title': 'The Way of Kings', 'author': 'Brandon Sanderson',
            'source_url': 'https://www.goodreads.com/book/show/7235533',
            'series_index': 1, 'series_position': '1',
            'series_position_source': 'Goodreads series listing',
            'provider': 'Goodreads',
        }]
        members, plan = enrich_series_members('The Stormlight Archive', catalog_members)
        series = {
            'provider': 'Goodreads', 'kind': 'series',
            'title': 'The Stormlight Archive',
            'source_url': 'https://www.goodreads.com/series/49075',
            'series_plan': plan, 'books': members,
        }

        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            series_id, _ = add_from_goodreads_metadata(
                db, series, series['source_url'])
            parent = dict(db.execute(
                'SELECT * FROM watched_items WHERE id=?', (series_id,)).fetchone())
            children = [dict(row) for row in db.execute(
                'SELECT * FROM watched_items WHERE parent_id=? ORDER BY id',
                (series_id,))]
            queue = [json.loads(row['metadata']) for row in list_items(db)]
            queue_states = {row['url']: row['state'] for row in list_items(db)}

        self.assertEqual(json.loads(parent['metadata'])['series_plan']['next_mainline_number'], 6)
        self.assertIsNone(json.loads(parent['metadata'])['series_plan']['next_mainline_title'])
        self.assertEqual([row['title'] for row in children], [
            'The Way of Kings', 'Elsecaller / King Lopen the First of Alethkar', 'Horneater'])
        horneater = children[-1]
        self.assertIsNone(horneater['release_date'])
        self.assertEqual(horneater['state'], 'upcoming')
        horneater_metadata = json.loads(horneater['metadata'])
        self.assertTrue(horneater_metadata['search_after_release_date'])
        self.assertEqual(horneater_metadata['series_index'], 4.5)
        self.assertEqual(horneater_metadata['publication_status'],
                         'announced; release date unconfirmed')
        request = next(item for item in queue if item.get('title') == 'Horneater')
        self.assertIsNone(request['release_date'])
        self.assertEqual(request['publication_status'],
                         'announced; release date unconfirmed')
        self.assertEqual(queue_states[f"watch-search://{horneater['id']}"], 'upcoming')

    def test_same_isbn_from_different_catalogs_reuses_the_child_watch(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            series_id = add_watch(db, 'series', 'Example Series', 'Example Author',
                                  'https://www.goodreads.com/series/123')
            first_id, first_created = _add_catalog_book_watch(
                db, {'title': 'Example Novel', 'author': 'Example Author',
                     'source_url': 'https://www.goodreads.com/book/show/1',
                     'catalog_metadata': {'catalog_id': 'gr:1',
                                          'isbns': ['0-306-40615-2']}},
                'Example Author', {}, series_id, 'Goodreads')
            second_id, second_created = _add_catalog_book_watch(
                db, {'title': 'Example Novel: A Novel',
                     'author': 'Example Author',
                     'source_url': 'https://openlibrary.org/works/OL1W',
                     'catalog_metadata': {'catalog_id': 'OL1W',
                                          'isbns': ['9780306406157']}},
                'Example Author', {}, series_id, 'Open Library')
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM watched_items WHERE kind='book'")]
            isbn_identity = db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider='isbn' AND external_id='9780306406157' ''').fetchone()

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(rows), 1)
        metadata = json.loads(rows[0]['metadata'])
        self.assertEqual(metadata['catalog_ids'], {
            'Goodreads': ['gr:1'], 'Open Library': ['OL1W']})
        self.assertIsNotNone(isbn_identity)
        self.assertEqual(isbn_identity['entity_id'], rows[0]['media_entity_id'])

    def test_series_format_title_variant_reuses_child_watch_without_isbn(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            series_id = add_watch(db, 'series', 'Example Series', 'Example Author',
                                  'https://www.goodreads.com/series/123')
            first_id, first_created = _add_catalog_book_watch(
                db, {'title': 'Example Novel', 'author': 'Example Author',
                     'series_index': 1,
                     'source_url': 'https://www.goodreads.com/book/show/1',
                     'catalog_metadata': {'catalog_id': 'gr:1'}},
                'Example Author', {}, series_id, 'Goodreads')
            second_id, second_created = _add_catalog_book_watch(
                db, {'title': 'Example Novel (Paperback)', 'author': 'Example Author',
                     'series_index': 1,
                     'source_url': 'https://openlibrary.org/works/OL1W',
                     'catalog_metadata': {'catalog_id': 'OL1W'}},
                'Example Author', {}, series_id, 'Open Library')
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM watched_items WHERE kind='book'")]

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['metadata'])['catalog_ids'], {
            'Goodreads': ['gr:1'], 'Open Library': ['OL1W']})

    def test_unmatched_format_labeled_series_member_stays_in_review(self):
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            series_id = add_watch(db, 'series', 'Example Series', 'Example Author',
                                  'https://www.goodreads.com/series/123')
            watch_id, created = _add_catalog_book_watch(
                db, {'title': 'Unmatched Novel (Paperback)', 'author': 'Example Author',
                     'source_url': 'https://openlibrary.org/works/OL2W'},
                'Example Author', {}, series_id, 'Open Library')
            reviews = list_graph_reviews(db)
            books = db.execute(
                "SELECT COUNT(*) FROM watched_items WHERE kind='book'").fetchone()[0]

        self.assertIsNone(watch_id)
        self.assertFalse(created)
        self.assertEqual(books, 0)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['issue_type'], 'uncertain_series_member')

    def test_goodreads_series_refresh_reuses_existing_format_variant_graph_work(self):
        page = {'books': [{
            'title': 'Example Novel (Paperback)', 'author': 'Example Author',
            'release_date': '2020-01-01',
            'source_url': 'https://www.goodreads.com/book/show/2',
        }]}
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            series_id = add_watch(
                db, 'series', 'Example Series', 'Example Author',
                'https://openlibrary.org/subjects/example-series',
                metadata={'catalog_provider': 'Open Library'})
            existing_id, _created = _add_catalog_book_watch(
                db, {'title': 'Example Novel', 'author': 'Example Author',
                     'source_url': 'https://openlibrary.org/works/OL1W',
                     'catalog_metadata': {'catalog_id': 'OL1W'}},
                'Example Author', {}, series_id, 'Open Library')
            series = dict(db.execute(
                'SELECT * FROM watched_items WHERE id=?', (series_id,)).fetchone())
            with patch('media_intake.watched.fetch_goodreads', return_value=page):
                _process_goodreads_collection(db, series)
            rows = [dict(row) for row in db.execute(
                "SELECT id,title,media_entity_id FROM watched_items WHERE kind='book'")]
            ids = [dict(row) for row in db.execute('''SELECT provider,external_id,entity_id
                FROM media_external_ids WHERE provider IN ('goodreads','openlibrary')
                ORDER BY provider''')]
            book_entities = db.execute('''SELECT COUNT(*) FROM media_entities
                WHERE entity_type='work' AND media_type='book' ''').fetchone()[0]
            graph_title = db.execute('SELECT title FROM media_entities WHERE id=?',
                                     (rows[0]['media_entity_id'],)).fetchone()['title']
            alternate_title = db.execute('''SELECT 1 FROM media_entity_aliases
                WHERE entity_id=? AND alias='Example Novel (Paperback)' AND provider='goodreads' ''',
                                         (rows[0]['media_entity_id'],)).fetchone()
            membership_count = db.execute('''SELECT COUNT(*) FROM media_relationships
                WHERE (source_entity_id=? AND target_entity_id=?)
                   OR (source_entity_id=? AND target_entity_id=?)''',
                (rows[0]['media_entity_id'], series['media_entity_id'],
                 series['media_entity_id'], rows[0]['media_entity_id'])).fetchone()[0]
            reviews = [row for row in list_graph_reviews(db)
                       if row['issue_type'] == 'uncertain_series_member']

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], existing_id)
        self.assertEqual(book_entities, 1)
        self.assertEqual({(row['provider'], row['external_id']) for row in ids},
                         {('goodreads', '2'), ('openlibrary', 'OL1W')})
        self.assertEqual({row['entity_id'] for row in ids}, {rows[0]['media_entity_id']})
        self.assertEqual(graph_title, 'Example Novel')
        self.assertIsNotNone(alternate_title)
        self.assertEqual(membership_count, 1)
        self.assertEqual(reviews, [])

    def test_conflicting_series_position_is_not_auto_queued_and_can_be_reviewed(self):
        series = {
            'provider': 'Goodreads', 'kind': 'series', 'title': 'Example Series',
            'source_url': 'https://www.goodreads.com/series/123',
            'books': [{'title': 'Example Novel', 'author': 'Example Author',
                       'series_index': 1,
                       'source_url': 'https://www.goodreads.com/book/show/1'}],
            'review_members': [{
                'title': 'The Other Novel', 'author': 'Example Author',
                'series_index': '1.0', 'provider': 'Open Library',
                'source_url': 'https://openlibrary.org/works/OL2W',
                'review_reason': 'This title differs from Example Novel, but the author and series position match.',
                'identity_conflict': {'title': 'Example Novel', 'series_index': 1},
                'catalog_metadata': {'catalog_id': 'OL2W'},
            }],
        }
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            add_from_goodreads_metadata(db, series, series['source_url'])
            before = [row['title'] for row in db.execute(
                "SELECT title FROM watched_items WHERE kind='book' ORDER BY id")]
            reviews = list_graph_reviews(db)
            resolve_graph_review(db, reviews[0]['id'], 'accepted')
            after = [row['title'] for row in db.execute(
                "SELECT title FROM watched_items WHERE kind='book' ORDER BY id")]

        self.assertEqual(before, ['Example Novel'])
        self.assertEqual(reviews[0]['detail']['candidate']['identity_conflict']['title'],
                         'Example Novel')
        self.assertEqual(after, ['Example Novel', 'The Other Novel'])

    def test_series_packaging_placeholder_is_reviewed_not_queued_as_a_book(self):
        self.assertIsNone(series_member_review_reason('Dune', {'title': 'Dune'}))
        self.assertTrue(series_member_review_reason(
            'The Stormlight Archive', {'title': 'The Stormlight Archive Part One'}))
        self.assertTrue(series_member_review_reason(
            'The Stormlight Archive', {'title': 'The Way of Kings Part One'}))
        for title in ('The Way of Kings Part 1', 'Words of Radiance Volume One',
                      'The Way of Kings (Paperback)',
                      'Words of Radiance - Kindle Edition'):
            with self.subTest(title=title):
                self.assertTrue(series_member_review_reason('The Stormlight Archive', {
                    'title': title}))
        self.assertIsNone(series_member_review_reason('Dune', {
            'title': 'The Paperback Writer'}))
        series = {
            'provider': 'Goodreads', 'kind': 'series', 'title': 'The Stormlight Archive',
            'source_url': 'https://www.goodreads.com/series/49075',
            'books': [
                {'title': 'The Stormlight Archive Part One', 'author': 'Brandon Sanderson',
                 'source_url': 'https://www.goodreads.com/book/show/9001'},
                {'title': 'The Way of Kings', 'author': 'Brandon Sanderson',
                 'source_url': 'https://www.goodreads.com/book/show/7235533'},
            ],
        }
        with tempfile.TemporaryDirectory() as temp, closing(connect(temp)) as db:
            add_from_goodreads_metadata(db, series, series['source_url'])
            rows = [tuple(row) for row in db.execute(
                'SELECT kind,title FROM watched_items ORDER BY id')]
            reviews = list_graph_reviews(db)
            resolve_graph_review(db, reviews[0]['id'], 'accepted')
            accepted_rows = [tuple(row) for row in db.execute(
                'SELECT kind,title FROM watched_items ORDER BY id')]
            queue = [json.loads(row['metadata']) for row in list_items(db)]
            add_from_goodreads_metadata(db, series, series['source_url'])
            final_rows = [tuple(row) for row in db.execute(
                'SELECT kind,title FROM watched_items ORDER BY id')]
            open_reviews = list_graph_reviews(db)

        self.assertEqual(rows, [('series', 'The Stormlight Archive'),
                                ('book', 'The Way of Kings')])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['issue_type'], 'uncertain_series_member')
        self.assertEqual(reviews[0]['detail']['candidate']['title'],
                         'The Stormlight Archive Part One')
        self.assertEqual(accepted_rows[-1], ('book', 'The Stormlight Archive Part One'))
        self.assertTrue(any(item.get('watch_search') and
                            item.get('title') == 'The Stormlight Archive Part One'
                            for item in queue))
        self.assertEqual(final_rows, accepted_rows)
        self.assertEqual(open_reviews, [])

    def test_series_refresh_updates_graph_and_creates_member_watches(self):
        page = {'books': [
            {'title': 'The Way of Kings', 'author': 'Brandon Sanderson',
             'source_url': 'https://www.goodreads.com/book/show/7235533'},
            {'title': 'Words of Radiance', 'author': 'Brandon Sanderson',
             'source_url': 'https://www.goodreads.com/book/show/17332218'},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            with closing(connect(temp)) as db:
                watch_id = add_watch(db, 'series', 'The Stormlight Archive',
                                     source_url='https://www.goodreads.com/series/49075',
                                     metadata={'known_books': []})
                watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
                with patch('media_intake.watched.fetch_goodreads', return_value=page):
                    _process_goodreads_collection(db, watch)
                    _process_goodreads_collection(db, watch)
                rows = [dict(row) for row in db.execute('SELECT kind,title FROM watched_items ORDER BY id')]
                count = db.execute('SELECT COUNT(*) FROM media_relationships').fetchone()[0]
                snapshots = db.execute('SELECT COUNT(*) FROM media_graph_snapshots').fetchone()[0]

        self.assertEqual(rows, [
            {'kind': 'series', 'title': 'The Stormlight Archive'},
            {'kind': 'book', 'title': 'The Way of Kings'},
            {'kind': 'book', 'title': 'Words of Radiance'},
        ])
        self.assertEqual(count, 2)
        self.assertEqual(snapshots, 1)

    def test_google_books_series_refresh_adds_new_members_and_searches_them(self):
        series_url = 'https://books.google.com/books?q=series%3AExample+Series'
        first = {'title': 'Book One', 'author': 'Example Author',
                 'provider': 'Google Books',
                 'source_url': 'https://books.google.com/books?id=one',
                 'series': 'Example Series', 'series_index': 1,
                 'series_position_source': 'Google Books seriesInfo',
                 'catalog_metadata': {'catalog_id': 'one', 'isbns': ['9780306406157']}}
        second = {'title': 'Book Two', 'author': 'Example Author',
                  'provider': 'Google Books',
                  'source_url': 'https://books.google.com/books?id=two',
                  'series': 'Example Series', 'series_index': 2,
                  'series_position_source': 'Google Books seriesInfo',
                  'catalog_metadata': {'catalog_id': 'two'}}
        catalog_series = {'provider': 'Google Books', 'kind': 'series',
                          'title': 'Example Series', 'source_url': series_url,
                          'catalog_metadata': {'catalog_id': 'SERIES1'},
                          'books': [first, second]}
        initial_series = {**catalog_series, 'books': [first],
                          'catalog_sources': {'Google Books': [series_url]},
                          'catalog_ids': {'Google Books': ['SERIES1']}}

        with tempfile.TemporaryDirectory() as temp:
            with closing(connect(temp)) as db:
                series_id, _ = add_from_goodreads_metadata(
                    db, initial_series, series_url, sources={})
                with patch('media_intake.watched.search_google_books_metadata',
                           return_value=[catalog_series]), \
                     patch('media_intake.watched._search_book', return_value=False) as search:
                    process_due_watches(db, temp, force=True)
                    watch = dict(db.execute(
                        'SELECT * FROM watched_items WHERE id=?', (series_id,)).fetchone())
                    _process_google_books_series_collection(db, watch)
                rows = [dict(row) for row in db.execute(
                    'SELECT kind,title,parent_id FROM watched_items ORDER BY id')]
                member_edges = db.execute("""SELECT COUNT(DISTINCT relation.id)
                    FROM media_relationships AS relation
                    JOIN media_relationship_evidence AS evidence
                      ON evidence.relationship_id=relation.id
                    WHERE relation.relationship_type IN ('contains','part_of','parent_series')
                      AND evidence.provider='google books'""").fetchone()[0]
                second_google_id = db.execute("""SELECT COUNT(*) FROM media_external_ids
                    WHERE provider='googlebooks' AND external_id='two'""").fetchone()[0]
                searches = [call.args[1]['title'] for call in search.call_args_list]

        self.assertEqual([(row['kind'], row['title']) for row in rows], [
            ('series', 'Example Series'), ('book', 'Book One'), ('book', 'Book Two')])
        self.assertTrue(all(row['parent_id'] == series_id for row in rows[1:]))
        self.assertEqual(member_edges, 2)
        self.assertEqual(second_google_id, 1)
        self.assertCountEqual(searches, ['Book One', 'Book Two'])

    def test_due_series_scan_searches_each_discovered_book(self):
        page = {'books': [
            {'title': 'Book One', 'author': 'Example Author',
             'source_url': 'https://www.goodreads.com/book/show/1001'},
            {'title': 'Book Two', 'author': 'Example Author',
             'source_url': 'https://www.goodreads.com/book/show/1002'},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            with closing(connect(temp)) as db:
                add_watch(db, 'series', 'Example Series', author='Example Author',
                          source_url='https://www.goodreads.com/series/123')
                with patch('media_intake.watched.fetch_goodreads', return_value=page), \
                     patch('media_intake.watched._search_book', return_value=False) as search:
                    process_due_watches(db, temp, force=True)

        self.assertCountEqual([call.args[1]['title'] for call in search.call_args_list],
                              ['Book One', 'Book Two'])
        self.assertTrue(all(call.args[1]['kind'] == 'book' for call in search.call_args_list))

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
                author_edges = list(db.execute('''SELECT e.provider_entity_id,
                        e.raw_relationship_type FROM media_relationships r
                    JOIN media_relationship_evidence e ON e.relationship_id=r.id
                    WHERE r.relationship_type='created_by' AND e.provider='open library'
                    ORDER BY e.provider_entity_id'''))

        self.assertEqual([row['title'] for row in rows],
                         ['Example Author', 'Older title', 'Upcoming title'])
        self.assertEqual(rows[1]['release_date'], '2001-01-01')
        self.assertEqual(rows[1]['parent_id'], watch_id)
        self.assertEqual(rows[2]['release_date'], '2099-01-01')
        self.assertEqual(rows[2]['parent_id'], watch_id)
        self.assertEqual([tuple(row) for row in author_edges], [
            ('OL11W', 'author bibliography'), ('OL12W', 'author bibliography')])


if __name__ == '__main__':
    unittest.main()
