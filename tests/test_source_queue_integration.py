import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_intake import external_downloaders as ext
from media_intake import source_health
from media_intake.core import connect, download, enqueue, enqueue_for_watch, find_duplicate, item_identity_key, list_items

FIXTURES = json.loads((Path(__file__).parent / 'fixtures' / 'source_catalog' / 'queue_items.json').read_text(encoding='utf-8'))
HEX = FIXTURES['hash_hex']


def magnet(digest, **extra):
    query = f'xt=urn:btih:{digest}' + ''.join(f'&{key}={value}' for key, value in extra.items())
    return {'title': 'Example', 'format': '.torrent', 'source': 'fixture', 'url': 'magnet:?' + query}


class Workspace(unittest.TestCase):
    """Each test runs against a clean --root, like a first CLI run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = connect(self.tmp.name)
        self.addCleanup(self.db.close)


class IdentityTests(Workspace):
    def test_catalogued_items_carry_a_canonical_source_identity(self):
        for name, key, label in (('yts', 'yts', 'YTS'), ('yts_alias', 'yts', 'YTS'), ('nyaa', 'nyaa', 'Nyaa')):
            row = enqueue(self.db, FIXTURES[name])
            metadata = json.loads(row['metadata'])
            self.assertEqual((metadata['source_key'], metadata['source_name']), (key, label), name)
            self.assertTrue(metadata['dedupe_key'].startswith(key + ':'))

    def test_older_metadata_with_an_alias_source_key_is_canonicalised(self):
        item = dict(FIXTURES['legacy_metadata'], url='https://cdn.example.test/film.torrent')
        self.assertEqual(json.loads(enqueue(self.db, item)['metadata'])['source_key'], 'yts')

    def test_uncatalogued_items_are_left_alone(self):
        metadata = json.loads(enqueue(self.db, FIXTURES['plain_file'])['metadata'])
        self.assertNotIn('dedupe_key', metadata)
        self.assertNotIn('source_name', metadata)
        self.assertNotIn('source_key', metadata)
        explicit = dict(FIXTURES['plain_file'], url='https://files.example.test/b.epub', source_key='internet_archive')
        self.assertEqual(json.loads(enqueue(self.db, explicit)['metadata'])['source_key'], 'internet_archive')

    def test_identity_keys(self):
        self.assertEqual(item_identity_key(magnet(HEX.upper())), 'btih:' + HEX)
        self.assertEqual(item_identity_key(magnet(base64.b32encode(bytes.fromhex(HEX)).decode())), 'btih:' + HEX)
        self.assertEqual(item_identity_key(magnet('zz')), None)
        self.assertEqual(item_identity_key({'url': 'https://www.yts.mx/a/b?x=1#frag'}), 'yts:/a/b?x=1')
        self.assertIsNone(item_identity_key({'url': 'https://files.example.test/a'}))


class DuplicateTests(Workspace):
    def test_alias_domains_do_not_create_a_second_row(self):
        first = enqueue(self.db, FIXTURES['yts'])
        second = enqueue(self.db, FIXTURES['yts_alias'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(list_items(self.db)), 1)
        self.assertEqual(second['url'], FIXTURES['yts']['url'])
        self.assertEqual(json.loads(second['metadata'])['alternate_urls'], [FIXTURES['yts_alias']['url']])

    def test_magnet_spellings_and_trackers_are_one_item(self):
        first = enqueue(self.db, magnet(HEX))
        for variant in (magnet(HEX.upper()), magnet(HEX, tr='udp%3A%2F%2Ftracker.example%3A1337', dn='Other+Name')):
            self.assertEqual(enqueue(self.db, variant)['id'], first['id'])
        self.assertEqual(len(list_items(self.db)), 1)

    def test_base32_magnet_matches_its_hex_form(self):
        digest32 = base64.b32encode(bytes.fromhex(HEX)).decode()
        self.assertEqual(enqueue(self.db, magnet(HEX))['id'], enqueue(self.db, magnet(digest32))['id'])

    def test_similar_but_different_sources_stay_separate(self):
        enqueue(self.db, FIXTURES['nyaa'])
        enqueue(self.db, FIXTURES['nyaa_lookalike'])
        same_path_elsewhere = dict(FIXTURES['nyaa'], url='https://yts.bz/download/1.torrent', source='yts.bz')
        enqueue(self.db, same_path_elsewhere)
        self.assertEqual(len(list_items(self.db)), 3)

    def test_rows_saved_before_identity_keys_existed_are_found(self):
        legacy = dict(FIXTURES['yts'])
        self.db.execute('INSERT INTO items(url, metadata) VALUES (?, ?)', (legacy['url'], json.dumps(legacy)))
        self.db.execute('INSERT INTO items(url, metadata) VALUES (?, ?)', (magnet(HEX)['url'], json.dumps(magnet(HEX))))
        self.db.commit()
        self.assertEqual(enqueue(self.db, FIXTURES['yts_alias'])['url'], legacy['url'])
        self.assertEqual(enqueue(self.db, magnet(HEX.upper(), dn='x'))['url'], magnet(HEX)['url'])
        self.assertEqual(len(list_items(self.db)), 2)

    def test_the_same_url_still_updates_in_place(self):
        enqueue(self.db, FIXTURES['plain_file'])
        again = enqueue(self.db, dict(FIXTURES['plain_file'], title='Renamed'))
        self.assertEqual((len(list_items(self.db)), json.loads(again['metadata'])['title']), (1, 'Renamed'))

    def test_watch_requests_reuse_a_duplicate_under_another_alias(self):
        first = enqueue(self.db, FIXTURES['yts'])
        row = enqueue_for_watch(self.db, FIXTURES['yts_alias'], 7)
        self.assertEqual(row['id'], first['id'])
        self.assertEqual(len(list_items(self.db)), 1)
        self.assertEqual(find_duplicate(self.db, dict(FIXTURES['yts_alias'], dedupe_key='yts:/torrent/download/ABC123'))['id'], first['id'])


class HealthTests(Workspace):
    def test_download_outcomes_are_recorded_under_the_canonical_key(self):
        row = enqueue(self.db, FIXTURES['yts_alias'])
        with patch('media_intake.core._download', side_effect=ValueError('HTTP 404')), self.assertRaises(ValueError):
            download(self.db, row['id'], self.tmp.name)
        summary = source_health.summary(self.db, 'yts')
        self.assertEqual((summary['downloads_failed'], summary['last_error']), (1, 'HTTP 404'))
        self.assertEqual([s['source_key'] for s in source_health.summaries(self.db)], ['yts'])

    def test_history_saved_under_an_alias_merges_without_a_migration(self):
        source_health.record(self.db, 'softarchive.download', 'search', True)
        self.db.execute("INSERT INTO source_events(source_key, kind, ok, detail) VALUES ('yts.mx', 'download', 0, 'old')")
        self.db.commit()
        source_health.record(self.db, 'YTS.BZ', 'download', False, detail='new')
        summary = source_health.summary(self.db, 'yts.mx')
        self.assertEqual((summary['source_key'], summary['downloads_failed']), ('yts', 2))
        self.assertEqual(sorted(s['source_key'] for s in source_health.summaries(self.db)), ['softarchive.download', 'yts'])
        source_health.clear(self.db, 'yts')
        self.assertEqual([s['source_key'] for s in source_health.summaries(self.db)], ['softarchive.download'])

    def test_unknown_keys_behave_as_before(self):
        source_health.record(self.db, '  Some.Site ', 'search', True)
        self.assertEqual(source_health.summary(self.db, 'some.site')['searches_ok'], 1)


class ExternalManagerTests(unittest.TestCase):
    def _labels(self, item):
        calls = []

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _size):
                return b'{"code": 0, "data": "t"}'

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            return Reply()

        ext.hand_off(item, {'downloader': 'gopeed', 'gopeed_url': 'http://127.0.0.1:9999'}, opener=opener)
        return calls[0]['opts'], calls[0]['req'].get('labels')

    def test_source_identity_reaches_the_download_manager(self):
        _opts, labels = self._labels(FIXTURES['yts_alias'])
        self.assertEqual(labels['source_key'], 'yts')
        _opts, labels = self._labels(dict(magnet(HEX), source='https://nyaa.si/view/1'))
        self.assertEqual(labels['source_key'], 'nyaa')

    def test_generic_items_keep_their_existing_labels(self):
        _opts, labels = self._labels(FIXTURES['plain_file'])
        self.assertEqual(labels, {'source': 'Media Intake', 'title': 'Plain Book'})


if __name__ == '__main__':
    unittest.main()
