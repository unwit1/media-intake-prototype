import datetime as dt
import json
import sqlite3
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from media_intake.core import connect
from media_intake.media_graph import (add_media_relationship, capture_graph_snapshot,
                                     effective_watch_policy, list_graph_reviews, queue_graph_review,
                                     resolve_graph_review, _sync_watch_row,
                                     set_reviewed_media_release_date,
                                     set_reviewed_media_series_index, set_reviewed_media_status,
                                     set_reviewed_release_date, upsert_media_entity,
                                     upsert_media_release)
from media_intake.media_graph import set_watch_policy
from media_intake.watched import (add_watch, _add_catalog_book_watch,
                                  _apply_wikidata_watch_policy,
                                  _sync_series_members_graph,
                                  _search_wikidata_series,
                                  _wikidata_series_members)
from media_intake.wikidata import import_wikidata_entity, search_wikidata


class MediaGraphTest(unittest.TestCase):
    def test_existing_release_table_migrates_canonical_observation_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(sqlite3.connect(Path(tmp) / 'intake.sqlite3')) as legacy_db:
                legacy_db.executescript('''
                    CREATE TABLE media_releases (
                        id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL,
                        release_type TEXT NOT NULL DEFAULT 'publication', release_date TEXT,
                        region TEXT, platform TEXT, status TEXT, provider TEXT NOT NULL DEFAULT '',
                        provider_release_id TEXT, source_url TEXT, observed_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        UNIQUE(entity_id,provider,provider_release_id,release_type));
                ''')
            with closing(connect(tmp)) as db:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(media_releases)')}

        self.assertIn('is_canonical', columns)

    def test_new_watch_is_linked_to_entity_and_series_parent(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            series_id = add_watch(
                db, 'series', 'Example Series', source_url='https://www.goodreads.com/series/123',
                metadata={'catalog_provider': 'Open Library',
                          'catalog_metadata': {'catalog_id': '/series/OL123S'}})
            child_id = add_watch(db, 'book', 'Book One', source_url='https://openlibrary.org/works/OL123W',
                                 parent_id=series_id)
            parent = db.execute('SELECT * FROM watched_items WHERE id=?', (series_id,)).fetchone()
            child = db.execute('SELECT * FROM watched_items WHERE id=?', (child_id,)).fetchone()
            self.assertIsNotNone(parent['media_entity_id'])
            self.assertIsNotNone(child['media_entity_id'])
            relationship = db.execute('''SELECT relationship_type FROM media_relationships
                WHERE source_entity_id=? AND target_entity_id=?''',
                                      (child['media_entity_id'], parent['media_entity_id'])).fetchone()
            self.assertEqual(relationship['relationship_type'], 'part_of')
            self.assertTrue(db.execute('''SELECT 1 FROM media_external_ids
                WHERE entity_id=? AND provider='openlibrary' AND external_id='/series/OL123S' ''',
                                       (parent['media_entity_id'],)).fetchone())

    def test_existing_watch_database_is_migrated_without_losing_parent_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'intake.sqlite3'
            raw = sqlite3.connect(path)
            raw.executescript('''
                CREATE TABLE items (id INTEGER PRIMARY KEY,url TEXT UNIQUE,metadata TEXT);
                CREATE TABLE watched_items (
                    id INTEGER PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,
                    author TEXT NOT NULL DEFAULT 'Unknown',source_url TEXT,release_date TEXT,
                    state TEXT NOT NULL,next_check_at TEXT,last_checked_at TEXT,last_error TEXT,
                    file_path TEXT,parent_id INTEGER,metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE gui_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO watched_items(id,kind,title,source_url,state,metadata)
                    VALUES (1,'series','Old Series','https://www.goodreads.com/series/456','series','{}');
                INSERT INTO watched_items(id,kind,title,source_url,state,parent_id,metadata)
                    VALUES (2,'book','Old Book','https://openlibrary.org/works/OL456W','watching',1,'{}');
            ''')
            raw.commit()
            raw.close()

            with closing(connect(tmp)) as db:
                rows = db.execute('SELECT id,media_entity_id FROM watched_items ORDER BY id').fetchall()
                self.assertTrue(all(row['media_entity_id'] for row in rows))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM media_entities').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM media_relationships').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM media_relationship_evidence').fetchone()[0], 1)

    def test_provider_evidence_is_preserved_without_duplicate_relationships(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            first = upsert_media_entity(db, 'work', 'First', external_ids=[('fixture', 'first')])
            second = upsert_media_entity(db, 'work', 'Second', external_ids=[('fixture', 'second')])
            relationship_id = add_media_relationship(
                db, first, second, 'follows', confidence=.9, status='accepted',
                provider='Wikidata', provider_entity_id='Q2', raw_relationship_type='P155',
                source_url='https://www.wikidata.org/entity/Q1', evidence={'property_id': 'P155'})
            same_relationship = add_media_relationship(
                db, first, second, 'follows', confidence=.8, status='candidate',
                provider='BookBrainz', provider_entity_id='bb:2', raw_relationship_type='follows',
                source_url='https://bookbrainz.org/example', evidence={'catalog_id': 'bb:2'})
            add_media_relationship(db, first, second, 'same_universe', confidence=.7,
                                   provider='Fixture', raw_relationship_type='shared setting')
            self.assertEqual(relationship_id, same_relationship)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_relationships').fetchone()[0], 2)
            self.assertEqual(db.execute('''SELECT COUNT(*) FROM media_relationship_evidence
                WHERE relationship_id=?''', (relationship_id,)).fetchone()[0], 2)

    def test_conflicting_catalog_dates_and_statuses_enter_review_with_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            entity_id = upsert_media_entity(
                db, 'work', 'Example Release', media_type='video',
                canonical_url='https://www.themoviedb.org/movie/1',
                release_date='2030-04-05', status='upcoming',
                external_ids=[('tmdb_movie', '1')], provider='TMDB')
            upsert_media_entity(
                db, 'work', 'Example Release', media_type='video',
                canonical_url='https://www.tvmaze.com/shows/2',
                release_date='2030-05-06', status='ongoing', entity_id=entity_id,
                external_ids=[('tvmaze', '2')], provider='TVMaze')
            # Repeated refresh observations update the same claims and do not
            # create another copy of an already-open disagreement.
            upsert_media_entity(
                db, 'work', 'Example Release', media_type='video',
                canonical_url='https://www.tvmaze.com/shows/2',
                release_date='2030-05-06', status='ongoing', entity_id=entity_id,
                external_ids=[('tvmaze', '2')], provider='TVMaze')
            reviews = [item for item in list_graph_reviews(db)
                       if item['issue_type'] == 'conflicting_metadata']

        self.assertEqual({item['detail']['field'] for item in reviews},
                         {'release_date', 'status'})
        release_review = next(item for item in reviews
                              if item['detail']['field'] == 'release_date')
        self.assertEqual({claim['provider'] for claim in release_review['detail']['claims']},
                         {'tmdb', 'tvmaze'})
        self.assertEqual({claim['value'] for claim in release_review['detail']['claims']},
                         {'2030-04-05', '2030-05-06'})

    def test_strict_provider_majority_reconciles_lifecycle_status_and_audits_review(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            watch_id = add_watch(db, 'video', 'Example Series', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series', 'sources': {}})
            entity_id = int(db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                       (watch_id,)).fetchone()[0])
            for provider, status in (('TVMaze', 'ongoing'),
                                     ('TMDB', 'cancelled'),
                                     ('IGDB', 'ongoing')):
                upsert_media_entity(db, 'series', 'Example Series', media_type='video',
                                    status=status, entity_id=entity_id, provider=provider)
            entity = db.execute('SELECT status,metadata_json FROM media_entities WHERE id=?',
                                (entity_id,)).fetchone()
            watch = db.execute('SELECT metadata FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()
            review = db.execute('''SELECT status,detail_json FROM media_review_queue
                WHERE entity_id=? AND issue_type='conflicting_metadata'
                  AND json_extract(detail_json,'$.field')='status'
                ORDER BY id DESC LIMIT 1''', (entity_id,)).fetchone()
            canonical = db.execute('''SELECT provider,value_json,is_canonical
                FROM media_claim_observations WHERE entity_id=? AND field_name='status'
                AND is_canonical=1''', (entity_id,)).fetchone()

        entity_metadata = json.loads(entity['metadata_json'])
        watch_metadata = json.loads(watch['metadata'])
        review_detail = json.loads(review['detail_json'])
        self.assertEqual(entity['status'], 'ongoing')
        self.assertEqual(entity_metadata['status_source'], 'provider_consensus')
        self.assertEqual(set(entity_metadata['canonical_status_observation']['supporting_providers']),
                         {'igdb', 'tvmaze'})
        self.assertEqual(watch_metadata['catalog_status'], 'ongoing')
        self.assertEqual(watch_metadata['catalog_status_source'], 'provider_consensus')
        self.assertEqual(review['status'], 'accepted')
        self.assertEqual(review_detail['automatic_resolution']['method'],
                         'strict provider majority')
        self.assertEqual((canonical['value_json'], canonical['is_canonical']), ('"ongoing"', 1))

    def test_lifecycle_status_tie_remains_in_needs_review(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            entity_id = upsert_media_entity(db, 'series', 'Example Series', media_type='video')
            for provider, status in (('TVMaze', 'ongoing'),
                                     ('TMDB', 'cancelled'),
                                     ('IGDB', 'hiatus')):
                upsert_media_entity(db, 'series', 'Example Series', media_type='video',
                                    status=status, entity_id=entity_id, provider=provider)
            entity = db.execute('SELECT metadata_json FROM media_entities WHERE id=?',
                                (entity_id,)).fetchone()
            reviews = [item for item in list_graph_reviews(db)
                       if item['issue_type'] == 'conflicting_metadata' and
                       item['detail'].get('field') == 'status']

        self.assertNotEqual(json.loads(entity['metadata_json']).get('status_source'),
                            'provider_consensus')
        self.assertTrue(reviews)

    def test_reviewed_lifecycle_status_stays_canonical_after_catalog_refresh(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            watch_id = add_watch(db, 'video', 'Example Series', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series', 'sources': {}})
            entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (watch_id,)).fetchone()[0])
            upsert_media_entity(
                db, 'series', 'Example Series', media_type='video', status='ongoing',
                entity_id=entity_id, provider='TVMaze',
                canonical_url='https://www.tvmaze.com/shows/21')
            upsert_media_entity(
                db, 'series', 'Example Series', media_type='video', status='cancelled',
                entity_id=entity_id, provider='TMDB',
                canonical_url='https://www.themoviedb.org/tv/21')
            review = next(item for item in list_graph_reviews(db)
                          if item['issue_type'] == 'conflicting_metadata'
                          and item['detail']['field'] == 'status')
            cancelled_claim = next(claim for claim in review['detail']['claims']
                                   if claim['value'] == 'cancelled')

            selected = set_reviewed_media_status(db, review['id'], cancelled_claim['id'])
            entity = db.execute('SELECT status,metadata_json FROM media_entities WHERE id=?',
                                (entity_id,)).fetchone()
            chosen_claims = db.execute('''SELECT provider,is_canonical FROM media_claim_observations
                WHERE entity_id=? AND field_name='status' ORDER BY provider''', (entity_id,)).fetchall()
            watch = db.execute('SELECT metadata FROM watched_items WHERE id=?', (watch_id,)).fetchone()
            review_status = db.execute('SELECT status FROM media_review_queue WHERE id=?',
                                       (review['id'],)).fetchone()['status']

            # A later provider refresh carries a stale status. It must remain
            # evidence, without replacing the user's reviewed canonical value.
            upsert_media_entity(
                db, 'series', 'Example Series', media_type='video', status='ongoing',
                entity_id=entity_id, provider='TVMaze',
                canonical_url='https://www.tvmaze.com/shows/21')
            refreshed_entity = db.execute('SELECT status FROM media_entities WHERE id=?',
                                          (entity_id,)).fetchone()
            refreshed_watch = db.execute('SELECT * FROM watched_items WHERE id=?',
                                         (watch_id,)).fetchone()
            _sync_watch_row(db, refreshed_watch)
            synced_entity = db.execute('SELECT status FROM media_entities WHERE id=?',
                                       (entity_id,)).fetchone()
            refreshed_claim = db.execute('''SELECT is_canonical FROM media_claim_observations
                WHERE entity_id=? AND field_name='status' AND provider='tvmaze' ''',
                                         (entity_id,)).fetchone()

        self.assertEqual(selected['status'], 'cancelled')
        self.assertEqual(entity['status'], 'cancelled')
        self.assertEqual(json.loads(entity['metadata_json'])['status_source'], 'review')
        self.assertEqual([(row['provider'], row['is_canonical']) for row in chosen_claims],
                         [('tmdb', 1), ('tvmaze', 0)])
        self.assertEqual(json.loads(watch['metadata'])['catalog_status'], 'cancelled')
        self.assertEqual(json.loads(watch['metadata'])['catalog_status_source'], 'review')
        self.assertEqual(review_status, 'accepted')
        self.assertEqual(refreshed_entity['status'], 'cancelled')
        self.assertEqual(refreshed_claim['is_canonical'], 0)
        self.assertEqual(synced_entity['status'], 'cancelled')

    def test_same_platform_release_date_conflicts_enter_review(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            entity_id = upsert_media_entity(db, 'work', 'Example Game', media_type='game')
            upsert_media_release(
                db, entity_id, 'platform_launch', '2030-06-01', region='US',
                platform='Steam', status='upcoming', provider='IGDB',
                provider_release_id='igdb-release-1',
                source_url='https://www.igdb.com/games/example-game')
            upsert_media_release(
                db, entity_id, 'platform_launch', '2030-06-10', region='US',
                platform='steam', status='upcoming', provider='Steam',
                provider_release_id='steam-app:100',
                source_url='https://store.steampowered.com/app/100/')
            upsert_media_release(
                db, entity_id, 'platform_launch', '2030-06-10', region='US',
                platform='Steam', status='upcoming', provider='Steam',
                provider_release_id='steam-app:100',
                source_url='https://store.steampowered.com/app/100/')
            reviews = [item for item in list_graph_reviews(db)
                       if item['issue_type'] == 'conflicting_release_dates']

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['detail']['region'], 'US')
        self.assertEqual(reviews[0]['detail']['platform'].casefold(), 'steam')
        self.assertEqual({claim['provider'] for claim in reviews[0]['detail']['claims']},
                         {'IGDB', 'Steam'})
        self.assertEqual({claim['release_date'] for claim in reviews[0]['detail']['claims']},
                         {'2030-06-01', '2030-06-10'})

    def test_reviewed_game_release_date_becomes_canonical_and_schedules_watch(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            watch_id = add_watch(db, 'game', 'Example Game', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game', 'sources': {}})
            watch = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()
            entity_id = int(watch['media_entity_id'])
            upsert_media_release(
                db, entity_id, 'platform_launch', '2030-06-01', region='US',
                platform='Steam', status='upcoming', provider='IGDB',
                provider_release_id='igdb-release-1')
            steam = upsert_media_release(
                db, entity_id, 'platform_launch', '2030-06-10', region='US',
                platform='Steam', status='upcoming', provider='Steam',
                provider_release_id='steam-app:100')
            review = next(item for item in list_graph_reviews(db)
                          if item['issue_type'] == 'conflicting_release_dates')

            selected = set_reviewed_release_date(db, review['id'], steam['id'])
            release_rows = db.execute('''SELECT provider,release_date,is_canonical FROM media_releases
                WHERE entity_id=? AND release_type='platform_launch' ORDER BY provider''',
                                      (entity_id,)).fetchall()
            entity = db.execute('SELECT release_date,metadata_json FROM media_entities WHERE id=?',
                                (entity_id,)).fetchone()
            watched = db.execute('SELECT release_date,metadata,state,next_check_at FROM watched_items WHERE id=?',
                                 (watch_id,)).fetchone()
            review_row = db.execute('SELECT status FROM media_review_queue WHERE id=?',
                                    (review['id'],)).fetchone()

        self.assertEqual(selected['release_date'], '2030-06-10')
        self.assertEqual([(row['provider'], row['is_canonical']) for row in release_rows],
                         [('IGDB', 0), ('Steam', 1)])
        self.assertEqual(entity['release_date'], '2030-06-10')
        self.assertEqual(json.loads(entity['metadata_json'])[
            'canonical_release_date_observation']['provider'], 'Steam')
        self.assertEqual(watched['release_date'], '2030-06-10')
        self.assertEqual(json.loads(watched['metadata'])['release_date_source'], 'review')
        self.assertEqual(watched['state'], 'upcoming')
        self.assertEqual(watched['next_check_at'], '2030-06-10T00:00:00')
        self.assertEqual(review_row['status'], 'accepted')

    def test_movie_release_window_conflicts_can_be_reviewed_without_changing_premiere(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            watch_id = add_watch(db, 'video', 'Example Film', release_date='2028-04-01',
                                 metadata={'entity_type': 'work', 'video_subtype': 'movie',
                                           'sources': {}})
            entity_id = int(db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                       (watch_id,)).fetchone()['media_entity_id'])
            upsert_media_release(
                db, entity_id, 'video_digital', '2030-04-05', region='US', provider='TMDB',
                provider_release_id='tmdb-digital-1')
            selected_release = upsert_media_release(
                db, entity_id, 'video_digital', '2030-04-12', region='US', provider='Studio',
                provider_release_id='studio-digital-1')
            review = next(item for item in list_graph_reviews(db)
                          if item['issue_type'] == 'conflicting_release_dates')

            selection = set_reviewed_release_date(db, review['id'], selected_release['id'])
            entity = db.execute('SELECT release_date,metadata_json FROM media_entities WHERE id=?',
                                (entity_id,)).fetchone()
            watch = db.execute('SELECT release_date,metadata FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()
            chosen = db.execute('SELECT is_canonical FROM media_releases WHERE id=?',
                                (selected_release['id'],)).fetchone()

        self.assertEqual(selection['release_date'], '2030-04-12')
        self.assertEqual(entity['release_date'], '2028-04-01')
        self.assertIn('canonical_release_dates', json.loads(entity['metadata_json']))
        self.assertEqual(watch['release_date'], '2028-04-01')
        self.assertNotIn('release_date_source', json.loads(watch['metadata']))
        self.assertEqual(chosen['is_canonical'], 1)

    def test_one_provider_with_two_release_window_dates_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            entity_id = upsert_media_entity(db, 'work', 'Example Film', media_type='video')
            upsert_media_release(db, entity_id, 'video_theatrical', '2030-06-01', region='US',
                                 provider='TMDB', provider_release_id='theatrical-1')
            upsert_media_release(db, entity_id, 'video_theatrical', '2030-06-08', region='US',
                                 provider='TMDB', provider_release_id='theatrical-2')
            reviews = [item for item in list_graph_reviews(db)
                       if item['issue_type'] == 'ambiguous_release_dates']

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['detail']['release_type'], 'video_theatrical')
        self.assertEqual({claim['release_date'] for claim in reviews[0]['detail']['claims']},
                         {'2030-06-01', '2030-06-08'})

    def test_conflicting_episode_air_dates_can_be_reviewed_for_that_episode_only(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            parent_id = add_watch(db, 'video', 'Example Series', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series', 'sources': {}})
            parent_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (parent_id,)).fetchone()[0])
            position_key = {'provider': 'media-intake-episode',
                            'external_id': f'{parent_entity_id}:S01E02'}
            child_id = add_watch(db, 'video_episode', 'Example Series S01E02 - Next',
                                 release_date='2030-04-05', parent_id=parent_id,
                                 metadata={'entity_type': 'work', 'video_subtype': 'episode',
                                           'external_ids': [position_key], 'sources': {}})
            upsert_media_release(
                db, parent_entity_id, 'episode', '2030-04-05', provider='TMDB',
                provider_release_id='tmdb:episode:100',
                metadata={'season_number': 1, 'episode_number': 2, 'title': 'Next'})
            selected_release = upsert_media_release(
                db, parent_entity_id, 'episode', '2030-04-12', provider='TVMaze',
                provider_release_id='tvmaze:episode:200',
                metadata={'season_number': 1, 'episode_number': 2, 'title': 'Next'})
            upsert_media_release(
                db, parent_entity_id, 'episode', '2030-05-01', provider='TMDB',
                provider_release_id='tmdb:episode:101',
                metadata={'season_number': 1, 'episode_number': 3, 'title': 'Following'})
            review = next(item for item in list_graph_reviews(db)
                          if item['issue_type'] == 'conflicting_episode_dates')

            selection = set_reviewed_release_date(db, review['id'], selected_release['id'])
            child = db.execute('SELECT release_date,state,next_check_at,metadata FROM watched_items WHERE id=?',
                               (child_id,)).fetchone()
            child_entity = db.execute('''SELECT e.release_date FROM media_entities e
                JOIN watched_items w ON w.media_entity_id=e.id WHERE w.id=?''',
                                       (child_id,)).fetchone()
            canonical_ids = {row['provider_release_id'] for row in db.execute('''SELECT provider_release_id
                FROM media_releases WHERE entity_id=? AND release_type='episode' AND is_canonical=1''',
                                       (parent_entity_id,))}

        self.assertEqual(selection['release_date'], '2030-04-12')
        self.assertEqual(child['release_date'], '2030-04-12')
        self.assertEqual(child['state'], 'upcoming')
        self.assertEqual(child['next_check_at'], '2030-04-12T00:00:00')
        self.assertEqual(child_entity['release_date'], '2030-04-12')
        self.assertEqual(json.loads(child['metadata'])['release_date_source'], 'review')
        self.assertEqual(canonical_ids, {'tvmaze:episode:200'})

    def test_video_episode_parent_edges_use_episode_and_special_relationships(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            parent_id = add_watch(db, 'video', 'Example Series', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series', 'sources': {}})
            parent_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (parent_id,)).fetchone()[0])
            episode_id = add_watch(db, 'video_episode', 'Example Series S01E01',
                                   parent_id=parent_id,
                                   metadata={'entity_type': 'work', 'video_subtype': 'episode',
                                             'sources': {}})
            special_id = add_watch(db, 'video_episode', 'Example Series Special',
                                   parent_id=parent_id,
                                   metadata={'entity_type': 'work', 'video_subtype': 'special',
                                             'sources': {}})
            episode_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (episode_id,)).fetchone()[0])
            special_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (special_id,)).fetchone()[0])
            relationships = {(row['source_entity_id'], row['target_entity_id']): row['relationship_type']
                for row in db.execute('''SELECT source_entity_id,target_entity_id,relationship_type
                    FROM media_relationships WHERE source_entity_id IN (?,?)''',
                    (episode_entity_id, special_entity_id))}

        self.assertEqual(relationships[(episode_entity_id, parent_entity_id)], 'episode_of')
        self.assertEqual(relationships[(special_entity_id, parent_entity_id)], 'special_of')

    def test_series_member_catalog_disagreements_survive_watch_creation(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            parent_id = add_watch(db, 'series', 'Example Series', 'Example Author')
            member = {
                'title': 'Book One', 'author': 'Example Author', 'provider': 'Goodreads',
                'source_url': 'https://www.goodreads.com/book/show/1',
                'release_date': '2020-01-01', 'series': 'Example Series',
                'series_index': 1, 'catalog_metadata': {'catalog_id': '1'},
                'catalog_ids': {'Goodreads': ['1'], 'Open Library': ['OL1W']},
                'catalog_sources': {
                    'Goodreads': ['https://www.goodreads.com/book/show/1'],
                    'Open Library': ['https://openlibrary.org/works/OL1W'],
                },
                'metadata_variants': [
                    {'provider': 'Goodreads', 'title': 'Book One',
                     'source_url': 'https://www.goodreads.com/book/show/1',
                     'release_date': '2020-01-01', 'series_index': 1,
                     'catalog_metadata': {'catalog_id': '1'}},
                    {'provider': 'Open Library', 'title': 'Book One',
                     'source_url': 'https://openlibrary.org/works/OL1W',
                     'release_date': '2021-02-03', 'series_index': 1.5,
                     'catalog_metadata': {'catalog_id': 'OL1W'}},
                ],
                'metadata_conflicts': [{'field': 'release_date'}],
            }
            child_id, created = _add_catalog_book_watch(
                db, member, 'Example Author', {}, parent_id, 'Goodreads')
            openlibrary_member = {**member, 'provider': 'Open Library',
                                  'source_url': 'https://openlibrary.org/works/OL1W',
                                  'catalog_metadata': {'catalog_id': 'OL1W'},
                                  'metadata_variants': [member['metadata_variants'][1]]}
            merged_id, created_again = _add_catalog_book_watch(
                db, openlibrary_member, 'Example Author', {}, parent_id, 'Open Library')
            child = db.execute('SELECT metadata FROM watched_items WHERE id=?',
                               (child_id,)).fetchone()
            linked_openlibrary_id = db.execute('''SELECT 1 FROM media_external_ids x
                JOIN watched_items w ON w.media_entity_id=x.entity_id
                WHERE w.id=? AND x.provider='openlibrary' AND x.external_id='OL1W' ''',
                                                (child_id,)).fetchone()
            reviews = [item for item in list_graph_reviews(db)
                       if item['issue_type'] == 'conflicting_metadata']

        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(merged_id, child_id)
        child_metadata = json.loads(child['metadata'])
        self.assertEqual(child_metadata['catalog_provider'], 'Goodreads')
        self.assertIsNotNone(linked_openlibrary_id)
        self.assertEqual(len(child_metadata['catalog_metadata_variants']), 2)
        self.assertEqual(child_metadata['catalog_metadata_conflicts'],
                         [{'field': 'release_date'}])
        self.assertEqual({item['detail']['field'] for item in reviews},
                         {'release_date', 'series_index'})
        release_review = next(item for item in reviews
                              if item['detail']['field'] == 'release_date')
        self.assertEqual({claim['provider'] for claim in release_review['detail']['claims']},
                         {'goodreads', 'open library'})

    def test_reviewed_series_position_updates_watch_and_survives_catalog_refresh(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            parent_id = add_watch(db, 'series', 'Example Series', 'Example Author')
            member = {
                'title': 'Book One', 'author': 'Example Author', 'provider': 'Goodreads',
                'source_url': 'https://www.goodreads.com/book/show/1',
                'series': 'Example Series', 'series_index': 1,
                'catalog_metadata': {'catalog_id': '1'},
                'catalog_ids': {'Goodreads': ['1'], 'Open Library': ['OL1W']},
                'metadata_variants': [
                    {'provider': 'Goodreads', 'title': 'Book One', 'series_index': 1,
                     'source_url': 'https://www.goodreads.com/book/show/1',
                     'catalog_metadata': {'catalog_id': '1'}},
                    {'provider': 'Open Library', 'title': 'Book One', 'series_index': 1.5,
                     'source_url': 'https://openlibrary.org/works/OL1W',
                     'catalog_metadata': {'catalog_id': 'OL1W'}},
                ],
            }
            child_id, _created = _add_catalog_book_watch(
                db, member, 'Example Author', {}, parent_id, 'Goodreads')
            review = next(item for item in list_graph_reviews(db)
                          if item['issue_type'] == 'conflicting_metadata' and
                          item['detail'].get('field') == 'series_index')
            selected_claim = next(claim for claim in review['detail']['claims']
                                  if claim['provider'] == 'open library')

            result = set_reviewed_media_series_index(db, review['id'], selected_claim['id'])
            child = db.execute('SELECT media_entity_id,metadata FROM watched_items WHERE id=?',
                               (child_id,)).fetchone()
            entity = db.execute('SELECT metadata_json FROM media_entities WHERE id=?',
                                (child['media_entity_id'],)).fetchone()
            edge = db.execute('''SELECT metadata_json FROM media_relationships
                WHERE source_entity_id=? AND target_entity_id=? AND relationship_type='part_of' ''',
                              (child['media_entity_id'],
                               db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                          (parent_id,)).fetchone()[0])).fetchone()
            queue = db.execute('SELECT metadata FROM items WHERE url=?',
                               (f'watch-search://{child_id}',)).fetchone()

            refreshed = {**member, 'series_index': 1,
                         'metadata_variants': [member['metadata_variants'][0]]}
            _add_catalog_book_watch(db, refreshed, 'Example Author', {}, parent_id, 'Goodreads')
            refreshed_metadata = json.loads(db.execute(
                'SELECT metadata FROM watched_items WHERE id=?', (child_id,)).fetchone()[0])

        self.assertEqual(result['series_index'], 1.5)
        self.assertEqual(json.loads(child['metadata'])['series_index'], 1.5)
        self.assertEqual(json.loads(child['metadata'])['series_position_source'],
                         'Media Intake review')
        self.assertEqual(json.loads(entity['metadata_json'])['series_index'], 1.5)
        self.assertEqual(json.loads(edge['metadata_json'])['series_position'], '1.5')
        self.assertEqual(json.loads(queue['metadata'])['series_index'], 1.5)
        self.assertEqual(refreshed_metadata['series_index'], 1.5)
        self.assertEqual(refreshed_metadata['series_position_source'], 'Media Intake review')

    def test_reviewed_catalog_release_date_reschedules_book_watch_and_queue(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            parent_id = add_watch(db, 'series', 'Example Series', 'Example Author')
            member = {
                'title': 'Book One', 'author': 'Example Author', 'provider': 'Goodreads',
                'source_url': 'https://www.goodreads.com/book/show/1',
                'release_date': '2020-01-01', 'series': 'Example Series',
                'series_index': 1, 'catalog_metadata': {'catalog_id': '1'},
                'metadata_variants': [
                    {'provider': 'Goodreads', 'title': 'Book One',
                     'source_url': 'https://www.goodreads.com/book/show/1',
                     'release_date': '2020-01-01',
                     'catalog_metadata': {'catalog_id': '1'}},
                    {'provider': 'Open Library', 'title': 'Book One',
                     'source_url': 'https://openlibrary.org/works/OL1W',
                     'release_date': '2099-05-20',
                     'catalog_metadata': {'catalog_id': 'OL1W'}},
                ],
            }
            child_id, _created = _add_catalog_book_watch(
                db, member, 'Example Author', {}, parent_id, 'Goodreads')
            downloaded_id = add_watch(db, 'book', 'Saved copy', 'Example Author',
                                      release_date='2020-01-01',
                                      metadata={'release_date_source': 'Goodreads'})
            manual_id = add_watch(db, 'book', 'Manual date', 'Example Author',
                                  release_date='2022-02-02',
                                  metadata={'release_date_source': 'manual'})
            entity_id = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                   (child_id,)).fetchone()[0]
            db.execute('UPDATE watched_items SET media_entity_id=?,state=? WHERE id=?',
                       (entity_id, 'downloaded', downloaded_id))
            db.execute('UPDATE watched_items SET media_entity_id=? WHERE id=?',
                       (entity_id, manual_id))
            review = next(item for item in list_graph_reviews(db)
                          if item['issue_type'] == 'conflicting_metadata' and
                          item['detail'].get('field') == 'release_date')
            selected_claim = next(claim for claim in review['detail']['claims']
                                  if claim['provider'] == 'open library')

            result = set_reviewed_media_release_date(db, review['id'], selected_claim['id'])
            watch = db.execute('SELECT release_date,state,next_check_at,metadata FROM watched_items WHERE id=?',
                               (child_id,)).fetchone()
            queue = db.execute('SELECT metadata,state FROM items WHERE url=?',
                               (f'watch-search://{child_id}',)).fetchone()
            downloaded = db.execute('SELECT release_date,state FROM watched_items WHERE id=?',
                                     (downloaded_id,)).fetchone()
            manual = db.execute('SELECT release_date,state FROM watched_items WHERE id=?',
                                (manual_id,)).fetchone()

        self.assertEqual(result['release_date'], '2099-05-20')
        self.assertEqual(watch['release_date'], '2099-05-20')
        self.assertEqual(watch['state'], 'upcoming')
        self.assertEqual(watch['next_check_at'], '2099-05-20T00:00:00')
        self.assertEqual(json.loads(watch['metadata'])['release_date_source'], 'review')
        self.assertEqual(json.loads(queue['metadata'])['release_date'], '2099-05-20')
        self.assertEqual(queue['state'], 'upcoming')
        self.assertEqual(downloaded['release_date'], '2099-05-20')
        self.assertEqual(downloaded['state'], 'downloaded')
        self.assertEqual(manual['release_date'], '2022-02-02')

    def test_external_id_conflict_does_not_silently_merge_entities(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            first = upsert_media_entity(db, 'work', 'First', external_ids=[('wikidata', 'Q1')])
            second = upsert_media_entity(db, 'work', 'Second')
            with self.assertRaisesRegex(ValueError, 'already attached'):
                upsert_media_entity(db, 'work', 'Different title', entity_id=second,
                                    external_ids=[('wikidata', 'Q1')])
            self.assertNotEqual(first, second)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_entities').fetchone()[0], 2)

    def test_book_duplicate_review_uses_shared_work_title_normalization(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            upsert_media_entity(db, 'work', 'The Hobbit', media_type='book',
                                metadata={'author': 'J. R. R. Tolkien'})
            upsert_media_entity(db, 'work', 'Hobbit, The (Paperback)', media_type='book',
                                metadata={'author': 'J. R. R. Tolkien'})
            reviews = list_graph_reviews(db)
            upsert_media_entity(db, 'work', 'The Hobbit Revised Edition',
                                media_type='book', metadata={'author': 'J. R. R. Tolkien'})
            reviews_after_distinct_edition = list_graph_reviews(db)

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['issue_type'], 'suspected_duplicate')
        self.assertEqual(len(reviews_after_distinct_edition), 1)

    def test_author_collection_links_catalog_books_with_created_by_edges(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            author_id = add_watch(
                db, 'author', 'Example Author', source_url=
                'https://www.goodreads.com/author/show/42-example-author',
                metadata={'catalog_provider': 'Goodreads', 'sources': {}})
            author = dict(db.execute(
                'SELECT * FROM watched_items WHERE id=?', (author_id,)).fetchone())
            saved = _sync_series_members_graph(db, author, [{
                'title': 'Example Novel', 'author': 'Example Author',
                'provider': 'Goodreads',
                'source_url': 'https://www.goodreads.com/book/show/100-example-novel',
            }])
            edge = dict(db.execute('''SELECT r.relationship_type,r.source_entity_id,
                    r.target_entity_id,e.provider,e.provider_entity_id,e.raw_relationship_type,
                    e.payload_json
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id''').fetchone())
            work_id = db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider='goodreads' AND external_id='100' ''').fetchone()['entity_id']
            watch_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual(saved, 1)
        self.assertEqual(edge['relationship_type'], 'created_by')
        self.assertEqual(edge['source_entity_id'], work_id)
        self.assertEqual(edge['target_entity_id'], author['media_entity_id'])
        self.assertEqual((edge['provider'], edge['provider_entity_id'],
                          edge['raw_relationship_type']),
                         ('goodreads', '100', 'author bibliography'))
        self.assertIn('Example Author', edge['payload_json'])
        self.assertEqual(watch_count, 1)

    def test_graph_snapshot_reports_new_nodes_and_edges_once(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            root = upsert_media_entity(db, 'series', 'Example Series')
            baseline = capture_graph_snapshot(db, root)
            self.assertTrue(baseline['baseline'])
            work = upsert_media_entity(db, 'work', 'Book One')
            add_media_relationship(db, work, root, 'part_of', confidence=1,
                                   status='accepted', provider='Fixture',
                                   raw_relationship_type='member of')
            changed = capture_graph_snapshot(db, root)
            self.assertEqual({item['change_type'] for item in changed['changes']},
                             {'entity_added', 'relationship_added', 'claim_added'})
            unchanged = capture_graph_snapshot(db, root)
            self.assertTrue(unchanged['unchanged'])
            self.assertEqual(unchanged['changes'], [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_graph_snapshots').fetchone()[0], 2)
            db.execute('DELETE FROM media_relationships')
            reverted = capture_graph_snapshot(db, root)
            self.assertEqual({item['change_type'] for item in reverted['changes']},
                             {'entity_removed', 'relationship_removed'})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_graph_snapshots').fetchone()[0], 3)

    def test_graph_snapshot_records_release_date_changes_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            entity_id = upsert_media_entity(
                db, 'work', 'Future Film', media_type='video',
                release_date='2030-05-01', status='upcoming', provider='TMDB')
            upsert_media_release(db, entity_id, 'premiere', '2030-05-01',
                                 status='upcoming', provider='TMDB',
                                 provider_release_id='movie:1')
            self.assertTrue(capture_graph_snapshot(db, entity_id)['baseline'])
            upsert_media_entity(db, 'work', 'Future Film', media_type='video',
                                release_date='2030-10-12', status='upcoming',
                                entity_id=entity_id, provider='TMDB')
            upsert_media_release(db, entity_id, 'premiere', '2030-10-12',
                                 status='upcoming', provider='TMDB',
                                 provider_release_id='movie:1')
            changed = capture_graph_snapshot(db, entity_id)
            unchanged = capture_graph_snapshot(db, entity_id)

        self.assertIn('release_updated', {row['change_type'] for row in changed['changes']})
        self.assertTrue(unchanged['unchanged'])

    def test_review_queue_deduplicates_open_issues_and_can_resolve(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            first = queue_graph_review(db, 'ambiguous_entity', 'Check the match',
                                       {'candidate': 'Q1'}, dedupe_key='candidate:Q1')
            again = queue_graph_review(db, 'ambiguous_entity', 'Check the match',
                                       {'candidate': 'Q1'}, dedupe_key='candidate:Q1')
            self.assertEqual(first, again)
            self.assertEqual(len(list_graph_reviews(db)), 1)
            resolve_graph_review(db, first, 'rejected')
            reopened = queue_graph_review(db, 'ambiguous_entity', 'Check the match',
                                          {'candidate': 'Q1'}, dedupe_key='candidate:Q1')
            self.assertNotEqual(first, reopened)
            self.assertEqual([item['status'] for item in list_graph_reviews(db)], ['open'])

    def test_rejecting_relationship_review_marks_edge_ignored(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            source = upsert_media_entity(db, 'work', 'Source work', media_type='book')
            target = upsert_media_entity(db, 'work', 'Possible sequel', media_type='book')
            relationship_id = add_media_relationship(
                db, source, target, 'sequel_to', confidence=0.5, inferred=True,
                status='candidate', provider='Fixture', is_explicit=False)
            review = next(item for item in list_graph_reviews(db)
                          if item.get('relationship_id') == relationship_id)
            resolve_graph_review(db, review['id'], 'rejected')
            edge = db.execute('SELECT status FROM media_relationships WHERE id=?',
                              (relationship_id,)).fetchone()
            resolved = db.execute('SELECT status FROM media_review_queue WHERE id=?',
                                  (review['id'],)).fetchone()
        self.assertEqual(edge['status'], 'ignored')
        self.assertEqual(resolved['status'], 'rejected')

    def test_media_type_watch_policy_overrides_global_policy(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            set_watch_policy(db, 'global', '', {'episodes': 'ignore'})
            set_watch_policy(db, 'media_type', 'video', {'episodes': 'auto_add'})
            policy = effective_watch_policy(db, 'video')
        self.assertEqual(policy['episodes'], 'auto_add')

    def test_wikidata_search_normalizes_candidates(self):
        payload = {'search': [
            {'id': 'Q123', 'label': 'Example Series', 'description': 'book series',
             'concepturi': 'http://www.wikidata.org/entity/Q123'},
            {'id': 'bad', 'label': 'Invalid'},
        ]}
        with patch('media_intake.wikidata._get_json', return_value=payload):
            results = search_wikidata('Example Series')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['qid'], 'Q123')
        self.assertEqual(results[0]['provider'], 'Wikidata')

    def test_wikidata_partial_dates_are_preserved_without_inventing_release_day(self):
        entity = {
            'id': 'Q55', 'labels': {'en': {'value': 'A Partial Date Work'}},
            'claims': {'P577': [{'id': 'Q55$DATE', 'mainsnak': {'datavalue': {'value': {
                'time': '+2030-00-00T00:00:00Z', 'precision': 9}}}}]},
        }
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            result = import_wikidata_entity(db, 'Q55', entity_payload=entity, related_payloads={})
            self.assertIsNone(db.execute('SELECT release_date FROM media_entities WHERE id=?',
                                         (result['root_entity_id'],)).fetchone()['release_date'])
            release = db.execute('SELECT release_date,metadata_json FROM media_releases').fetchone()
            self.assertIsNone(release['release_date'])
            self.assertEqual(json.loads(release['metadata_json'])['precision'], 9)

    def test_wikidata_import_keeps_relationships_and_release_evidence(self):
        type_book = {'id': 'Q100', 'labels': {'en': {'value': 'book'}}}
        type_series = {'id': 'Q101', 'labels': {'en': {'value': 'book series'}}}
        person_type = {'id': 'Q102', 'labels': {'en': {'value': 'human'}}}
        root = {
            'id': 'Q1', 'labels': {'en': {'value': 'Book One'}},
            'descriptions': {'en': {'value': 'first novel in a series'}},
            'aliases': {'en': [{'value': 'Book One alternate title'}]},
            'claims': {
                'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}],
                'P179': [{'id': 'Q1$SERIES', 'mainsnak': {'datavalue': {'value': {'id': 'Q10'}}},
                         'qualifiers': {'P1545': [{'datavalue': {'value': '1'}}]}}],
                'P156': [{'id': 'Q1$NEXT', 'mainsnak': {'datavalue': {'value': {'id': 'Q2'}}}}],
                'P50': [{'id': 'Q1$AUTHOR', 'mainsnak': {'datavalue': {'value': {'id': 'Q3'}}}}],
                'P577': [{'id': 'Q1$DATE', 'mainsnak': {'datavalue': {'value': {
                    'time': '+2024-03-15T00:00:00Z'}}}}],
            },
        }
        related = {
            'Q100': type_book,
            'Q101': type_series,
            'Q102': person_type,
            'Q10': {'id': 'Q10', 'labels': {'en': {'value': 'Example Series'}},
                    'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q101'}}}}]}},
            'Q2': {'id': 'Q2', 'labels': {'en': {'value': 'Book Two'}},
                   'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}]}},
            'Q3': {'id': 'Q3', 'labels': {'en': {'value': 'Example Author'}},
                   'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q102'}}}}]}},
        }
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            watch_id = add_watch(db, 'book', 'Book One', source_url='https://example.org/book-one')
            watch = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?', (watch_id,)).fetchone()
            result = import_wikidata_entity(db, 'Q1', watch_entity_id=watch['media_entity_id'],
                                            entity_payload=root, related_payloads=related)
            relations = {item['relationship_type']: item for item in result['relationships']}
            self.assertEqual(set(relations), {'parent_series', 'followed_by', 'created_by'})
            self.assertEqual(relations['parent_series']['series_position'], '1')
            self.assertEqual(relations['followed_by']['target_title'], 'Book Two')
            self.assertEqual(result['related_entities'], 3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0], 1)
            self.assertEqual(db.execute('''SELECT COUNT(*) FROM media_relationship_evidence
                WHERE provider='wikidata' ''').fetchone()[0], 3)
            release = db.execute('''SELECT release_date,provider_release_id FROM media_releases
                WHERE entity_id=?''', (result['root_entity_id'],)).fetchone()
            self.assertEqual(release['release_date'], '2024-03-15')
            self.assertEqual(release['provider_release_id'], 'Q1$DATE')
            self.assertEqual(db.execute('''SELECT COUNT(*) FROM media_entity_aliases
                WHERE alias='Book One alternate title' ''').fetchone()[0], 1)

            import_wikidata_entity(db, 'Q1', watch_entity_id=watch['media_entity_id'],
                                   entity_payload=root, related_payloads=related)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_releases').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_relationships').fetchone()[0], 3)
            snapshot = capture_graph_snapshot(db, result['root_entity_id'])
            self.assertTrue(snapshot['baseline'])
            import_wikidata_entity(db, 'Q1', watch_entity_id=watch['media_entity_id'],
                                   entity_payload=root, related_payloads=related)
            self.assertTrue(capture_graph_snapshot(db, result['root_entity_id'])['unchanged'])

    def test_wikidata_ignores_deprecated_series_relationships_and_dates(self):
        root = {
            'id': 'Q20', 'labels': {'en': {'value': 'Current Work'}},
            'claims': {
                'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}],
                'P179': [
                    {'rank': 'deprecated', 'mainsnak': {'datavalue': {'value': {'id': 'Q10'}}}},
                    {'rank': 'normal', 'mainsnak': {'datavalue': {'value': {'id': 'Q11'}}}},
                ],
                'P156': [
                    {'rank': 'deprecated', 'mainsnak': {'datavalue': {'value': {'id': 'Q21'}}}},
                    {'rank': 'normal', 'mainsnak': {'datavalue': {'value': {'id': 'Q22'}}}},
                ],
                'P577': [
                    {'rank': 'deprecated', 'id': 'Q20$OLD_DATE',
                     'mainsnak': {'datavalue': {'value': {'time': '+2019-01-01T00:00:00Z'}}}},
                    {'rank': 'normal', 'id': 'Q20$DATE',
                     'mainsnak': {'datavalue': {'value': {'time': '+2024-03-15T00:00:00Z'}}}},
                ],
            },
        }
        related = {
            'Q10': {'id': 'Q10', 'labels': {'en': {'value': 'Old Series'}}},
            'Q11': {'id': 'Q11', 'labels': {'en': {'value': 'Current Series'}}},
            'Q21': {'id': 'Q21', 'labels': {'en': {'value': 'Old Sequel'}}},
            'Q22': {'id': 'Q22', 'labels': {'en': {'value': 'Current Sequel'}}},
        }
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            result = import_wikidata_entity(db, 'Q20', entity_payload=root,
                                            related_payloads=related)
            relation_types = {item['relationship_type']: item
                              for item in result['relationships']}
            release = db.execute('SELECT release_date,provider_release_id FROM media_releases').fetchone()
            evidence = db.execute('''SELECT payload_json FROM media_relationship_evidence
                WHERE provider='wikidata' ORDER BY id LIMIT 1''').fetchone()

        self.assertEqual(set(relation_types), {'parent_series', 'followed_by'})
        self.assertEqual({item['target_qid'] for item in relation_types.values()}, {'Q11', 'Q22'})
        self.assertEqual(release['release_date'], '2024-03-15')
        self.assertEqual(release['provider_release_id'], 'Q20$DATE')
        self.assertEqual(json.loads(evidence['payload_json'])['claim_rank'], 'normal')

    def test_wikidata_series_sparql_excludes_deprecated_memberships(self):
        payload = {'results': {'bindings': [{
            'work': {'value': 'http://www.wikidata.org/entity/Q31'},
            'workLabel': {'value': 'Current Member'},
            'seriesNumber': {'value': '1'},
            'workTypeLabel': {'value': 'novel'},
        }, {
            'work': {'value': 'http://www.wikidata.org/entity/Q32'},
            'workLabel': {'value': 'Film Adaptation'},
            'seriesNumber': {'value': '2'},
            'workTypeLabel': {'value': 'film'},
        }, {
            'work': {'value': 'http://www.wikidata.org/entity/Q33'},
            'workLabel': {'value': 'Unclassified Member'},
            'seriesNumber': {'value': '3'},
        }, {
            'work': {'value': 'http://www.wikidata.org/entity/Q34'},
            'workLabel': {'value': 'Light Novel Member'},
            'seriesNumber': {'value': '2'},
            'workTypeLabel': {'value': 'light novel'},
        }]}}
        requested = {}

        def fake_get(url, max_bytes):
            requested['query'] = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query)['query'][0]
            return json.dumps(payload).encode('utf-8'), url

        with patch('media_intake.watched.http_get', side_effect=fake_get):
            members = _wikidata_series_members('Q30')

        self.assertIn('FILTER(?rank != wikibase:DeprecatedRank)', requested['query'])
        self.assertIn('?workTypeLabel', requested['query'])
        self.assertEqual([member['title'] for member in members],
                         ['Current Member', 'Light Novel Member', 'Unclassified Member'])
        self.assertFalse(members[0].get('needs_review'))
        self.assertEqual(members[0]['catalog_metadata']['work_type_labels'], ['novel'])
        self.assertFalse(members[1].get('needs_review'))
        self.assertTrue(members[2].get('needs_review'))
        self.assertIn('does not identify it as a book', members[2]['review_reason'])

    def test_book_metadata_search_excludes_non_book_wikidata_series(self):
        candidates = [
            {'qid': 'Q40', 'title': 'Example Book Series',
             'description': 'Fantasy novel series by an author.'},
            {'qid': 'Q41', 'title': 'Example Television Series',
             'description': 'American television series.'},
            {'qid': 'Q42', 'title': 'Example Book Cycle',
             'description': 'Series of fantasy novels and novellas.'},
            {'qid': 'Q43', 'title': 'Example Comic Book Series',
             'description': 'Comic book series.'},
        ]
        with patch('media_intake.watched.search_wikidata', return_value=candidates), \
                patch('media_intake.watched._wikidata_series_members', return_value=[]):
            results = _search_wikidata_series('Example')

        self.assertEqual({item['qid'] for item in results}, {'Q40', 'Q42'})

    def test_wikidata_derivative_work_is_preserved_without_default_auto_add(self):
        film_type = {'id': 'Q100', 'labels': {'en': {'value': 'film'}}}
        root = {
            'id': 'Q1', 'labels': {'en': {'value': 'Original Film'}},
            'claims': {
                'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}],
                'P4969': [{'id': 'Q1$DERIVATIVE',
                           'mainsnak': {'datavalue': {'value': {'id': 'Q2'}}}}],
            },
        }
        derivative = {
            'id': 'Q2', 'labels': {'en': {'value': 'Follow-up Animation'}},
            'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}]},
        }
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            watch_id = add_watch(db, 'video', 'Original Film', metadata={
                'entity_type': 'work', 'video_subtype': 'movie', 'sources': {},
                'external_ids': [{'provider': 'wikidata', 'external_id': 'Q1'}],
            })
            watch = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()
            result = import_wikidata_entity(
                db, 'Q1', watch_entity_id=watch['media_entity_id'],
                entity_payload=root, related_payloads={'Q100': film_type, 'Q2': derivative})
            relation = db.execute('''SELECT r.relationship_type,e.provider,e.raw_relationship_type,
                    e.payload_json FROM media_relationships r
                JOIN media_relationship_evidence e ON e.relationship_id=r.id
                WHERE r.source_entity_id=?''', (watch['media_entity_id'],)).fetchone()
            _apply_wikidata_watch_policy(
                db, dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()),
                result)
            default_watch_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            set_watch_policy(db, 'media_type', 'video', {'derivative_work': 'auto_add'})
            _apply_wikidata_watch_policy(
                db, dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone()),
                result)
            configured_watch = db.execute('''SELECT kind,title FROM watched_items
                WHERE id<>?''', (watch_id,)).fetchone()

        self.assertEqual(relation['relationship_type'], 'derivative_work')
        self.assertEqual(relation['provider'], 'wikidata')
        self.assertEqual(relation['raw_relationship_type'], 'P4969')
        self.assertIn('derivative work', relation['payload_json'])
        self.assertEqual(default_watch_count, 1)
        self.assertEqual(tuple(configured_watch), ('video', 'Follow-up Animation'))

    def test_wikidata_explicit_derivative_descriptions_normalize_variant_edges(self):
        film_type = {'id': 'Q100', 'labels': {'en': {'value': 'film'}}}
        variant_rows = {
            'Q2': ('The Film Remake', '2024 film remake of the original'),
            'Q3': ('The Film Reboot', 'television reboot of the original film'),
            'Q4': ('The Director Cut', "director's cut of the original film"),
            'Q5': ('The Follow-up', 'animated follow-up film'),
            'Q6': ('The Side Story', 'web series spin-off of the original film'),
            'Q7': ('The Series Revival', 'television revival of the original series'),
            'Q8': ('The Alternate Version', 'alternate version of the original film'),
        }
        root = {
            'id': 'Q1', 'labels': {'en': {'value': 'Original Film'}},
            'claims': {
                'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}],
                'P4969': [{'id': f'Q1${target}', 'mainsnak': {
                    'datavalue': {'value': {'id': target}}}}
                    for target in variant_rows],
            },
        }
        related = {'Q100': film_type}
        for qid, (title, description) in variant_rows.items():
            related[qid] = {
                'id': qid, 'labels': {'en': {'value': title}},
                'descriptions': {'en': {'value': description}},
                'claims': {'P31': [{'mainsnak': {
                    'datavalue': {'value': {'id': 'Q100'}}}}]},
            }

        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Original Film', metadata={
                'entity_type': 'work', 'video_subtype': 'movie', 'sources': {},
                'external_ids': [{'provider': 'wikidata', 'external_id': 'Q1'}],
            })
            root_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (watch_id,)
            ).fetchone()[0])
            result = import_wikidata_entity(
                db, 'Q1', watch_entity_id=root_id, entity_payload=root,
                related_payloads=related)
            normalized = {item['target_qid']: item['relationship_type']
                          for item in result['relationships']}
            edges = {}
            for item in result['relationships']:
                edges[item['target_qid']] = dict(db.execute(
                    'SELECT source_entity_id,target_entity_id FROM media_relationships WHERE id=?',
                    (item['id'],)).fetchone())
            variant_ids = {row['external_id']: int(row['entity_id']) for row in db.execute(
                "SELECT entity_id,external_id FROM media_external_ids WHERE provider='wikidata'")}
            _apply_wikidata_watch_policy(
                db, dict(db.execute('SELECT * FROM watched_items WHERE id=?',
                                    (watch_id,)).fetchone()), result)
            watch_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]
            set_watch_policy(db, 'media_type', 'video', {'spin_off_of': 'auto_add'})
            _apply_wikidata_watch_policy(
                db, dict(db.execute('SELECT * FROM watched_items WHERE id=?',
                                    (watch_id,)).fetchone()), result)
            expanded_watch = db.execute('''SELECT kind,title FROM watched_items
                WHERE id<>?''', (watch_id,)).fetchone()

        self.assertEqual(normalized, {
            'Q2': 'remake_of', 'Q3': 'reboot_of',
            'Q4': 'alternate_cut_of', 'Q5': 'derivative_work',
            'Q6': 'spin_off_of', 'Q7': 'revival_of',
            'Q8': 'alternate_version_of',
        })
        for qid in ('Q2', 'Q3', 'Q4', 'Q6', 'Q7', 'Q8'):
            self.assertEqual(edges[qid]['source_entity_id'], variant_ids[qid])
            self.assertEqual(edges[qid]['target_entity_id'], root_id)
        self.assertEqual(edges['Q5']['source_entity_id'], root_id)
        self.assertEqual(edges['Q5']['target_entity_id'], variant_ids['Q5'])
        self.assertEqual(watch_count, 1)
        self.assertEqual(tuple(expanded_watch), ('video', 'The Side Story'))

    def test_wikidata_explicit_video_subtypes_are_normalized(self):
        cases = (
            ('Q201', 'short film', 'short_film', 'work'),
            ('Q202', 'original animation DVD', 'anime_oad', 'work'),
            ('Q203', 'independent animated film', 'independent_animation', 'work'),
            ('Q204', 'television miniseries', 'miniseries', 'series'),
            ('Q205', 'web series', 'web_series', 'series'),
            ('Q206', 'television series', 'tv_series', 'series'),
            ('Q207', 'Japanese animated television series', 'anime', 'series'),
        )
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            for qid, type_label, expected_subtype, expected_entity_type in cases:
                payload = {
                    'id': qid, 'labels': {'en': {'value': f'Example {expected_subtype}'}},
                    'claims': {'P31': [{'mainsnak': {
                        'datavalue': {'value': {'id': 'Q100'}}}}]},
                }
                result = import_wikidata_entity(
                    db, qid, entity_payload=payload,
                    related_payloads={'Q100': {'id': 'Q100',
                                               'labels': {'en': {'value': type_label}}}})
                entity = db.execute('SELECT media_type,metadata_json FROM media_entities WHERE id=?',
                                    (result['root_entity_id'],)).fetchone()
                self.assertEqual(entity['media_type'], 'video')
                self.assertEqual(result['entity_type'], expected_entity_type)
                self.assertEqual(result['video_subtype'], expected_subtype)
                self.assertEqual(json.loads(entity['metadata_json'])['video_subtype'],
                                 expected_subtype)

    def test_wikidata_series_members_stay_in_graph_without_becoming_watches(self):
        series_type = {'id': 'Q101', 'labels': {'en': {'value': 'book series'}}}
        book_type = {'id': 'Q100', 'labels': {'en': {'value': 'book'}}}
        edition_type = {'id': 'Q103', 'labels': {'en': {'value': 'book edition'}}}
        series = {
            'id': 'Q10', 'labels': {'en': {'value': 'Example Series'}},
            'descriptions': {'en': {'value': 'novel series'}},
            'claims': {
                'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q101'}}}}],
                'P527': [
                    {'id': 'Q10$BOOK', 'mainsnak': {'datavalue': {'value': {'id': 'Q11'}}}},
                    {'id': 'Q10$EDITION', 'mainsnak': {'datavalue': {'value': {'id': 'Q12'}}}},
                ],
            },
        }
        related = {
            'Q101': series_type,
            'Q100': book_type,
            'Q103': edition_type,
            'Q11': {'id': 'Q11', 'labels': {'en': {'value': 'Book One'}},
                    'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}],
                               'P577': [{'id': 'Q11$DATE', 'mainsnak': {'datavalue': {'value': {
                                   'time': '+2030-01-02T00:00:00Z'}}}}]}},
            'Q12': {'id': 'Q12', 'labels': {'en': {'value': 'Book One Deluxe Edition'}},
                    'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q103'}}}}]}},
        }
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            series_watch = add_watch(db, 'series', 'Example Series', author='Example Author',
                                     source_url='https://www.goodreads.com/series/100',
                                     metadata={'sources': {'openlibrary': True}})
            watch = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                               (series_watch,)).fetchone()
            result = import_wikidata_entity(db, 'Q10', watch_entity_id=watch['media_entity_id'],
                                            entity_payload=series, related_payloads=related)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='series'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='book'").fetchone()[0], 0)
            relation = db.execute('''SELECT r.relationship_type,e.release_date
                FROM media_relationships r JOIN media_entities e
                  ON e.id=r.target_entity_id
                WHERE r.source_entity_id=?''', (watch['media_entity_id'],)).fetchone()
            self.assertEqual(relation['relationship_type'], 'contains')
            self.assertEqual(relation['release_date'], '2030-01-02')
            self.assertEqual(db.execute('''SELECT entity_id FROM media_external_ids
                WHERE provider='wikidata' AND external_id='Q11' ''').fetchone()['entity_id'],
                db.execute('''SELECT target_entity_id FROM media_relationships
                    WHERE source_entity_id=?''', (watch['media_entity_id'],)).fetchone()['target_entity_id'])

    def test_wikidata_series_member_does_not_duplicate_existing_book_watch(self):
        book_type = {'id': 'Q100', 'labels': {'en': {'value': 'book'}}}
        series = {'id': 'Q10', 'labels': {'en': {'value': 'Example Series'}},
                  'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q101'}}}}],
                             'P527': [{'id': 'Q10$BOOK',
                                      'mainsnak': {'datavalue': {'value': {'id': 'Q11'}}}}]}}
        related = {'Q11': {'id': 'Q11', 'labels': {'en': {'value': 'Known Book'}},
                           'claims': {'P31': [{'mainsnak': {'datavalue': {'value': {'id': 'Q100'}}}}]}},
                   'Q100': book_type,
                   'Q101': {'id': 'Q101', 'labels': {'en': {'value': 'book series'}}}}
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            series_watch = add_watch(db, 'series', 'Example Series')
            add_watch(db, 'book', 'Known Book', author='Unknown',
                      source_url='https://example.org/known-book')
            watch = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                               (series_watch,)).fetchone()
            result = import_wikidata_entity(db, 'Q10', watch_entity_id=watch['media_entity_id'],
                                            entity_payload=series, related_payloads=related)
            self.assertEqual(len(result['relationships']), 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='book'").fetchone()[0], 1)
            reviews = list_graph_reviews(db)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['issue_type'], 'suspected_duplicate')
        self.assertEqual(reviews[0]['title'], 'Review possible duplicate: Known Book')

    def test_watching_a_wikidata_book_expands_its_parent_series_once(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            root_id = add_watch(db, 'book', 'Book One', 'Example Author',
                                'https://www.wikidata.org/entity/Q100', metadata={
                                    'entity_type': 'work', 'catalog_provider': 'Wikidata',
                                    'catalog_metadata': {'catalog_id': 'Q100'},
                                    'external_ids': [{'provider': 'wikidata',
                                                      'external_id': 'Q100'}],
                                })
            root = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (root_id,)).fetchone())
            imported = {'relationships': [{
                'relationship_type': 'parent_series', 'target_type': 'series',
                'target_media_type': 'book', 'target_qid': 'Q200',
                'target_title': 'Example Series',
                'target_source_url': 'https://www.wikidata.org/entity/Q200',
                'confidence': 0.9,
            }]}
            members = [
                {'title': 'Book One', 'author': 'Example Author',
                 'source_url': 'https://www.wikidata.org/entity/Q100',
                 'series_index': 1,
                 'catalog_metadata': {'catalog_id': 'Q100'}},
                {'title': 'Book Two', 'author': 'Example Author',
                 'source_url': 'https://www.wikidata.org/entity/Q101',
                 'series_index': 2,
                 'catalog_metadata': {'catalog_id': 'Q101'}},
            ]
            def imported_series(qid, watch_entity_id=None):
                return {'root_entity_id': watch_entity_id, 'relationships': []}
            with patch('media_intake.watched._wikidata_series_members', return_value=members), patch(
                    'media_intake.watched.import_wikidata_entity', side_effect=imported_series):
                _apply_wikidata_watch_policy(db, root, imported)

            rows = [dict(row) for row in db.execute(
                'SELECT id,kind,title,parent_id FROM watched_items ORDER BY id')]
            q100 = db.execute('''SELECT COUNT(*) FROM watched_items AS w
                JOIN media_external_ids AS x ON x.entity_id=w.media_entity_id
                WHERE x.provider='wikidata' AND x.external_id='Q100' ''').fetchone()[0]

        self.assertEqual(q100, 1)
        self.assertEqual([(row['kind'], row['title']) for row in rows], [
            ('book', 'Book One'), ('series', 'Example Series'), ('book', 'Book Two')])
        self.assertEqual(rows[0]['parent_id'], rows[1]['id'])
        self.assertEqual(rows[2]['parent_id'], rows[1]['id'])

    def test_wikidata_direct_sequel_is_auto_added_once_by_default(self):
        future_date = (dt.date.today() + dt.timedelta(days=60)).isoformat()
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            root_id = add_watch(db, 'book', 'Book One', 'Example Author',
                                'https://www.wikidata.org/entity/Q300', metadata={
                                    'entity_type': 'work', 'catalog_provider': 'Wikidata',
                                    'catalog_metadata': {'catalog_id': 'Q300'},
                                    'external_ids': [{'provider': 'wikidata',
                                                      'external_id': 'Q300'}],
                                })
            root = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (root_id,)).fetchone())
            imported = {'relationships': [{
                'relationship_type': 'followed_by', 'target_type': 'work',
                'target_media_type': 'book', 'target_qid': 'Q301',
                'target_title': 'Book Two',
                'target_source_url': 'https://www.wikidata.org/entity/Q301',
                'target_release_date': future_date, 'confidence': 0.9,
            }]}
            _apply_wikidata_watch_policy(db, root, imported)
            _apply_wikidata_watch_policy(db, root, imported)
            rows = [dict(row) for row in db.execute(
                'SELECT kind,title,state,release_date FROM watched_items ORDER BY id')]

        self.assertEqual(rows, [
            {'kind': 'book', 'title': 'Book One',
             'state': 'watching', 'release_date': None},
            {'kind': 'book', 'title': 'Book Two',
             'state': 'upcoming', 'release_date': future_date},
        ])

    def test_wikidata_future_sequel_uses_future_installment_policy(self):
        future_date = (dt.date.today() + dt.timedelta(days=45)).isoformat()
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            root_id = add_watch(db, 'book', 'Book One', 'Example Author',
                                'https://www.wikidata.org/entity/Q310', metadata={
                                    'entity_type': 'work', 'catalog_provider': 'Wikidata',
                                    'external_ids': [{'provider': 'wikidata',
                                                      'external_id': 'Q310'}],
                                })
            root = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (root_id,)).fetchone())
            set_watch_policy(db, 'media_type', 'book', {
                'direct_sequel_works': 'ignore',
                'future_installments': 'auto_add',
            })
            imported = {'relationships': [
                {'relationship_type': 'followed_by', 'target_type': 'work',
                 'target_media_type': 'book', 'target_qid': 'Q311',
                 'target_title': 'Upcoming Sequel', 'target_release_date': future_date,
                 'confidence': 0.9},
                {'relationship_type': 'followed_by', 'target_type': 'work',
                 'target_media_type': 'book', 'target_qid': 'Q312',
                 'target_title': 'Released Sequel', 'target_release_date': '2020-02-03',
                 'confidence': 0.9},
            ]}
            _apply_wikidata_watch_policy(db, root, imported)
            rows = [dict(row) for row in db.execute('''SELECT title,state,release_date
                FROM watched_items WHERE id!=? ORDER BY title''', (root_id,))]

        self.assertEqual(rows, [{'title': 'Upcoming Sequel', 'state': 'upcoming',
                                 'release_date': future_date}])

    def test_wikidata_direct_sequel_series_obeys_policy(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            root_id = add_watch(db, 'book', 'First Series',
                                'Example Author', 'https://www.wikidata.org/entity/Q400',
                                metadata={'entity_type': 'work', 'catalog_provider': 'Wikidata',
                                          'external_ids': [{'provider': 'wikidata',
                                                            'external_id': 'Q400'}]})
            root = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (root_id,)).fetchone())
            sequel = {'relationships': [{
                'relationship_type': 'followed_by', 'target_type': 'series',
                'target_media_type': 'book', 'target_qid': 'Q401',
                'target_title': 'Second Series',
                'target_source_url': 'https://www.wikidata.org/entity/Q401',
                'confidence': 0.9,
            }]}
            with patch('media_intake.watched._process_wikidata_series_collection') as expand:
                _apply_wikidata_watch_policy(db, root, sequel)
                _apply_wikidata_watch_policy(db, root, sequel)
            sequel_count = db.execute(
                "SELECT COUNT(*) FROM watched_items WHERE kind='series'").fetchone()[0]
            set_watch_policy(db, 'media_type', 'book',
                             {'direct_sequel_series': 'discover'})
            db.commit()
            another = add_watch(db, 'book', 'Standalone Work', 'Example Author',
                                'https://www.wikidata.org/entity/Q402', metadata={
                                    'entity_type': 'work', 'catalog_provider': 'Wikidata',
                                    'external_ids': [{'provider': 'wikidata',
                                                      'external_id': 'Q402'}],
                                })
            another_watch = dict(db.execute(
                'SELECT * FROM watched_items WHERE id=?', (another,)).fetchone())
            with patch('media_intake.watched._process_wikidata_series_collection') as expand:
                _apply_wikidata_watch_policy(db, another_watch, sequel)
            final_count = db.execute(
                "SELECT COUNT(*) FROM watched_items WHERE kind='series'").fetchone()[0]

        self.assertEqual(sequel_count, 1)
        self.assertEqual(final_count, 1)
        self.assertEqual(expand.call_count, 0)

    def test_wikidata_prequel_work_uses_its_own_watch_policy(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            root_id = add_watch(db, 'book', 'Book Two', 'Example Author',
                                'https://www.wikidata.org/entity/Q700', metadata={
                                    'entity_type': 'work', 'catalog_provider': 'Wikidata',
                                    'external_ids': [{'provider': 'wikidata',
                                                      'external_id': 'Q700'}],
                                })
            root = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (root_id,)).fetchone())
            prequel = {'relationships': [{
                'relationship_type': 'follows', 'target_type': 'work',
                'target_media_type': 'book', 'target_qid': 'Q701',
                'target_title': 'Book One',
                'target_source_url': 'https://www.wikidata.org/entity/Q701',
                'confidence': 0.9,
            }]}

            set_watch_policy(db, 'media_type', 'book',
                             {'direct_prequel_series': 'discover'})
            _apply_wikidata_watch_policy(db, root, prequel)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM watched_items WHERE title='Book One'").fetchone()[0], 1)

            set_watch_policy(db, 'media_type', 'book',
                             {'direct_prequel_works': 'discover'})
            another_id = add_watch(db, 'book', 'Sequel Work', 'Example Author',
                                   'https://www.wikidata.org/entity/Q702')
            another = dict(db.execute(
                'SELECT * FROM watched_items WHERE id=?', (another_id,)).fetchone())
            second_prequel = {'relationships': [{**prequel['relationships'][0],
                                                  'target_qid': 'Q703',
                                                  'target_title': 'Earlier Work',
                                                  'target_source_url':
                                                  'https://www.wikidata.org/entity/Q703'}]}
            _apply_wikidata_watch_policy(db, another, second_prequel)
            earlier_count = db.execute(
                "SELECT COUNT(*) FROM watched_items WHERE title='Earlier Work'").fetchone()[0]

        self.assertEqual(earlier_count, 0)

    def test_wikidata_sequel_series_expands_members_immediately_with_cycle_guard(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            root_id = add_watch(db, 'book', 'Book One', 'Example Author',
                                'https://www.wikidata.org/entity/Q500', metadata={
                                    'entity_type': 'work', 'catalog_provider': 'Wikidata',
                                    'external_ids': [{'provider': 'wikidata',
                                                      'external_id': 'Q500'}],
                                })
            root = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (root_id,)).fetchone())
            sequel = {'relationships': [{
                'relationship_type': 'followed_by', 'target_type': 'series',
                'target_media_type': 'book', 'target_qid': 'Q501',
                'target_title': 'Second Series',
                'target_source_url': 'https://www.wikidata.org/entity/Q501',
                'confidence': 0.9,
            }]}

            with patch('media_intake.watched._process_wikidata_series_collection') as expand:
                _apply_wikidata_watch_policy(db, root, sequel)
                expand.assert_called_once()
                self.assertEqual(expand.call_args.kwargs['qid_override'], 'Q501')
                self.assertEqual(expand.call_args.kwargs['continuity_path'], ['Q501'])

                # If the new series points back to itself, retain its watch but do
                # not recursively refresh the same node again.
                sequel_watch = dict(db.execute(
                    "SELECT * FROM watched_items WHERE kind='series' AND title='Second Series'"
                ).fetchone())
                _apply_wikidata_watch_policy(
                    db, sequel_watch, sequel, continuity_path=['Q501'])
                self.assertEqual(expand.call_count, 1)


if __name__ == '__main__':
    unittest.main()
