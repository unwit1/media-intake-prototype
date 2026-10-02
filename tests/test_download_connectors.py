import json
import urllib.error
import urllib.parse
import unittest
from unittest.mock import patch

from media_intake.download_connectors import (ANIME_DOWNLOAD_ADAPTERS,
                                               GENERAL_DOWNLOAD_ADAPTERS,
                                               formats_for_category,
                                               resolve_softarchive_downloads,
                                               resolve_site_downloads,
                                               search_fosshub_foss,
                                               search_github_foss,
                                               search_internet_archive,
                                               search_google_cse_browser,
                                               search_registered_download_site,
                                               search_site_downloads,
                                               search_softarchive,
                                               search_sourceforge_foss,
                                               search_xdcc_eu)


class DownloadConnectorTest(unittest.TestCase):
    def test_fosshub_catalog_search_returns_metadata_only_for_explicit_foss(self):
        catalog = {'items': [
            {'name': 'Open Project', 'url': 'https://www.fosshub.com/Open-Project.html',
             'description': 'A free open-source editor.', 'category': 'Tools',
             'operatingSystem': ['Windows'], 'softwareVersion': '1.2'},
            {'name': 'Freeware Utility', 'url': 'https://www.fosshub.com/Freeware.html',
             'description': 'A free utility for Windows.', 'category': 'Tools'},
        ]}
        with patch('media_intake.download_connectors.http_get',
                   return_value=(json.dumps(catalog).encode(),
                                 'https://www.fosshub.com/software-catalog.json')):
            rows = search_fosshub_foss('Open Project')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_key'], 'fosshub.com')
        self.assertEqual(rows[0]['source_role'], 'metadata')
        self.assertEqual(rows[0]['format'], 'catalog')
        self.assertEqual(rows[0]['catalog_metadata']['software_version'], '1.2')

    def test_sourceforge_resolves_only_supported_open_source_release_packages(self):
        search_page = (b'<a href="/projects/open-editor/">Open Editor</a>'
                       b'<a href="/projects/unrelated/">Unrelated Tool</a>')
        release = {'release': {'filename': '/1.0/open-editor.tar.xz', 'bytes': 4096,
                               'sha256sum': 'abc'},
                   'platform_releases': {
                       'windows': {'filename': '/1.0/editor.msi', 'bytes': 5000},
                       'docs': {'filename': '/1.0/manual.pdf', 'bytes': 2000},
                   }}

        def fake_http_get(url, **_kwargs):
            if '/directory/?' in url:
                return search_page, url
            if '/projects/open-editor/best_release.json' in url:
                return json.dumps(release).encode(), url
            if '/projects/unrelated/best_release.json' in url:
                return json.dumps({'release': {'filename': '/1.0/manual.pdf'}}).encode(), url
            raise AssertionError(f'Unexpected SourceForge request: {url}')

        with patch('media_intake.download_connectors.http_get', side_effect=fake_http_get) as get:
            rows = search_sourceforge_foss('Open Editor', limit=10)

        self.assertEqual({row['format'] for row in rows}, {'.tar.xz', '.msi'})
        self.assertEqual({row['source_key'] for row in rows}, {'sourceforge.net'})
        self.assertTrue(all(row['software_license'] == 'OSI-approved-open-source'
                            for row in rows))
        self.assertTrue(any('license=osi-approved-open-source' in call.args[0]
                            for call in get.call_args_list))
        self.assertTrue(any(row['url'].endswith('/1.0/open-editor.tar.xz/download')
                            for row in rows))

    def test_github_search_requires_an_identified_open_source_license_and_release_assets(self):
        search = {'items': [
            {'full_name': 'open/editor', 'name': 'editor', 'html_url': 'https://github.com/open/editor',
             'owner': {'login': 'open'}, 'license': {'spdx_id': 'MIT'}},
            {'full_name': 'unknown/tool', 'license': None},
        ]}
        releases = [{'tag_name': 'v1.0', 'html_url': 'https://github.com/open/editor/releases/tag/v1.0',
                     'published_at': '2025-01-01T00:00:00Z', 'assets': [
                         {'name': 'editor.zip', 'browser_download_url':
                          'https://github.com/open/editor/releases/download/v1.0/editor.zip',
                          'size': 1234, 'digest': 'sha256:abc'},
                         {'name': 'notes.txt', 'browser_download_url':
                          'https://github.com/open/editor/releases/download/v1.0/notes.txt', 'size': 20},
                     ]}]

        def fake_http_get(url, **_kwargs):
            if '/search/repositories?' in url:
                return json.dumps(search).encode(), url
            if '/repos/open/editor/releases?' in url:
                return json.dumps(releases).encode(), url
            raise AssertionError(f'Unexpected GitHub request: {url}')

        with patch('media_intake.download_connectors.http_get', side_effect=fake_http_get):
            rows = search_github_foss('editor')

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_key'], 'github_foss')
        self.assertEqual(rows[0]['software_license'], 'MIT')
        self.assertEqual(rows[0]['format'], '.zip')

    def test_anime_adapters_search_and_resolve_category_compatible_video_files(self):
        listing = (b'<article><h2><a href="/anime/example-anime">'
                   b'Example Anime Series</a></h2></article>')
        detail = (b'<main><a href="https://cdn.example.test/example.mkv">MKV</a>'
                  b'<a href="https://cdn.example.test/wrong.epub">EPUB</a></main>')
        for source_key, provider, searcher, resolver in ANIME_DOWNLOAD_ADAPTERS:
            with self.subTest(source=provider):
                base_url = searcher.__defaults__[0]
                with patch('media_intake.download_connectors.http_get',
                           return_value=(listing, base_url + '?q=Example+Anime')) as get:
                    hits = searcher('Example Anime')
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]['source_key'], source_key)
                self.assertEqual(hits[0]['provider'], provider)
                search_query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(get.call_args.args[0]).query)
                self.assertIn(search_query.get('s') or search_query.get('q'),
                              [['Example Anime']])
                with patch('media_intake.download_connectors.http_get',
                           return_value=(detail, hits[0]['detail_url'])):
                    files = resolver(hits[0],
                                     allowed_formats=formats_for_category('Anime'))
                self.assertEqual([item['format'] for item in files], ['.mkv'])
                self.assertEqual(files[0]['source_key'], source_key)

    def test_internet_archive_uses_public_epub_and_rejects_restricted_files(self):
        search_payload = {'response': {'docs': [
            {'identifier': 'example-book', 'title': 'Example Book',
             'creator': ['Example Author'], 'mediatype': 'texts'},
            {'identifier': 'borrowed-book', 'title': 'Example Book',
             'creator': 'Example Author', 'mediatype': 'texts'},
        ]}}
        archive_payload = {
            'metadata': {'title': 'Example Book', 'creator': 'Example Author'},
            'files': [
                {'name': 'Example_book_encrypted.epub', 'format': 'Encrypted EPUB'},
                {'name': 'private-copy.epub', 'format': 'EPUB', 'private': 'true'},
                {'name': 'Example_book.pdf', 'format': 'Text PDF', 'size': '1024'},
                {'name': 'Example_book.epub', 'format': 'EPUB', 'size': '2048'},
            ],
        }
        restricted_payload = {
            'metadata': {'title': 'Example Book', 'access-restricted-item': 'true'},
            'files': [{'name': 'borrowed.epub', 'format': 'EPUB'}],
        }

        def fake_http_get(url, **_kwargs):
            if '/advancedsearch.php?' in url:
                return json.dumps(search_payload).encode(), url
            if '/metadata/example-book' in url:
                return json.dumps(archive_payload).encode(), url
            if '/metadata/borrowed-book' in url:
                return json.dumps(restricted_payload).encode(), url
            raise AssertionError(f'Unexpected Internet Archive request: {url}')

        with patch('media_intake.download_connectors.http_get', side_effect=fake_http_get):
            results = search_internet_archive('Example Book', category='Books')

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['format'], '.epub')
        self.assertEqual(results[0]['author'], 'Example Author')
        self.assertEqual(results[0]['source_key'], 'internet_archive')
        self.assertEqual(results[0]['url'],
                         'https://archive.org/download/example-book/Example_book.epub')

    def test_internet_archive_rejects_redirects_outside_archive(self):
        url = 'https://archive.org/advancedsearch.php?q=title%3A%28%22Example%22%29'
        with patch('media_intake.download_connectors.http_get',
                   return_value=(b'{}', 'https://other.example/advancedsearch.php')):
            with self.assertRaisesRegex(ValueError, 'redirected outside archive.org'):
                search_internet_archive('Example')

    def test_softarchive_search_reads_matching_detail_pages_from_full_listing(self):
        page = b'''<html><nav><a href="/full/category">Category</a></nav>
        <article><h2><a href="/full/example-game">Example Game for Windows</a></h2></article>
        <article><h2><a href="/full/unrelated">Unrelated Tool</a></h2></article></html>'''
        with patch('media_intake.download_connectors.http_get',
                   return_value=(page, 'https://softarchive.download/full?s=Example+Game')) as get:
            results = search_softarchive('Example Game')

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Example Game for Windows')
        self.assertEqual(results[0]['detail_url'],
                         'https://softarchive.download/full/example-game')
        self.assertEqual(results[0]['source_key'], 'softarchive.download')
        self.assertIn('s=Example+Game', get.call_args.args[0])

    def test_softarchive_resolves_supported_direct_archive_links(self):
        detail = {'title': 'Example Game', 'author': 'Unknown',
                  'detail_url': 'https://softarchive.download/full/example-game'}
        page = b'''<main><a href="https://files.example.test/example-game.zip">Download ZIP</a>
        <a href="https://files.example.test/readme.html">Read me</a>
        <a href="http://files.example.test/insecure.rar">Insecure mirror</a></main>'''
        with patch('media_intake.download_connectors.http_get',
                   return_value=(page, detail['detail_url'])):
            files = resolve_softarchive_downloads(detail, allowed_formats={'.zip', '.rar'})

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['url'], 'https://files.example.test/example-game.zip')
        self.assertEqual(files[0]['source_key'], 'softarchive.download')
        self.assertEqual(files[0]['format'], '.zip')

    def test_registered_old_games_search_parses_game_detail_links(self):
        page = b'''<html><article><a href="https://oldgamesdownload.com/game/sample-title/">Sample Title</a></article>
        <a href="https://oldgamesdownload.com/game/unrelated/">Unrelated</a></html>'''
        with patch('media_intake.download_connectors.http_get',
                   return_value=(page, 'https://oldgamesdownload.com/?s=Sample+Title')) as get:
            results = search_site_downloads('Sample Title', 'oldgamesdownload.com')

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['detail_url'],
                         'https://oldgamesdownload.com/game/sample-title/')
        self.assertEqual(results[0]['source_key'], 'oldgamesdownload.com')
        self.assertIn('s=Sample+Title', get.call_args.args[0])

    def test_myabandonware_search_uses_its_post_form_and_resolves_file_assets(self):
        page = b'''<a href="/game/another-world-a/">Another World</a>'''
        with patch('media_intake.download_connectors._post_site_form',
                   return_value=(page, 'https://www.myabandonware.com/search/')) as post:
            results = search_site_downloads('Another World', 'myabandonware.com')

        post.assert_called_once_with('https://www.myabandonware.com/search/',
                                     {'searchValue': 'Another World'})
        self.assertEqual(results[0]['detail_url'],
                         'https://www.myabandonware.com/game/another-world-a/')
        detail = {'title': 'Another World', 'detail_url': results[0]['detail_url']}
        with patch('media_intake.download_connectors.http_get', return_value=(
                b'<a href="https://files.example.test/another-world.zip">Download</a>',
                detail['detail_url'])):
            files = resolve_site_downloads(
                detail, 'myabandonware.com', allowed_formats=formats_for_category('Games'))
        self.assertEqual(files[0]['url'], 'https://files.example.test/another-world.zip')
        self.assertEqual(files[0]['source_key'], 'myabandonware.com')

    def test_http_challenge_returns_a_manual_browser_search_link(self):
        error = urllib.error.HTTPError('https://ankergames.net/search?query=Sample',
                                       403, 'Forbidden', {}, None)
        with patch('media_intake.download_connectors.http_get', side_effect=error):
            results = search_site_downloads('Sample', 'ankergames.net', category='Games')

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['discovery_only'])
        self.assertIn('/search?query=Sample', results[0]['url'])
        self.assertIn('HTTP 403', results[0]['catalog_metadata']['note'])

    def test_xdcc_eu_search_returns_queueable_pack_configuration(self):
        page = b'''<table><tr><th>Network</th><th>Channel</th><th>Bot</th><th>Slot #</th>
        <th>Gets</th><th>Size</th><th>Name</th></tr><tr><td>RetroNet</td>
        <td><a href="irc://irc.example.net/retrogames">#retrogames</a></td>
        <td>ArchiveBot</td><td>#42</td><td>0x</td><td>12.5M</td>
        <td>Example_Game.zip</td></tr></table>'''
        with patch('media_intake.download_connectors.http_get',
                   return_value=(page, 'https://www.xdcc.eu/search.php?searchkey=Example+Game')):
            results = search_xdcc_eu(
                'Example Game', allowed_formats={'.zip'}, category='Games')

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['source_key'], 'xdcc.eu')
        self.assertEqual(results[0]['irc']['server'], 'irc.example.net')
        self.assertEqual(results[0]['irc']['channel'], '#retrogames')
        self.assertEqual(results[0]['irc']['pack'], '42')
        self.assertFalse(results[0]['irc']['tls'])
        self.assertEqual(results[0]['filesize'], int(12.5 * 1024 * 1024))

    def test_xdcc_eu_rejects_redirect_outside_its_host(self):
        with patch('media_intake.download_connectors.http_get', return_value=(
                b'<table></table>', 'https://other.example/search.php')):
            with self.assertRaisesRegex(ValueError, 'redirected outside xdcc.eu'):
                search_xdcc_eu('Sample')

    def test_google_cse_is_a_browser_search_not_a_file_result(self):
        result = search_google_cse_browser('Sample Game', 'Games')[0]
        self.assertTrue(result['discovery_only'])
        self.assertEqual(result['category'], 'Games')
        self.assertIn('006516753008110874046%3A1ugcdt3vo7z', result['url'])
        self.assertIn('gsc.q=Sample%20Game', result['url'])
        self.assertEqual(len(GENERAL_DOWNLOAD_ADAPTERS), 8)

    def test_gog_rev_search_is_browser_only_until_its_dynamic_results_can_be_read(self):
        item = search_registered_download_site(
            'Example Game', 'gog-rev.com', category='Games')[0]
        self.assertTrue(item['discovery_only'])
        self.assertEqual(item['format'], 'link')
        self.assertIn('gog-rev.com/search?q=Example+Game', item['url'])


if __name__ == '__main__':
    unittest.main()
