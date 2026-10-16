import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_intake import source_catalog as catalog
from media_intake.rights_evidence import source_key_for_item

REFERENCE = {
    # id: (name, domain identifier, coverage)
    'yts': ('YTS', 'yts.bz', 'Film'),
    'the-pirate-bay': ('The Pirate Bay', 'thepiratebay.org', 'General index'),
    'nyaa': ('Nyaa', 'nyaa.si', 'Anime'),
    'fitgirl-repacks': ('FitGirl Repacks', 'fitgirl-repacks.site', 'Games'),
    'rutracker': ('RuTracker', 'rutracker.org', 'Mixed media'),
    'mangadex': ('MangaDex', 'mangadex.org', 'Manga / comics'),
    'pixeldrain': ('Pixeldrain', 'pixeldrain.com', 'File hosting'),
}


class CatalogRecordTests(unittest.TestCase):
    def test_reference_identities_are_registered(self):
        found = {record['id']: record for record in catalog.records()}
        self.assertEqual(set(found), set(REFERENCE))
        for source_id, (name, domain, coverage) in REFERENCE.items():
            with self.subTest(source=source_id):
                record = found[source_id]
                self.assertEqual((record['name'], record['coverage']), (name, coverage))
                self.assertIn(domain, record['domains'])
                self.assertEqual(record['source_key'], source_id)

    def test_every_record_resolves_by_id_name_and_domain(self):
        for source_id, (name, domain, _coverage) in REFERENCE.items():
            for value in (source_id, name, name.upper(), name.replace(' ', ''), domain,
                          f'https://{domain}/path?q=1', f'https://www.{domain}/', f'cdn.{domain}', f'{domain}:443'):
                with self.subTest(source=source_id, value=value):
                    self.assertEqual(catalog.source_key(value), source_id)

    def test_documented_domain_aliases_share_one_record(self):
        # Two dated reports list different YTS domains; neither is asserted current.
        self.assertIs(catalog.lookup('yts.mx'), catalog.lookup('yts.bz'))
        self.assertEqual(len([r for r in catalog.records() if r['id'] == 'yts']), 1)

    def test_similar_names_are_not_confused(self):
        for value in ('notyts.bz', 'yts.bz.example.net', 'myyts.mx', 'nyaa.eu', 'nyaa.si.example.org',
                      'thepiratebay.example', 'pixeldrain.net', 'mangadex.com', 'rutracker.net',
                      'fitgirl-repacks.com', 'Nyaa Torrents Mirror', '', None, '   ', 'magnet:?xt=urn:btih:abc'):
            with self.subTest(value=value):
                self.assertIsNone(catalog.lookup(value))

    def test_documented_distinct_identities_never_resolve(self):
        for value, owner in (('YIFY', 'yts'), ('yify', 'yts'), ('NyaaTorrents', 'nyaa'), ('nyaa-torrents', 'nyaa')):
            with self.subTest(value=value):
                self.assertIsNone(catalog.lookup(value))
                self.assertEqual(catalog.distinct_from(value)['id'], owner)

    def test_existing_adapters_are_referenced_not_duplicated(self):
        manga = catalog.resolve_adapters(catalog.lookup('mangadex'))
        self.assertTrue(callable(manga['search']) and callable(manga['chapters']))
        self.assertEqual(catalog.resolve_adapters(catalog.lookup('pixeldrain'))['file_host'].id, 'pixeldrain')
        for source_id in ('yts', 'the-pirate-bay', 'nyaa', 'fitgirl-repacks', 'rutracker'):
            record = catalog.lookup(source_id)
            self.assertEqual(record['adapters'], {})
            self.assertTrue(record['limitation'], f'{source_id} must state why it has no adapter')

    def test_normalize_host(self):
        for value, host in (('HTTPS://WWW.Example.COM:8080/x', 'example.com'), ('example.com.', 'example.com'),
                            ('www.example.com', 'example.com'), ('//a.b/c', 'a.b'), ('', ''), (None, ''),
                            ('http://[bad', '')):
            self.assertEqual(catalog.normalize_host(value), host, value)


class CatalogValidationTests(unittest.TestCase):
    def _with(self, mutate):
        document = json.loads(Path(catalog.CATALOG_PATH).read_text(encoding='utf-8'))
        mutate(document)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'catalog.json'
            path.write_text(json.dumps(document), encoding='utf-8')
            with patch.object(catalog, 'CATALOG_PATH', path):
                catalog.reload()
                try:
                    return catalog.records()
                finally:
                    catalog.reload()

    def test_shipped_catalog_is_valid(self):
        self.assertEqual(len(self._with(lambda d: None)), len(REFERENCE))

    def test_rejects_a_domain_claimed_twice(self):
        with self.assertRaisesRegex(ValueError, 'belongs to both'):
            self._with(lambda d: d['sources'][1]['domains'].append('yts.bz'))

    def test_rejects_duplicate_ids_and_keys_and_names(self):
        with self.assertRaisesRegex(ValueError, 'duplicate source id'):
            self._with(lambda d: d['sources'][1].update(id='yts'))
        with self.assertRaisesRegex(ValueError, 'duplicate source source_key'):
            self._with(lambda d: d['sources'][1].update(source_key='yts'))
        with self.assertRaisesRegex(ValueError, 'belongs to both'):
            self._with(lambda d: d['sources'][1]['aliases'].append('YTS'))

    def test_rejects_a_distinct_identity_that_is_also_a_name(self):
        with self.assertRaisesRegex(ValueError, 'also a known name'):
            self._with(lambda d: d['sources'][0]['distinct_identities'].append('Nyaa'))

    def test_rejects_unnormalised_domains_and_missing_fields(self):
        with self.assertRaisesRegex(ValueError, 'lower-case bare domain'):
            self._with(lambda d: d['sources'][0]['domains'].append('WWW.Yts.Bz'))
        with self.assertRaisesRegex(ValueError, 'missing'):
            self._with(lambda d: d['sources'][0].pop('coverage'))


class SourceKeyForItemTests(unittest.TestCase):
    def test_catalog_hosts_and_documented_aliases(self):
        for url, key in (('https://yts.bz/torrent/x.torrent', 'yts'), ('https://www.yts.mx/m', 'yts'),
                         ('https://nyaa.si/download/1.torrent', 'nyaa'),
                         ('https://thepiratebay.org/torrent/1', 'the-pirate-bay'),
                         ('https://fitgirl-repacks.site/game/', 'fitgirl-repacks'),
                         ('https://rutracker.org/forum/viewtopic.php?t=1', 'rutracker'),
                         ('https://mangadex.org/title/abc', 'mangadex'),
                         ('https://pixeldrain.com/api/file/abc', 'pixeldrain')):
            with self.subTest(url=url):
                self.assertEqual(source_key_for_item({'url': url}), key)

    def test_magnets_use_the_page_or_site_they_came_from(self):
        magnet = 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567'
        self.assertEqual(source_key_for_item({'url': magnet, 'source': 'https://nyaa.si/view/1'}), 'nyaa')
        self.assertEqual(source_key_for_item({'url': magnet, 'provider': 'The Pirate Bay'}), 'the-pirate-bay')
        self.assertEqual(source_key_for_item({'url': magnet, 'site': 'yts.mx'}), 'yts')
        self.assertEqual(source_key_for_item({'url': magnet}), 'direct_file_url')
        self.assertEqual(source_key_for_item({'url': magnet, 'source': 'https://nyaa.eu/view/1'}), 'direct_file_url')

    def test_older_metadata_with_an_alias_as_its_source_key(self):
        for stored, key in (('yts.mx', 'yts'), ('YTS.BZ', 'yts'), ('Nyaa', 'nyaa'), ('thepiratebay.org', 'the-pirate-bay')):
            self.assertEqual(source_key_for_item({'url': 'https://cdn.example/f.bin', 'source_key': stored}), key)
        self.assertEqual(source_key_for_item({'url': 'https://cdn.example/f.bin', 'source_key': 'yify'}), 'yify')

    def test_existing_source_keys_are_unchanged(self):
        for url, key in (('https://archive.org/download/a/b.epub', 'internet_archive'),
                         ('https://www.softarchive.download/x.zip', 'softarchive.download'),
                         ('https://meawfy.com/f.epub', 'meawfy.com'), ('https://github.com/a/b/releases/x.zip', 'github_foss'),
                         ('https://getcomics.org/x/', 'getcomics.org'), ('irc+xdcc://srv/bot/1', 'irc'),
                         ('https://cdn.example/f.bin', 'direct_file_url')):
            self.assertEqual(source_key_for_item({'url': url}), key)
        self.assertEqual(source_key_for_item({'url': 'https://x/f', 'source_key': 'internet_archive'}), 'internet_archive')
        self.assertEqual(source_key_for_item({'url': 'https://x/f', 'source_key': 'comicvine'}), 'direct_file_url')


if __name__ == '__main__':
    unittest.main()
