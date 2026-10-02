"""Catalog enrichment for webcomic series: proposals, acceptance, conflicts, locks, refresh durability."""
import json
import tempfile
import unittest

from media_intake.core import connect
from media_intake.webcomics import metadata_bridge as bridge
from media_intake.webcomics.models import Snapshot, WebcomicEntry, WebcomicSeries
from media_intake.webcomics.watcher import apply_snapshot

SECRET = 'cv-secret-key'


def catalog_results():
    return {
        'anilist': lambda query, credentials: [
            {'external_id': '101', 'title': 'Fixture Comic', 'creators': [{'name': 'Ada Artist', 'role': 'creator'}],
             'description': 'From AniList', 'status': 'RELEASING', 'source_url': 'https://anilist.co/manga/101'},
            {'external_id': '102', 'title': 'Fixture Comic Spin-off', 'source_url': 'https://anilist.co/manga/102'}],
        'comicvine': lambda query, credentials: (
            [{'external_id': '4050-9', 'title': 'Fixture Comic!', 'creators': [{'name': 'A. Artist', 'role': 'creator'}],
              'description': 'From Comic Vine', 'cover_url': 'https://cv.example/cover.jpg',
              'source_url': 'https://comicvine.example/9'}] if credentials.get('comicvine_api_key') == SECRET else []),
        'wikidata': lambda query, credentials: (_ for _ in ()).throw(OSError('Wikidata offline')),
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        self.db.execute("INSERT INTO watched_items(kind,title,state,metadata) VALUES('webcomic','Fixture Comic','watching','{}')")
        self.db.execute("INSERT INTO webcomic_series(id,metadata) VALUES('canonical',?)", (json.dumps({
            'title': 'Fixture Comic', 'creators': [{'name': 'Ada Artist', 'role': 'author'}]}),))
        self.db.execute("INSERT INTO webcomic_sources(series_id,watch_id,url,adapter,adapter_version) VALUES('canonical',1,'https://comic.example/','feed','1')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def refresh(self, title='Fixture Comic'):
        series = WebcomicSeries('canonical', title, 'https://comic.example/', 'feed',
                                creators=[{'name': 'Ada Artist', 'role': 'author'}])
        series.metadata_provenance['title'] = {'candidates': [], 'selected': {'value': title, 'confidence': 1.0}}
        snapshot = Snapshot(series, [WebcomicEntry('1', 'https://comic.example/1', 'Page 1', ordinal=1)],
                            metrics={'entry_count': 1})
        apply_snapshot(self.db, 1, snapshot)
        self.db.commit()
        return json.loads(self.db.execute("SELECT metadata FROM webcomic_series WHERE id='canonical'").fetchone()[0])

    def test_two_catalogs_conflicts_preserved_and_credentials_never_stored(self):
        self.assertEqual(bridge.available_catalogs({}), ['anilist', 'wikidata', 'mangaupdates'])
        found, errors = bridge.find_matches(self.db, 'canonical', ['anilist', 'comicvine', 'wikidata'],
                                            {'comicvine_api_key': SECRET}, search=catalog_results())
        self.assertEqual(errors, ['Wikidata: Wikidata offline'])
        self.assertEqual([(m['provider'], m['external_id'], m['score']) for m in found][:2],
                         [('anilist', '101', 0.95), ('comicvine', '4050-9', 0.75)])  # punctuation-insensitive title
        self.assertLess(found[-1]['score'], 0.75)
        stored = json.dumps([dict(row) for row in self.db.execute('SELECT * FROM webcomic_metadata_matches')])
        self.assertNotIn(SECRET, stored)
        # Nothing is applied until accepted.
        self.assertEqual(self.refresh().get('description'), None)
        for match in found[:2]:
            bridge.set_match_status(self.db, match['id'], 'accepted')
        bridge.set_match_status(self.db, found[-1]['id'], 'rejected')
        data = self.refresh()
        self.assertEqual(data['title'], 'Fixture Comic')  # the source's own title outranks catalogs
        self.assertEqual(data['external_ids'], {'anilist': '101', 'comicvine': '4050-9'})
        self.assertEqual(data['cover_url'], 'https://cv.example/cover.jpg')
        descriptions = [c['value'] for c in data['metadata_provenance']['description']['candidates']]
        self.assertEqual(sorted(descriptions), ['From AniList', 'From Comic Vine'])
        titles = [c['value'] for c in data['metadata_provenance']['title']['candidates']]
        self.assertIn('Fixture Comic!', titles)
        self.assertNotIn(SECRET, json.dumps(data))
        self.assertNotIn('Fixture Comic Spin-off', json.dumps(data))

    def test_locks_survive_refresh(self):
        found, _errors = bridge.find_matches(self.db, 'canonical', ['anilist'], search=catalog_results())
        bridge.set_match_status(self.db, found[0]['id'], 'accepted')
        bridge.lock_field(self.db, 'canonical', 'title', 'Fixture Comic (Remastered)')
        bridge.lock_field(self.db, 'canonical', 'description', 'My own summary')
        for page_title in ('Fixture Comic', 'FIXTURE COMIC – Home'):
            data = self.refresh(page_title)
            self.assertEqual((data['title'], data['description']), ('Fixture Comic (Remastered)', 'My own summary'))
        self.assertEqual(self.db.execute('SELECT title FROM watched_items').fetchone()[0], 'Fixture Comic (Remastered)')
        bridge.unlock_field(self.db, 'canonical', 'description')
        self.assertEqual(self.refresh()['description'], 'From AniList')
        with self.assertRaises(ValueError):
            bridge.lock_field(self.db, 'canonical', 'canonical_id', 'x')


if __name__ == '__main__':
    unittest.main()
