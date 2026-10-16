"""Connectors inside category search: approval gating, isolation, grouping, paging and settings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from connector_site import FakeSite, Reply, fixture
from media_intake.connectors import (load_source_config, missing_settings, save_source_config, search_sources,
                                     CONNECTORS)
from media_intake.core import connect
from media_intake.discovery import (DOWNLOAD_CONNECTORS, METADATA_CONNECTORS, connected_download_sources,
                                    search_media_category, search_more_connector_results)
from media_intake.rights_evidence import ensure_rights_evidence, load_rights_evidence


def approved_rights(folder, *keys):
    path = ensure_rights_evidence(Path(folder) / 'rights-evidence.json')
    document = json.loads(path.read_text(encoding='utf-8'))
    for key in keys:
        document['sources'][key]['approved_for_download'] = True
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def direct(base, **extra):
    return dict(base=base, enabled=True, access='direct', **extra)


class GatingTest(unittest.TestCase):
    def test_new_file_sources_start_as_no_in_a_fresh_and_an_existing_rights_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = ensure_rights_evidence(Path(folder) / 'rights-evidence.json')
            document = load_rights_evidence(path)
            for spec in CONNECTORS.values():
                if spec.file_capable:
                    self.assertIs(document['sources'][spec.key]['approved_for_download'], False, spec.key)
                else:
                    self.assertNotIn(spec.key, document['sources'])
            # An older file without the new rows gets them added as NO, keeping the user's other choices.
            older = json.loads(path.read_text(encoding='utf-8'))
            for spec in CONNECTORS.values():
                older['sources'].pop(spec.key, None)
            older['sources']['internet_archive']['approved_for_download'] = True
            path.write_text(json.dumps(older), encoding='utf-8')
            upgraded = load_rights_evidence(ensure_rights_evidence(path))
            self.assertIs(upgraded['sources']['nyaa']['approved_for_download'], False)
            self.assertIs(upgraded['sources']['internet_archive']['approved_for_download'], True)

    def test_unapproved_sources_are_not_contacted_and_say_why(self):
        with tempfile.TemporaryDirectory() as folder, FakeSite() as site:
            path = approved_rights(folder)
            site.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            found = search_sources('x', 'Videos', {'rights_evidence_path': str(path), 'source_configs': {
                'thepiratebay': direct(site.base)}})
            self.assertEqual(site.requests, [])
        self.assertEqual(found['rows'], [])
        self.assertIn('rights-evidence.json', found['skipped']['thepiratebay'])

    def test_without_a_rights_file_file_sources_fail_closed(self):
        with FakeSite() as site:
            found = search_sources('x', 'Videos', {'source_configs': {'thepiratebay': direct(site.base)}})
            self.assertEqual(site.requests, [])
        self.assertIn('thepiratebay', found['skipped'])

    def test_turned_off_and_unconfigured_sources_are_skipped_with_reasons(self):
        with tempfile.TemporaryDirectory() as folder:
            path = approved_rights(folder, 'nzbgeek', 'yts')
            found = search_sources('x', 'Videos', {'rights_evidence_path': str(path), 'source_configs': {
                'nzbgeek': direct('https://api.nzbgeek.info'), 'yts': dict(enabled=False)}})
        self.assertEqual(found['skipped']['yts'], 'turned off in Settings → Sources')
        self.assertEqual(found['skipped']['nzbgeek'], 'needs API key in Settings → Sources')

    def test_metadata_sources_need_no_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            path = approved_rights(folder)
            with patch('media_intake.connectors._SEARCHERS', {
                    **__import__('media_intake.connectors', fromlist=['_SEARCHERS'])._SEARCHERS,
                    'anidb': lambda spec, *args: __import__('media_intake.connectors.base', fromlist=['x'])
                    .SearchPage([{'title': 'Cowboy Bebop', 'source_role': 'metadata'}])}):
                found = search_sources('cowboy bebop', 'Anime', {'rights_evidence_path': str(path),
                                                                 'source_configs': {'anidb': {'enabled': True}}})
        self.assertEqual(found['searched'], ['AniDB'])
        self.assertEqual(found['rows'][0]['title'], 'Cowboy Bebop')


class IsolationAndDedupTest(unittest.TestCase):
    def test_a_broken_source_reports_an_error_and_the_rest_still_return_results(self):
        with tempfile.TemporaryDirectory() as folder, FakeSite() as good, FakeSite() as broken:
            path = approved_rights(folder, 'thepiratebay', 'yts', '1337x')
            good.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            good.route('GET', '/api/v2/list_movies.json', Reply(fixture('yts_list.json'),
                                                                content_type='application/json'))
            broken.route('GET', r'/.*', Reply(b'<html>Internal error</html>', 500))
            health = []
            found = search_sources('night of the living dead', 'Videos', {
                'rights_evidence_path': str(path), 'source_configs': {
                    'thepiratebay': direct(good.base), 'yts': direct(good.base), '1337x': direct(broken.base)}},
                health=lambda key, ok, detail: health.append((key, ok)))
        self.assertEqual(len(found['errors']), 1)
        self.assertTrue(found['errors'][0].startswith('1337x: the site is having problems'))
        self.assertIn(('1337x', False), health)
        self.assertIn(('thepiratebay', True), health)
        # The 1080p release is on both The Pirate Bay and YTS (same info hash): it appears once.
        hashes = [row['info_hash'] for row in found['rows']]
        self.assertEqual(len(hashes), len(set(hashes)))
        shared = next(row for row in found['rows'] if row['info_hash'] == 'a' * 40)
        self.assertEqual(shared['mirror_providers'], ['The Pirate Bay', 'YTS'])
        self.assertEqual(shared['seeders'], 120)
        self.assertTrue(all(row['rights_evidence_path'] == str(path) for row in found['rows']))
        self.assertEqual(found['next_pages'], {'yts': 2})

    def test_a_connector_bug_is_contained(self):
        with tempfile.TemporaryDirectory() as folder:
            path = approved_rights(folder, 'yts')
            with patch('media_intake.connectors.torrent_sites.search_yts', side_effect=KeyError('movies')), \
                    patch.dict('media_intake.connectors._SEARCHERS', {'yts': lambda *a: (_ for _ in ()).throw(
                        KeyError('movies'))}):
                found = search_sources('x', 'Videos', {'rights_evidence_path': str(path),
                                                       'source_configs': {'yts': direct('https://yts.mx')}})
        self.assertEqual(len(found['errors']), 1)
        self.assertIn('YTS: unexpected problem', found['errors'][0])


class CategorySearchTest(unittest.TestCase):
    def _search(self, site, path, category='Videos', **patches):
        options = {'rights_evidence_path': str(path), 'source_configs': {
            'thepiratebay': direct(site.base), 'yts': direct(site.base)}}
        with patch('media_intake.discovery.search_tvmaze', return_value=[]), \
                patch('media_intake.discovery._query_wikidata', return_value=[{
                    'provider': 'Wikidata', 'title': 'Night of the Living Dead', 'kind': 'video',
                    'video_subtype': 'movie', 'release_date': '1968-10-01',
                    'source_url': 'https://www.wikidata.org/wiki/Q208386'}]), \
                patch('media_intake.discovery._internet_archive_files', return_value=([], [])), \
                patch('media_intake.discovery._meawfy_files', return_value=([], [])), \
                patch('media_intake.discovery._softarchive_files', return_value=([], [])), \
                patch('media_intake.discovery._irc_files', return_value=([], [])):
            return search_media_category('night of the living dead', category, options), options

    def test_releases_group_under_the_work_with_metadata_and_seeders_order(self):
        with tempfile.TemporaryDirectory() as folder, FakeSite() as site:
            path = approved_rights(folder, 'thepiratebay', 'yts')
            site.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            site.route('GET', '/api/v2/list_movies.json', Reply(fixture('yts_list.json'),
                                                                content_type='application/json'))
            response, _options = self._search(site, path)
        titles = [(row['title'], row.get('release_year') or str(row.get('release_date'))[:4])
                  for row in response['results']]
        film = next(row for row in response['results'] if row['title'] == 'Night of the Living Dead'
                    and str(row.get('release_date') or row.get('release_year'))[:4] == '1968')
        self.assertIn('Wikidata', film['metadata_sources'])
        self.assertEqual(len(film['download_variants']), 3)  # TPB 1080p (also on YTS), TPB 720p, YTS 720p
        self.assertEqual([row['seeders'] for row in film['download_variants']][:1], [120])
        self.assertTrue(all(row['connector_row'] for row in film['download_variants']))
        remake = [row for row in response['results'] if str(row.get('release_year')) == '1990']
        self.assertEqual(len(remake), 1, titles)  # the 1990 remake stays a separate work
        self.assertIn('The Pirate Bay', response['searched'])
        self.assertEqual(response['connector_pages'], {'yts': 2})

    def test_load_more_adds_the_next_page_to_the_same_work(self):
        page_two = json.loads(fixture('yts_list.json'))
        page_two['data']['page_number'] = 2
        movie = page_two['data']['movies'][0]
        movie['torrents'] = [dict(movie['torrents'][0], hash='9' * 40, quality='2160p', seeds=3)]
        with tempfile.TemporaryDirectory() as folder, FakeSite() as site:
            path = approved_rights(folder, 'thepiratebay', 'yts')
            site.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            site.route('GET', '/api/v2/list_movies.json', lambda request: Reply(
                json.dumps(page_two) if request['query'].get('page') == ['2'] else fixture('yts_list.json'),
                content_type='application/json'))
            response, options = self._search(site, path)
            before = len(response['results'])
            more = search_more_connector_results('night of the living dead', 'Videos', options,
                                                 response['connector_pages'], response['results'])
        film = next(row for row in more['results'] if row['title'] == 'Night of the Living Dead'
                    and any(v.get('resolution') == '2160p' for v in row.get('download_variants') or []))
        self.assertEqual(len(more['results']), before)
        self.assertEqual(len(film['download_variants']), 4)
        self.assertEqual(more['connector_pages'], {})  # 23 movies: page 2 is the last
        self.assertEqual(more['added'], 1)

    def test_category_search_without_source_settings_leaves_connectors_out(self):
        with patch('media_intake.connectors.search_sources') as searcher, \
                patch('media_intake.discovery.search_sources') as discovery_searcher, \
                patch('media_intake.discovery.search_tvmaze', return_value=[]), \
                patch('media_intake.discovery._query_wikidata', return_value=[]), \
                patch('media_intake.discovery._internet_archive_files', return_value=([], [])), \
                patch('media_intake.discovery._meawfy_files', return_value=([], [])), \
                patch('media_intake.discovery._softarchive_files', return_value=([], [])):
            response = search_media_category('anything', 'Videos', {})
        searcher.assert_not_called()
        discovery_searcher.assert_not_called()
        self.assertNotIn('more sources', response['searched'])

    def test_anime_releases_survive_the_anime_only_filter(self):
        with tempfile.TemporaryDirectory() as folder, FakeSite() as site:
            path = approved_rights(folder, 'nyaa')
            site.route('GET', '/', Reply(fixture('nyaa_rss.xml'), content_type='application/rss+xml'))
            options = {'rights_evidence_path': str(path), 'source_configs': {'nyaa': direct(site.base)}}
            with patch('media_intake.discovery.search_anilist', return_value=[]), \
                    patch('media_intake.discovery.search_tvmaze', return_value=[]), \
                    patch('media_intake.discovery._query_wikidata', return_value=[]), \
                    patch('media_intake.discovery._internet_archive_files', return_value=([], [])), \
                    patch('media_intake.discovery._meawfy_files', return_value=([], [])), \
                    patch('media_intake.discovery._softarchive_files', return_value=([], [])), \
                    patch('media_intake.discovery._general_download_site_files', return_value=([], [])), \
                    patch('media_intake.discovery._irc_files', return_value=([], [])):
                response = search_media_category('big buck bunny', 'Anime', options)
        self.assertEqual(len(response['results']), 1)
        self.assertEqual(len(response['results'][0]['download_variants']), 2)
        self.assertEqual(response['results'][0]['kind'], 'anime')

    def test_sources_appear_in_the_right_categories(self):
        self.assertIn('Nyaa', DOWNLOAD_CONNECTORS['Anime'])
        self.assertIn('Nyaa', DOWNLOAD_CONNECTORS['Manga'])
        self.assertNotIn('Nyaa', DOWNLOAD_CONNECTORS['Videos'])
        self.assertIn('FitGirl Repacks', DOWNLOAD_CONNECTORS['Games'])
        self.assertIn("Vimm's Lair", DOWNLOAD_CONNECTORS['Games'])
        self.assertIn('Z-Library', DOWNLOAD_CONNECTORS['Books'])
        self.assertIn('MagzDB', DOWNLOAD_CONNECTORS['Books'])
        self.assertIn('YTS', DOWNLOAD_CONNECTORS['Videos'])
        self.assertNotIn('YTS', DOWNLOAD_CONNECTORS['Books'])
        self.assertIn('AniDB', METADATA_CONNECTORS['Anime'])
        self.assertIn('WorldCat', METADATA_CONNECTORS['Books'])
        for names in DOWNLOAD_CONNECTORS.values():
            for metadata_only in ('AniDB', 'WorldCat', 'AIO Search'):
                self.assertNotIn(metadata_only, names)

    def test_turned_off_sources_leave_the_priority_list(self):
        on = connected_download_sources('Videos', {'source_configs': {'yts': {'enabled': True}}})
        off = connected_download_sources('Videos', {'source_configs': {'yts': {'enabled': False}}})
        self.assertIn('YTS', on)
        self.assertNotIn('YTS', off)


class SettingsTest(unittest.TestCase):
    def test_settings_persist_and_secrets_never_land_in_plain_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            db = connect(folder)
            try:
                with patch('media_intake.kindle._dpapi_crypt', side_effect=RuntimeError('no DPAPI')):
                    session_only = save_source_config(db, 'nzbgeek', {
                        'base': 'https://api.nzbgeek.info', 'api_key': 'SECRET', 'enabled': False, 'access': 'auto'})
                self.assertEqual(session_only, ['api_key'])
                stored = dict(db.execute('SELECT key, value FROM gui_settings').fetchall())
                self.assertNotIn('SECRET', json.dumps(stored))
                config = load_source_config(db, 'nzbgeek')
                self.assertEqual((config['base'], config['enabled'], config['api_key']),
                                 ('https://api.nzbgeek.info', False, 'SECRET'))
                with patch.dict('os.environ', {'MEDIA_INTAKE_DRUNKENSLUG_API_KEY': 'ENVKEY'}):
                    self.assertEqual(load_source_config(db, 'drunkenslug')['api_key'], 'ENVKEY')
                with self.assertRaises(ValueError):
                    save_source_config(db, 'yts', {'access': 'proxy'})
            finally:
                db.close()

    def test_secrets_use_protected_storage_when_available(self):
        with tempfile.TemporaryDirectory() as folder:
            db = connect(folder)
            try:
                with patch('media_intake.kindle._dpapi_crypt', side_effect=lambda payload, protect, **_: payload[::-1]):
                    self.assertEqual(save_source_config(db, 'rutracker', {'username': 'u', 'password': 'pw!'}), [])
                    from media_intake import connectors
                    connectors._SESSION_SECRETS.clear()
                    self.assertEqual(load_source_config(db, 'rutracker')['password'], 'pw!')
            finally:
                db.close()

    def test_aio_search_is_off_by_default_and_missing_settings_are_named(self):
        self.assertFalse(load_source_config(None, 'aiosearch')['enabled'])
        self.assertTrue(load_source_config(None, 'nyaa')['enabled'])
        self.assertEqual(missing_settings(CONNECTORS['z-library'], {'base': 'https://x.example'}), ['account'])
        self.assertEqual(missing_settings(CONNECTORS['worldcat'], {}), ['client_id', 'client_secret'])


if __name__ == '__main__':
    unittest.main()
