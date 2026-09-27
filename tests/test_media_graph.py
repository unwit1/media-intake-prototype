import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from media_intake.core import connect
from media_intake.media_graph import (add_media_relationship, capture_graph_snapshot,
                                     list_graph_reviews, queue_graph_review,
                                     resolve_graph_review, upsert_media_entity)
from media_intake.watched import add_watch
from media_intake.wikidata import import_wikidata_entity, search_wikidata


class MediaGraphTest(unittest.TestCase):
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

    def test_external_id_conflict_does_not_silently_merge_entities(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            first = upsert_media_entity(db, 'work', 'First', external_ids=[('wikidata', 'Q1')])
            second = upsert_media_entity(db, 'work', 'Second')
            with self.assertRaisesRegex(ValueError, 'already attached'):
                upsert_media_entity(db, 'work', 'Different title', entity_id=second,
                                    external_ids=[('wikidata', 'Q1')])
            self.assertNotEqual(first, second)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_entities').fetchone()[0], 2)

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
                             {'entity_added', 'relationship_added'})
            unchanged = capture_graph_snapshot(db, root)
            self.assertTrue(unchanged['unchanged'])
            self.assertEqual(unchanged['changes'], [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_graph_snapshots').fetchone()[0], 2)
            db.execute('DELETE FROM media_relationships')
            reverted = capture_graph_snapshot(db, root)
            self.assertEqual({item['change_type'] for item in reverted['changes']},
                             {'entity_removed', 'relationship_removed'})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM media_graph_snapshots').fetchone()[0], 3)

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
            self.assertEqual(list_graph_reviews(db), [])


if __name__ == '__main__':
    unittest.main()
