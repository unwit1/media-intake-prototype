import datetime as dt
import json
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from media_intake.core import connect
from media_intake.media_graph import set_watch_policy, upsert_media_entity
from media_intake.media_resolver import refresh_graph, resolve


class MediaResolverSearchTest(unittest.TestCase):
    def test_category_search_uses_provider_neutral_resolver_entry_point(self):
        response = {'category': 'Books', 'results': [{'title': 'Example'}],
                    'errors': [], 'searched': ['Goodreads']}
        with patch('media_intake.media_resolver.search_media_category',
                   return_value=response) as search:
            result = resolve('Example', 'Books', {'irc': True},
                             {'catalog_key': 'credential'}, ['IRC', 'Anna\'s Archive'])

        self.assertEqual(result, response)
        search.assert_called_once_with(
            'Example', 'Books', {'irc': True}, {'catalog_key': 'credential'},
            ['IRC', 'Anna\'s Archive'])


class MediaResolverGraphRefreshTest(unittest.TestCase):
    def test_openlibrary_author_refresh_keeps_partial_pages_and_reports_errors(self):
        def author_page(url, max_bytes=None):
            if 'offset=200' in url:
                raise ValueError('fixture page unavailable')
            entry = ({'key': '/works/OL11W', 'title': 'First Work',
                      'first_publish_date': '2001'} if 'offset=100' not in url else
                     {'key': '/works/OL12W', 'title': 'Second Work',
                      'first_publish_date': '2020-06-04'})
            size = 201 if 'offset=100' not in url else 201
            return json.dumps({'entries': [entry], 'size': size}).encode(), url

        author_url = 'https://openlibrary.org/authors/OL123A'
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'person', 'Example Author', media_type='book',
                canonical_url=author_url,
                metadata={'catalog_metadata': {'catalog_id': '/authors/OL123A',
                                               'work_count': 201}},
                external_ids=[{'provider': 'openlibrary', 'external_id': 'OL123A',
                               'source_url': author_url}],
                provider='Open Library')
            with patch('media_intake.media_resolver.http_get', side_effect=author_page):
                result = refresh_graph(db, root_id, max_depth=0)
            edges = [dict(row) for row in db.execute('''SELECT r.relationship_type,
                    r.source_entity_id,r.target_entity_id,e.provider_entity_id
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id ORDER BY e.provider_entity_id''')]
            work_ids = {row['external_id']: row['entity_id'] for row in db.execute(
                "SELECT external_id,entity_id FROM media_external_ids "
                "WHERE provider='openlibrary' AND external_id!='OL123A'")}
            watches = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            items = db.execute('SELECT COUNT(*) FROM items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'Open Library', 'external_id': 'OL123A', 'relationships': 2}])
        self.assertEqual(result['errors'], [
            'Open Library OL123A: fixture page unavailable'])
        self.assertEqual([edge['provider_entity_id'] for edge in edges], ['OL11W', 'OL12W'])
        self.assertEqual({edge['relationship_type'] for edge in edges}, {'created_by'})
        self.assertEqual({edge['target_entity_id'] for edge in edges}, {root_id})
        self.assertEqual(set(work_ids), {'OL11W', 'OL12W'})
        self.assertEqual(watches, 0)
        self.assertEqual(items, 0)

    def test_goodreads_author_graph_refresh_links_books_without_creating_watches(self):
        author_url = 'https://www.goodreads.com/author/show/42-example-writer'
        page = {'kind': 'author', 'title': 'Example Writer', 'books': [{
            'title': 'Example Novel', 'author': 'Example Writer',
            'source_url': 'https://www.goodreads.com/book/show/100-example-novel',
        }]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'person', 'Example Writer', media_type='book',
                canonical_url=author_url,
                external_ids=[{'provider': 'goodreads', 'external_id': '42',
                               'source_url': author_url}],
                provider='Goodreads')
            with patch('media_intake.watched.fetch_goodreads', return_value=page):
                result = refresh_graph(db, root_id, max_depth=0)
            edge = dict(db.execute('''SELECT r.relationship_type,r.source_entity_id,
                    r.target_entity_id,r.status,e.provider,e.provider_entity_id,
                    e.raw_relationship_type
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id''').fetchone())
            ids = {row['external_id']: row['entity_id'] for row in db.execute(
                "SELECT external_id,entity_id FROM media_external_ids WHERE provider='goodreads'")}
            watches = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            items = db.execute('SELECT COUNT(*) FROM items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'Goodreads', 'external_id': '42', 'relationships': 1}])
        self.assertEqual((edge['relationship_type'], edge['provider'],
                          edge['provider_entity_id'], edge['raw_relationship_type']),
                         ('created_by', 'goodreads', '100', 'author bibliography'))
        self.assertEqual(edge['source_entity_id'], ids['100'])
        self.assertEqual(edge['target_entity_id'], root_id)
        self.assertEqual(edge['status'], 'accepted')
        self.assertEqual(watches, 0)
        self.assertEqual(items, 0)

    def test_goodreads_series_refresh_updates_graph_without_creating_watches(self):
        series_url = 'https://www.goodreads.com/series/42-example-series'
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Series', media_type='book',
                canonical_url=series_url,
                external_ids=[{'provider': 'goodreads', 'external_id': '42',
                               'source_url': series_url}],
                provider='Goodreads')
            page = {'kind': 'series', 'title': 'Example Series',
                    'collection_error': 'page 2 was unavailable', 'books': [
                {'title': 'First Book', 'author': 'Writer',
                 'source_url': 'https://www.goodreads.com/book/show/100-first-book',
                 'series_index': 1, 'series_position_source': 'Goodreads series listing'},
                {'title': 'Example Series Part One', 'author': 'Writer',
                 'source_url': 'https://www.goodreads.com/book/show/101-part-one',
                 'series_index': 0},
            ]}
            with patch('media_intake.watched.fetch_goodreads', return_value=page):
                result = refresh_graph(db, root_id, max_depth=0)
            edges = [dict(row) for row in db.execute('''
                SELECT r.relationship_type,r.status,e.provider,e.provider_entity_id,
                       e.raw_relationship_type,r.metadata_json
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id''')]
            reviews = [dict(row) for row in db.execute(
                "SELECT issue_type,title FROM media_review_queue WHERE status='open'")]
            watches = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            queue_items = db.execute('SELECT COUNT(*) FROM items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'Goodreads', 'external_id': '42', 'relationships': 1}])
        self.assertEqual(result['errors'], ['Goodreads 42: page 2 was unavailable'])
        self.assertEqual(len(edges), 1)
        self.assertEqual((edges[0]['relationship_type'], edges[0]['provider'],
                          edges[0]['provider_entity_id'], edges[0]['raw_relationship_type']),
                         ('contains', 'goodreads', '100', 'series listing'))
        self.assertEqual(json.loads(edges[0]['metadata_json']), {'series_position': 1})
        self.assertEqual(reviews[0]['issue_type'], 'uncertain_series_member')
        self.assertEqual(watches, 0)
        self.assertEqual(queue_items, 0)

    def test_google_books_series_refresh_preserves_membership_position_graph_only(self):
        series = {'kind': 'series', 'title': 'Example Series', 'books': [{
            'title': 'First Book', 'author': 'Writer',
            'source_url': 'https://books.google.com/books?id=vol-1',
            'series_index': 1, 'series_position_source': 'Google Books seriesInfo',
            'catalog_metadata': {'catalog_id': 'vol-1', 'isbns': ['9780306406157']},
        }]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Series', media_type='book',
                canonical_url='https://books.google.com/books?q=series%3AExample',
                external_ids=[{'provider': 'googlebooks', 'external_id': 'series:Example'}],
                provider='Google Books')
            with patch('media_intake.media_resolver.search_google_books_metadata',
                       return_value=[series]):
                result = refresh_graph(db, root_id, max_depth=0)
            member = dict(db.execute('''SELECT e.title,e.metadata_json,r.metadata_json AS edge_metadata,
                    x.provider,x.external_id
                FROM media_relationships r
                JOIN media_entities e ON e.id=r.target_entity_id
                JOIN media_external_ids x ON x.entity_id=e.id AND x.provider='googlebooks' ''').fetchone())
            watches = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            queue_items = db.execute('SELECT COUNT(*) FROM items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'Google Books', 'external_id': 'series:Example', 'relationships': 1}])
        self.assertEqual((member['title'], member['provider'], member['external_id']),
                         ('First Book', 'googlebooks', 'vol-1'))
        self.assertEqual(json.loads(member['edge_metadata']), {'series_position': 1})
        self.assertIn('9780306406157', json.loads(member['metadata_json'])['isbns'])
        self.assertEqual(watches, 0)
        self.assertEqual(queue_items, 0)

    def test_goodreads_and_google_books_series_refresh_merge_shared_isbn_work(self):
        isbn = '9780306406157'
        series_url = 'https://www.goodreads.com/series/42-example-series'
        goodreads_page = {'kind': 'series', 'title': 'Example Series', 'books': [{
            'title': 'Example Novel', 'author': 'Example Writer',
            'source_url': 'https://www.goodreads.com/book/show/100-example-novel',
            'catalog_metadata': {'isbns': [isbn]},
        }]}
        google_series = {'kind': 'series', 'title': 'Example Series', 'books': [{
            'title': 'Example Novel', 'author': 'Example Writer',
            'source_url': 'https://books.google.com/books?id=vol-100',
            'series_index': 1,
            'catalog_metadata': {'catalog_id': 'vol-100', 'isbns': [isbn]},
        }]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Series', media_type='book',
                canonical_url=series_url,
                external_ids=[
                    {'provider': 'goodreads', 'external_id': '42',
                     'source_url': series_url},
                    {'provider': 'googlebooks', 'external_id': 'series:Example'},
                ], provider='Test')
            with patch('media_intake.watched.fetch_goodreads',
                       return_value=goodreads_page), patch(
                    'media_intake.media_resolver.search_google_books_metadata',
                    return_value=[google_series]):
                result = refresh_graph(db, root_id, max_depth=0)
            work_rows = [dict(row) for row in db.execute('''SELECT e.id,e.title
                FROM media_entities e WHERE e.entity_type='work' AND e.media_type='book' ''')]
            work_ids = {row['entity_id'] for row in db.execute('''
                SELECT entity_id FROM media_external_ids
                WHERE (provider='goodreads' AND external_id='100')
                   OR (provider='googlebooks' AND external_id='vol-100')
                   OR (provider='isbn' AND external_id=?)''', (isbn,))}
            conflict_count = db.execute('''SELECT COUNT(*) FROM media_review_queue
                WHERE issue_type='catalog_identity_conflict' ''').fetchone()[0]

        self.assertEqual(len(work_rows), 1)
        self.assertEqual(work_rows[0]['title'], 'Example Novel')
        self.assertEqual(work_ids, {work_rows[0]['id']})
        self.assertEqual(conflict_count, 0)
        self.assertEqual({item['provider'] for item in result['refreshed']},
                         {'Goodreads', 'Google Books'})

    def test_graph_refresh_does_not_merge_shared_isbn_when_authors_conflict(self):
        isbn = '9780306406157'
        series_url = 'https://www.goodreads.com/series/42-example-series'
        goodreads_page = {'kind': 'series', 'title': 'Example Series', 'books': [{
            'title': 'Example Novel', 'author': 'Different Writer',
            'source_url': 'https://www.goodreads.com/book/show/100-example-novel',
            'catalog_metadata': {'isbns': [isbn]},
        }]}
        google_series = {'kind': 'series', 'title': 'Example Series', 'books': [{
            'title': 'Example Novel', 'author': 'Example Writer',
            'source_url': 'https://books.google.com/books?id=vol-100',
            'series_index': 1,
            'catalog_metadata': {'catalog_id': 'vol-100', 'isbns': [isbn]},
        }]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Series', media_type='book',
                canonical_url=series_url,
                external_ids=[
                    {'provider': 'goodreads', 'external_id': '42',
                     'source_url': series_url},
                    {'provider': 'googlebooks', 'external_id': 'series:Example'},
                ], provider='Test')
            with patch('media_intake.watched.fetch_goodreads',
                       return_value=goodreads_page), patch(
                    'media_intake.media_resolver.search_google_books_metadata',
                    return_value=[google_series]):
                refresh_graph(db, root_id, max_depth=0)
            works = list(db.execute('''SELECT id FROM media_entities
                WHERE entity_type='work' AND media_type='book' '''))
            linked_isbn = list(db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider='isbn' AND external_id=?''', (isbn,)))
            conflicts = list(db.execute('''SELECT issue_type FROM media_review_queue
                WHERE issue_type='catalog_identity_conflict' ''').fetchall())

        self.assertEqual(len(works), 2)
        self.assertEqual(len(linked_isbn), 1)
        self.assertEqual(len(conflicts), 1)

    def test_anilist_continuation_policies_follow_target_type_and_release_date(self):
        future = dt.date.today() + dt.timedelta(days=40)
        details = {
            'id': 20, 'siteUrl': 'https://anilist.co/anime/20',
            'relations': {'edges': [
                {'relationType': 'SEQUEL', 'node': {
                    'id': 21, 'type': 'ANIME', 'format': 'TV',
                    'siteUrl': 'https://anilist.co/anime/21',
                    'title': {'english': 'Sequel Series'},
                    'startDate': {'year': future.year, 'month': future.month,
                                  'day': future.day}}},
                {'relationType': 'SEQUEL', 'node': {
                    'id': 22, 'type': 'ANIME', 'format': 'MOVIE',
                    'siteUrl': 'https://anilist.co/anime/22',
                    'title': {'english': 'Sequel Movie'},
                    'startDate': {'year': 2020, 'month': 2, 'day': 3}}},
                {'relationType': 'SEQUEL', 'node': {
                    'id': 23, 'type': 'ANIME', 'format': 'MOVIE',
                    'siteUrl': 'https://anilist.co/anime/23',
                    'title': {'english': 'Upcoming Sequel Movie'},
                    'startDate': {'year': future.year, 'month': future.month,
                                  'day': future.day}}},
                {'relationType': 'PREQUEL', 'node': {
                    'id': 24, 'type': 'ANIME', 'format': 'TV_SHORT',
                    'siteUrl': 'https://anilist.co/anime/24',
                    'title': {'english': 'Prequel Series'},
                    'startDate': {'year': 2018, 'month': 5, 'day': 6}}},
                {'relationType': 'PREQUEL', 'node': {
                    'id': 25, 'type': 'ANIME', 'format': 'MOVIE',
                    'siteUrl': 'https://anilist.co/anime/25',
                    'title': {'english': 'Prequel Movie'},
                    'startDate': {'year': 2017, 'month': 5, 'day': 6}}},
            ]},
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Anime', media_type='video',
                canonical_url='https://anilist.co/anime/20',
                external_ids=[{'provider': 'anilist', 'external_id': '20'}],
                provider='AniList')
            set_watch_policy(db, 'media_type', 'video', {
                'direct_sequel_series': 'discover',
                'direct_prequel_series': 'discover',
                'direct_sequel_works': 'auto_add',
                'direct_prequel_works': 'auto_add',
                'future_installments': 'ignore',
            })
            with patch('media_intake.media_resolver.fetch_anilist_media',
                       return_value=details):
                refresh_graph(db, root_id, max_depth=0)
            rows = {row['external_id']: dict(row) for row in db.execute('''
                SELECT x.external_id,e.entity_type,r.relationship_type,r.status
                FROM media_external_ids x JOIN media_entities e ON e.id=x.entity_id
                JOIN media_relationships r ON r.source_entity_id=e.id OR r.target_entity_id=e.id
                WHERE x.provider='anilist' AND x.external_id!='20'
                ORDER BY x.external_id''')}
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual({key: (row['entity_type'], row['relationship_type'], row['status'])
                          for key, row in rows.items()}, {
            '21': ('series', 'sequel_to', 'candidate'),
            '22': ('work', 'sequel_to', 'accepted'),
            '23': ('work', 'sequel_to', 'ignored'),
            '24': ('series', 'prequel_to', 'candidate'),
            '25': ('work', 'prequel_to', 'accepted'),
        })
        self.assertEqual(watched_count, 0)

    def test_anilist_relationship_refresh_is_provider_neutral_and_graph_only(self):
        details = {
            'id': 10, 'siteUrl': 'https://anilist.co/anime/10',
            'relations': {'edges': [
                {'relationType': 'SEQUEL', 'node': {
                    'id': 11, 'type': 'ANIME', 'format': 'TV',
                    'siteUrl': 'https://anilist.co/anime/11',
                    'title': {'english': 'Example Sequel'},
                    'startDate': {'year': 2027, 'month': 4, 'day': 1},
                    'status': 'NOT_YET_RELEASED'}},
                {'relationType': 'REMAKE', 'node': {
                    'id': 12, 'type': 'ANIME', 'format': 'MOVIE',
                    'siteUrl': 'https://anilist.co/anime/12',
                    'title': {'english': 'Example Remake'},
                    'startDate': {'year': 2028, 'month': 1, 'day': 2},
                    'status': 'NOT_YET_RELEASED'}},
            ]},
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Anime', media_type='video',
                canonical_url='https://anilist.co/anime/10',
                external_ids=[{'provider': 'anilist', 'external_id': '10'}],
                provider='AniList')
            with patch('media_intake.media_resolver.fetch_anilist_media',
                       return_value=details):
                result = refresh_graph(db, root_id, max_depth=0)
            edges = [dict(row) for row in db.execute('''SELECT r.relationship_type,
                    r.source_entity_id,r.target_entity_id,r.status,e.raw_relationship_type
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id ORDER BY r.relationship_type''')]
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            ids = {row['external_id']: row['entity_id'] for row in db.execute(
                "SELECT external_id,entity_id FROM media_external_ids WHERE provider='anilist'")}

        self.assertEqual(result['refreshed'], [{
            'provider': 'AniList', 'external_id': '10', 'relationships': 2}])
        self.assertEqual([(row['relationship_type'], row['raw_relationship_type'])
                          for row in edges], [('remake_of', 'REMAKE'),
                                              ('sequel_to', 'SEQUEL')])
        self.assertEqual(edges[0]['source_entity_id'], ids['12'])
        self.assertEqual(edges[0]['target_entity_id'], root_id)
        self.assertEqual(edges[1]['source_entity_id'], ids['11'])
        self.assertEqual(edges[1]['target_entity_id'], root_id)
        self.assertEqual(watched_count, 0)

    def test_graph_refresh_walks_relationships_to_requested_depth(self):
        def media_record(media_id, sequel_id=None, sequel_title=None):
            relations = []
            if sequel_id:
                relations.append({'relationType': 'SEQUEL', 'node': {
                    'id': sequel_id, 'type': 'MANGA', 'format': 'MANGA',
                    'siteUrl': f'https://anilist.co/manga/{sequel_id}',
                    'title': {'english': sequel_title}}})
            return {'id': media_id, 'siteUrl': f'https://anilist.co/manga/{media_id}',
                    'relations': {'edges': relations}}

        catalog = {
            '10': media_record(10, 11, 'Second'),
            '11': media_record(11, 12, 'Third'),
            '12': media_record(12),
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'First', media_type='manga',
                canonical_url='https://anilist.co/manga/10',
                external_ids=[{'provider': 'anilist', 'external_id': '10'}],
                provider='AniList')
            with patch('media_intake.media_resolver.fetch_anilist_media',
                       side_effect=lambda media_id: catalog[str(media_id)]):
                result = refresh_graph(db, root_id, max_depth=2)
            ids = {row['external_id']: row['entity_id'] for row in db.execute(
                "SELECT external_id,entity_id FROM media_external_ids "
                "WHERE provider='anilist'")}
            edges = [dict(row) for row in db.execute('''SELECT source_entity_id,
                target_entity_id,relationship_type FROM media_relationships''')]
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual({row['external_id'] for row in result['refreshed']},
                         {'10', '11', '12'})
        self.assertEqual(result['entities_visited'], 3)
        self.assertEqual(len(edges), 2)
        self.assertTrue(any(row['source_entity_id'] == ids['12'] and
                            row['target_entity_id'] == ids['11'] and
                            row['relationship_type'] == 'sequel_to' for row in edges))
        self.assertEqual(len(result['graph']['entities']), 3)
        self.assertEqual(watched_count, 0)

    def test_graph_refresh_reports_entity_bound_without_adding_watches(self):
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(db, 'work', 'Root', media_type='book')
            for index in range(101):
                child_id = upsert_media_entity(
                    db, 'work', f'Child {index}', media_type='book',
                    provider='fixture')
                db.execute('''INSERT INTO media_relationships
                    (source_entity_id,target_entity_id,relationship_type,confidence,
                     inferred,status,metadata_json,created_at,updated_at)
                    VALUES (?,?,?,0.9,0,'candidate','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)''',
                    (root_id, child_id, 'same_universe'))
            result = refresh_graph(db, root_id, max_depth=1)
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual(result['entities_visited'], 100)
        self.assertTrue(result['refresh_truncated'])
        self.assertEqual(watched_count, 0)

    def test_tmdb_movie_refresh_links_collection_members_without_self_edges(self):
        details = {'id': 100, 'belongs_to_collection': {
            'id': 500, 'name': 'Example Collection'}}
        collection = {'id': 500, 'name': 'Example Collection', 'parts': [
            {'id': 100, 'title': 'Original Movie', 'release_date': '2020-01-01'},
            {'id': 101, 'title': 'Follow-up Movie', 'release_date': '2024-02-03'},
        ]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'work', 'Original Movie', media_type='video',
                canonical_url='https://www.themoviedb.org/movie/100',
                external_ids=[{'provider': 'tmdb_movie', 'external_id': '100'}],
                provider='TMDB')
            with patch('media_intake.media_resolver.fetch_tmdb_media',
                       return_value=details), patch(
                    'media_intake.media_resolver.fetch_tmdb_collection',
                    return_value=collection):
                result = refresh_graph(db, root_id, max_depth=0,
                                       source_options={'tmdb_api_key': 'test-key'})
            edges = [dict(row) for row in db.execute('''SELECT relationship_type,
                    source_entity_id,target_entity_id FROM media_relationships''')]
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            follow_up = db.execute('''SELECT e.id FROM media_entities e
                JOIN media_external_ids x ON x.entity_id=e.id
                WHERE x.provider='tmdb_movie' AND x.external_id='101' ''').fetchone()
            collection_entity = db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider='tmdb_collection' AND external_id='500' ''').fetchone()

        self.assertEqual(result['refreshed'], [{
            'provider': 'TMDB', 'external_id': '100', 'relationships': 3}])
        self.assertEqual(len(edges), 3)
        self.assertTrue(all(row['source_entity_id'] != row['target_entity_id']
                            for row in edges))
        self.assertEqual({row['relationship_type'] for row in edges}, {'part_of', 'contains'})
        self.assertEqual(watched_count, 0)
        self.assertIsNotNone(follow_up)
        self.assertTrue(any(row['source_entity_id'] == root_id and
                            row['target_entity_id'] == collection_entity['entity_id']
                            for row in edges))
        self.assertTrue(any(row['source_entity_id'] == collection_entity['entity_id'] and
                            row['target_entity_id'] == follow_up['id']
                            for row in edges))

    def test_tmdb_tv_refresh_links_next_episode_and_records_air_date(self):
        details = {'id': 700, 'next_episode_to_air': {
            'id': 701, 'name': 'The Next Episode', 'season_number': 2,
            'episode_number': 4, 'air_date': '2027-04-09'}}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Show', media_type='video',
                canonical_url='https://www.themoviedb.org/tv/700',
                external_ids=[{'provider': 'tmdb_tv', 'external_id': '700'}],
                provider='TMDB')
            with patch('media_intake.media_resolver.fetch_tmdb_media',
                       return_value=details):
                result = refresh_graph(db, root_id, max_depth=0,
                                       source_options={'tmdb_api_key': 'test-key'})
            edges = [dict(row) for row in db.execute('''SELECT relationship_type,
                    source_entity_id,target_entity_id FROM media_relationships''')]
            release = dict(db.execute('''SELECT release_date,status,provider
                FROM media_releases WHERE provider='TMDB' ''').fetchone())
            episode_id = db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider='tmdb_episode' AND external_id='701' ''').fetchone()['entity_id']

        self.assertEqual(result['refreshed'], [{
            'provider': 'TMDB', 'external_id': '700', 'relationships': 1}])
        self.assertEqual({row['relationship_type'] for row in edges},
                         {'contains', 'episode_of'})
        self.assertTrue(any(row['source_entity_id'] == episode_id and
                            row['target_entity_id'] == root_id and
                            row['relationship_type'] == 'episode_of' for row in edges))
        self.assertEqual(release, {'release_date': '2027-04-09',
                                   'status': 'upcoming', 'provider': 'TMDB'})

    def test_tvmaze_refresh_links_seasons_episodes_and_specials_without_watches(self):
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Show', media_type='video',
                canonical_url='https://www.tvmaze.com/shows/88/example-show',
                external_ids=[{'provider': 'tvmaze', 'external_id': '88'}],
                provider='TVMaze')
            with patch('media_intake.media_resolver.fetch_tvmaze_show', return_value={
                    'id': 88, 'name': 'Example Show'}), patch(
                    'media_intake.media_resolver.list_tvmaze_seasons', return_value=[{
                        'id': 880, 'number': 1, 'premiereDate': '2026-09-01'}]), patch(
                    'media_intake.media_resolver.list_tvmaze_episodes', return_value=[
                        {'id': 881, 'name': 'First', 'season': 1, 'number': 1,
                         'airdate': '2026-09-01', 'url': 'https://www.tvmaze.com/episodes/881'},
                        {'id': 882, 'name': 'Special', 'season': 0, 'number': 1,
                         'type': 'special', 'airdate': '2026-09-02',
                         'url': 'https://www.tvmaze.com/episodes/882'},
                    ]):
                result = refresh_graph(db, root_id, max_depth=0)
            edges = [dict(row) for row in db.execute('''SELECT r.relationship_type,
                    r.source_entity_id,r.target_entity_id,r.status,e.provider_entity_id
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id ORDER BY r.relationship_type,e.provider_entity_id''')]
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'TVMaze', 'external_id': '88', 'relationships': 3}])
        relationships = {(row['relationship_type'], row['provider_entity_id'])
                         for row in edges}
        self.assertEqual(relationships, {
            ('contains', '880'), ('season_of', '880'),
            ('contains', '881'), ('episode_of', '881'),
            ('contains', '882'), ('special_of', '882'),
        })
        self.assertEqual(watched_count, 0)

    def test_igdb_refresh_normalizes_versions_and_related_game_content(self):
        details = {
            'id': 100, 'category': 8, 'slug': 'example-remake',
            'franchises': [{'id': 200, 'name': 'Example Franchise',
                            'slug': 'example-franchise'}],
            'collection': {'id': 300, 'name': 'Example Collection',
                           'slug': 'example-collection'},
            'version_parent': {'id': 90, 'name': 'Original Game'},
            'parent_game': None,
            'dlcs': [{'id': 101, 'name': 'Example DLC', 'slug': 'example-dlc'}],
            'expansions': [], 'standalone_expansions': [],
            'remakes': [{'id': 110, 'name': 'Follow-up Remake',
                         'slug': 'follow-up-remake'}],
            'remasters': [], 'ports': [],
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'work', 'Example Remake', media_type='game',
                canonical_url='https://www.igdb.com/games/example-remake',
                metadata={'game_subtype': 'remake'},
                external_ids=[{'provider': 'igdb', 'external_id': '100'}],
                provider='IGDB')
            with patch('media_intake.media_resolver.fetch_igdb_game',
                       return_value=details):
                result = refresh_graph(db, root_id, max_depth=0, source_options={
                    'igdb_client_id': 'test-client',
                    'igdb_client_secret': 'test-secret'})
            edges = [dict(row) for row in db.execute('''SELECT r.relationship_type,
                    r.source_entity_id,r.target_entity_id,r.status,e.raw_relationship_type,
                    x.external_id FROM media_relationships r
                JOIN media_relationship_evidence e ON e.relationship_id=r.id
                JOIN media_external_ids x ON x.entity_id=CASE
                    WHEN r.source_entity_id=? THEN r.target_entity_id
                    ELSE r.source_entity_id END AND x.provider IN
                    ('igdb','igdb_collection','igdb_franchise')
                WHERE r.source_entity_id=? OR r.target_entity_id=?''',
                (root_id, root_id, root_id))]
            ids = {row['external_id']: row['entity_id'] for row in db.execute(
                "SELECT external_id,entity_id FROM media_external_ids WHERE provider='igdb'")}
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'IGDB', 'external_id': '100', 'relationships': 5}])
        relation_types = {(row['relationship_type'], row['raw_relationship_type'])
                          for row in edges}
        self.assertEqual(relation_types, {
            ('part_of', 'franchises'), ('part_of', 'collection'),
            ('remake_of', 'version_parent'), ('contains', 'dlcs'),
            ('remake_of', 'remakes'),
        })
        self.assertNotEqual(ids['90'], root_id)
        self.assertTrue(any(row['source_entity_id'] == root_id and
                            row['target_entity_id'] == ids['90'] and
                            row['raw_relationship_type'] == 'version_parent'
                            for row in edges))
        self.assertTrue(any(row['source_entity_id'] == ids['110'] and
                            row['target_entity_id'] == root_id and
                            row['raw_relationship_type'] == 'remakes'
                            for row in edges))
        self.assertEqual(watched_count, 0)

    def test_steam_refresh_links_dlc_as_game_content(self):
        def steam_details(app_id):
            if str(app_id) == '400':
                return {'name': 'Example Game', 'dlc': [399, 401]}
            if str(app_id) == '399':
                raise ValueError('temporary fixture failure')
            return {'name': 'Example Expansion'}

        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'work', 'Example Game', media_type='game',
                canonical_url='https://store.steampowered.com/app/400/',
                external_ids=[{'provider': 'steam', 'external_id': '400'}],
                provider='Steam')
            with patch('media_intake.media_resolver.fetch_steam_game',
                       side_effect=steam_details):
                result = refresh_graph(db, root_id, max_depth=0)
            edge = dict(db.execute('''SELECT r.relationship_type,r.source_entity_id,
                    r.target_entity_id,r.status,e.raw_relationship_type
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id''').fetchone())
            dlc_id = db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider='steam' AND external_id='401' ''').fetchone()['entity_id']
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'Steam', 'external_id': '400', 'relationships': 1}])
        self.assertEqual(result['errors'], [
            'Steam 400 DLC 399: temporary fixture failure'])
        self.assertEqual(edge['relationship_type'], 'contains')
        self.assertEqual(edge['raw_relationship_type'], 'dlc')
        self.assertEqual(edge['source_entity_id'], root_id)
        self.assertEqual(edge['target_entity_id'], dlc_id)
        self.assertEqual(edge['status'], 'candidate')
        self.assertEqual(watched_count, 0)

    def test_openlibrary_refresh_links_series_members_and_positions(self):
        results = [{'kind': 'series', 'title': 'Example Series', 'books': [
            {'title': 'First Book', 'author': 'Writer',
             'source_url': 'https://openlibrary.org/works/OL1W',
             'release_date': '2020-01-01', 'series_index': 1,
             'series_position_source': 'Open Library series field',
             'catalog_metadata': {'catalog_id': '/works/OL1W'}},
            {'title': 'Second Book', 'author': 'Writer',
             'source_url': 'https://openlibrary.org/works/OL2W',
             'release_date': '2021-01-01', 'series_index': 1.5,
             'series_position_source': 'Open Library series field',
             'catalog_metadata': {'catalog_id': '/works/OL2W'}},
        ]}]
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Series', media_type='book',
                canonical_url='https://openlibrary.org/search.json?series=Example',
                external_ids=[{'provider': 'openlibrary',
                               'external_id': 'series:Example'}],
                provider='Open Library')
            with patch('media_intake.media_resolver.search_openlibrary_metadata',
                       return_value=results):
                result = refresh_graph(db, root_id, max_depth=0)
            edges = [dict(row) for row in db.execute('''SELECT r.relationship_type,
                    r.source_entity_id,r.target_entity_id,r.metadata_json,e.provider_entity_id
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id''')]
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            member_ids = {row['external_id']: row['entity_id'] for row in db.execute(
                "SELECT external_id,entity_id FROM media_external_ids WHERE provider='openlibrary'")}

        self.assertEqual(result['refreshed'], [{
            'provider': 'Open Library', 'external_id': 'series:Example',
            'relationships': 2}])
        self.assertEqual({row['relationship_type'] for row in edges}, {'contains'})
        self.assertEqual(member_ids['/works/OL1W'], edges[0]['target_entity_id'])
        second_metadata = json.loads(next(
            row['metadata_json'] for row in edges
            if row['provider_entity_id'] == '/works/OL2W'))
        self.assertEqual(second_metadata, {'series_position': 1.5})
        self.assertTrue(all(row['source_entity_id'] == root_id for row in edges))
        self.assertEqual(watched_count, 0)

    def test_ranobedb_refresh_retains_english_volume_dates_in_graph(self):
        future = '20271017'
        details = {'id': 42, 'title': 'Example Light Novel', 'books': [
            {'id': 4201, 'title': 'Volume One',
             'c_release_dates': {'en': 20200102, 'ja': 20190101}},
            {'id': 4202, 'title': 'Volume Two',
             'c_release_dates': {'en': int(future)}},
            {'id': 4203, 'title': 'Original Only',
             'c_release_dates': {'ja': 20170101}},
        ]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = upsert_media_entity(
                db, 'series', 'Example Light Novel', media_type='book',
                canonical_url='https://ranobedb.org/series/42',
                external_ids=[{'provider': 'ranobedb', 'external_id': '42'}],
                provider='RanobeDB')
            with patch('media_intake.media_resolver.ranobedb_series',
                       return_value=details):
                result = refresh_graph(db, root_id, max_depth=0)
            volumes = [dict(row) for row in db.execute('''SELECT e.title,e.release_date,
                    e.metadata_json,x.external_id FROM media_entities e
                JOIN media_external_ids x ON x.entity_id=e.id
                WHERE x.provider='ranobedb' AND x.external_id!='42' ORDER BY x.external_id''')]
            watched_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual(result['refreshed'], [{
            'provider': 'RanobeDB', 'external_id': '42', 'relationships': 3}])
        self.assertEqual(volumes[0]['release_date'], '2020-01-02')
        self.assertTrue(json.loads(volumes[0]['metadata_json'])['english_official'])
        self.assertEqual(volumes[1]['release_date'], '2027-10-17')
        self.assertEqual(volumes[2]['release_date'], None)
        self.assertIsNone(json.loads(volumes[2]['metadata_json'])['english_official'])
        self.assertEqual(watched_count, 0)


if __name__ == '__main__':
    unittest.main()
