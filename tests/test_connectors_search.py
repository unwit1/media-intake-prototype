"""Each source connector against fixture replies served over local HTTP (no live sites).

The fixtures follow each site's published response format (apibay JSON, Nyaa RSS,
YTS and EZTV JSON APIs, Newznab RSS, the WordPress REST API, Z-Library's eAPI,
WorldCat Search API v2, AniDB's title dump and HTTP API) or a saved page layout
(1337x, RuTracker, EZTVx, MagzDB, Vimm's Lair). Live behaviour is recorded
separately in docs/SOURCES.md.
"""
import gzip
from pathlib import Path
import tempfile
import time
import unittest

from connector_site import FakeSite, Reply, fixture
from media_intake.connectors import CONNECTORS, search_one
from media_intake.connectors.base import SourceError
from media_intake.connectors.releases import info_hash_from_magnet

HASH_A = 'a' * 40


def configs(key, base, **extra):
    return {key: dict(base=base, enabled=True, access='direct', **extra)}


class TorrentSiteSearchTest(unittest.TestCase):
    def test_pirate_bay_api_rows_are_parsed_sorted_and_categorized(self):
        with FakeSite() as site:
            site.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            page = search_one('thepiratebay', 'night of the living dead', 'Videos', 1,
                              configs('thepiratebay', site.base))
            request = site.hits('/q.php')[0]
        self.assertEqual(request['query']['cat'], ['200'])
        self.assertEqual([row['seeders'] for row in page.rows], [120, 35, 9])
        first = page.rows[0]
        self.assertEqual(first['title'], 'Night of the Living Dead')
        self.assertEqual(first['year'], 1968)
        self.assertEqual(first['quality'], '1080p BluRay x264')
        self.assertEqual(first['format'], '.torrent')
        self.assertEqual(first['info_hash'], HASH_A)
        self.assertEqual(info_hash_from_magnet(first['url']), HASH_A)
        self.assertEqual(first['video_subtype'], 'movie')
        self.assertEqual(first['source_key'], 'thepiratebay')
        self.assertIn('120 seeders', first['display_source'])
        self.assertEqual(page.rows[2]['year'], 1990)
        self.assertFalse(page.has_more)

    def test_pirate_bay_no_results_placeholder_is_not_a_result(self):
        with FakeSite() as site:
            site.route('GET', '/q.php', Reply(fixture('tpb_empty.json'), content_type='application/json'))
            page = search_one('thepiratebay', 'zzzz', 'Books', 1, configs('thepiratebay', site.base))
        self.assertEqual(page.rows, [])

    def test_1337x_listing_paging_and_magnet_resolution(self):
        from media_intake.connectors import resolve_download
        with FakeSite() as site:
            site.route('GET', r'/category-search/.+/Movies/1/', Reply(fixture('1337x_search.html')))
            site.route('GET', r'/torrent/5550001/.*', Reply(fixture('1337x_detail.html')))
            page = search_one('1337x', 'night of the living dead', 'Videos', 1, configs('1337x', site.base))
            self.assertEqual(len(page.rows), 2)
            self.assertTrue(page.has_more)
            row = page.rows[0]
            self.assertEqual((row['seeders'], row['peers']), (98, 7))
            self.assertEqual(row['size'], int(1.5 * 1024 ** 3))
            self.assertEqual(row['resolve_connector'], '1337x')
            self.assertTrue(row['url'].startswith(site.base + '/torrent/5550001/'))
            resolved = resolve_download(row, configs('1337x', site.base))
        self.assertTrue(resolved['resolved_url'].startswith('magnet:?xt=urn:btih:AAAA'))
        self.assertNotIn('&amp;', resolved['resolved_url'])
        self.assertEqual(page.rows[1]['resolution'], '2160p')

    def test_browser_check_page_is_reported_as_blocked_with_jackett_advice(self):
        with FakeSite() as site:
            site.route('GET', r'/.*', Reply(fixture('challenge.html'), status=403))
            with self.assertRaises(SourceError) as caught:
                search_one('1337x', 'anything', 'Videos', 1, configs('1337x', site.base))
        self.assertEqual(caught.exception.kind, 'blocked')
        self.assertIn('Jackett', str(caught.exception))

    def test_blocked_site_falls_back_to_the_users_jackett_indexer(self):
        torznab = (b'<?xml version="1.0"?><rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>'
                   b'<item><title>Night of the Living Dead 1968 1080p BluRay</title>'
                   b'<link>magnet:?xt=urn:btih:' + b'A' * 40 + b'</link><size>100</size>'
                   b'<torznab:attr name="seeders" value="5"/><torznab:attr name="infohash" value="' + b'a' * 40 +
                   b'"/></item></channel></rss>')
        with FakeSite() as site, FakeSite() as jackett:
            site.route('GET', r'/.*', Reply(fixture('challenge.html'), status=503))
            jackett.route('GET', r'/api/v2.0/indexers/1337x/results/torznab/api',
                          Reply(torznab, content_type='application/rss+xml'))
            options = configs('1337x', site.base)
            options['1337x']['access'] = 'auto'
            options['jackett'] = {'jackett_url': jackett.base, 'jackett_api_key': 'key'}
            page = search_one('1337x', 'night of the living dead', 'Videos', 1, options)
        self.assertEqual(page.via, 'jackett')
        self.assertIn('used Jackett', page.notice)
        self.assertEqual(page.rows[0]['provider'], '1337x (via Jackett)')
        self.assertEqual(page.rows[0]['source_key'], '1337x')

    def test_rutracker_signs_in_reads_cp1251_and_filters_forums_by_category(self):
        def login(request):
            self.assertIn(b'login_username=reader', request['body'])
            return Reply(b'', 302, {'Location': '/forum/index.php',
                                    'Set-Cookie': ['bb_session=0-777-abc; path=/forum/', 'bb_ssl=1; path=/']})
        with FakeSite() as site:
            site.route('POST', '/forum/login.php', login)
            site.route('GET', '/forum/index.php', Reply('<html>index</html>'))
            site.route('GET', '/forum/tracker.php', Reply(fixture('rutracker_tracker.html'),
                                                          content_type='text/html; charset=windows-1251'))
            site.route('GET', '/v1/get_tor_hash', Reply(fixture('rutracker_hash.json'),
                                                        content_type='application/json'))
            options = configs('rutracker', site.base, username='reader', password='secret', api_base=site.base)
            page = search_one('rutracker', 'night of the living dead', 'Videos', 1, options)
            self.assertIn('bb_session=0-777-abc', site.hits('/forum/tracker.php')[0]['headers']['Cookie'])
            self.assertEqual(len(page.rows), 1)  # the audiobook forum row is not a video
            row = page.rows[0]
            self.assertIn('Night of the Living Dead', row['release_title'])
            self.assertEqual((row['size'], row['seeders'], row['peers']), (7516192768, 40, 2))
            self.assertTrue(page.has_more)
            from media_intake.connectors import resolve_download
            resolved = resolve_download(row, options)
        self.assertEqual(info_hash_from_magnet(resolved['resolved_url']), '4' * 40)

    def test_rutracker_without_an_account_explains_what_is_needed(self):
        with self.assertRaises(SourceError) as caught:
            search_one('rutracker', 'x', 'Videos', 1, configs('rutracker', 'https://rutracker.org'))
        self.assertEqual(caught.exception.kind, 'config')
        self.assertIn('Settings → Sources', str(caught.exception))

    def test_nyaa_rss_carries_hash_seeders_size_and_paging(self):
        with FakeSite() as site:
            site.route('GET', '/', Reply(fixture('nyaa_rss.xml'), content_type='application/rss+xml',
                                         gzip_body=True))
            page = search_one('nyaa', 'big buck bunny', 'Anime', 2, configs('nyaa', site.base))
            query = site.hits('/')[0]['query']
        self.assertEqual((query['page'], query['c'], query['p']), (['rss'], ['1_0'], ['2']))
        self.assertEqual(len(page.rows), 2)
        row = page.rows[0]
        self.assertEqual(row['title'], 'Big Buck Bunny')
        self.assertEqual(row['episode'], 1)
        self.assertEqual(row['resolution'], '1080p')
        self.assertEqual(row['release_group'], 'OpenFansubs')
        self.assertEqual(row['size'], int(310.4 * 1024 ** 2))
        self.assertTrue(row['trusted'])
        self.assertEqual(row['kind'], 'anime')
        self.assertFalse(page.has_more)

    def test_yts_lists_each_quality_and_pages_by_movie_count(self):
        with FakeSite() as site:
            site.route('GET', '/api/v2/list_movies.json', Reply(fixture('yts_list.json'),
                                                                content_type='application/json'))
            page = search_one('yts', 'night of the living dead', 'Videos', 1, configs('yts', site.base))
        self.assertEqual([row['resolution'] for row in page.rows], ['1080p', '720p'])
        self.assertTrue(all(row['title'] == 'Night of the Living Dead' and row['year'] == 1968
                            for row in page.rows))
        self.assertEqual(page.rows[0]['imdb_id'], 'tt0063350')
        self.assertTrue(page.has_more)  # 23 movies, 20 per page
        self.assertEqual(search_one('yts', 'x', 'Books', 1, configs('yts', 'https://yts.mx')).rows, [])

    def test_eztvx_searches_by_name_and_by_imdb_id(self):
        with FakeSite() as site:
            site.route('GET', r'/search/.*', Reply(fixture('eztvx_search.html')))
            site.route('GET', '/api/get-torrents', Reply(fixture('eztvx_api.json'),
                                                         content_type='application/json'))
            by_name = search_one('eztvx', 'The Lucy Show', 'Videos', 1, configs('eztvx', site.base))
            by_id = search_one('eztvx', 'tt0063350', 'Videos', 1, configs('eztvx', site.base))
            self.assertEqual(site.hits('/api/get-torrents')[0]['query']['imdb_id'], ['0063350'])
        self.assertEqual(len(by_name.rows), 1)
        self.assertEqual((by_name.rows[0]['season'], by_name.rows[0]['episode']), (1, 1))
        self.assertEqual(by_name.rows[0]['seeders'], 15)
        self.assertEqual(by_name.rows[0]['info_hash'], '1' * 40)
        self.assertEqual(by_name.rows[0]['video_subtype'], 'tv_series')
        self.assertEqual([row['episode'] for row in by_id.rows], [1, 2])

    def test_fitgirl_wordpress_api_keeps_only_posts_with_releases(self):
        with FakeSite() as site:
            site.route('GET', '/wp-json/wp/v2/posts', Reply(fixture('fitgirl_posts.json'),
                                                            content_type='application/json',
                                                            headers={'X-WP-TotalPages': '3'}))
            page = search_one('fitgirl', 'freedoom', 'Games', 1, configs('fitgirl', site.base))
        self.assertEqual(len(page.rows), 1)
        row = page.rows[0]
        self.assertEqual(row['title'], 'Freedoom')
        self.assertEqual(row['info_hash'], '3' * 40)
        self.assertEqual(row['size'], int(31.2 * 1024 ** 2))
        self.assertEqual(row['original_size'], '58 MB')
        self.assertIn('Shooter', row['genres'])
        self.assertTrue(page.has_more)

    def test_fitgirl_past_the_last_page_is_empty_not_an_error(self):
        with FakeSite() as site:
            site.route('GET', '/wp-json/wp/v2/posts', Reply(b'{"code":"rest_post_invalid_page_number"}', 400,
                                                            content_type='application/json'))
            page = search_one('fitgirl', 'freedoom', 'Games', 4, configs('fitgirl', site.base))
        self.assertEqual(page.rows, [])


class UsenetSearchTest(unittest.TestCase):
    def test_newznab_rows_hide_the_api_key_and_report_total(self):
        with FakeSite() as site:
            site.route('GET', '/api', Reply(fixture('newznab_search.xml'), content_type='application/rss+xml'))
            page = search_one('nzbgeek', 'night of the living dead', 'Videos', 1,
                              configs('nzbgeek', site.base, api_key='KEY'))
            query = site.hits('/api')[0]['query']
        self.assertEqual((query['apikey'], query['cat'], query['offset']), (['KEY'], ['2000,5000'], ['0']))
        self.assertTrue(page.has_more)  # total 101
        row = page.rows[0]
        self.assertEqual((row['format'], row['grabs'], row['size']), ('.nzb', 321, 1610612736))
        self.assertEqual(row['imdb_id'], 'tt0063350')
        self.assertEqual(row['nzb_guid'], 'abc123')
        self.assertNotIn('KEY', row['source'] + row['display_source'])
        self.assertEqual(row['details_url'], 'https://indexer.example/details/abc123')

    def test_newznab_error_codes_become_actionable_messages(self):
        for name, kind, words in (('newznab_error.xml', 'auth', 'API key'),
                                  ('newznab_limit.xml', 'limit', 'limit')):
            with self.subTest(name=name), FakeSite() as site:
                site.route('GET', '/api', Reply(fixture(name), content_type='application/xml'))
                with self.assertRaises(SourceError) as caught:
                    search_one('drunkenslug', 'x', 'Books', 1, configs('drunkenslug', site.base, api_key='bad'))
                self.assertEqual(caught.exception.kind, kind)
                self.assertIn(words, str(caught.exception))
                self.assertTrue(str(caught.exception).startswith('DrunkenSlug: '))

    def test_missing_api_key_is_a_setup_message_not_a_request(self):
        with FakeSite() as site:
            with self.assertRaises(SourceError) as caught:
                search_one('nzbgeek', 'x', 'Videos', 1, configs('nzbgeek', site.base))
            self.assertEqual(site.requests, [])
        self.assertEqual(caught.exception.kind, 'config')


class LibrarySearchTest(unittest.TestCase):
    def test_zlibrary_signs_in_once_searches_and_drops_unsupported_formats(self):
        with FakeSite() as site:
            site.route('POST', '/eapi/user/login', Reply(fixture('zlib_login.json'), content_type='application/json'))
            site.route('POST', '/eapi/book/search', Reply(fixture('zlib_search.json'),
                                                          content_type='application/json'))
            options = configs('z-library', site.base, username='reader@example.invalid', password='pw')
            page = search_one('z-library', 'pride and prejudice', 'Books', 1, options)
            search = site.hits('/eapi/book/search')[0]
        self.assertIn('remix_userkey=k3y', search['headers']['Cookie'])
        self.assertIn(b'message=pride+and+prejudice', search['body'])
        self.assertEqual([row['format'] for row in page.rows], ['.epub', '.pdf'])
        self.assertEqual(page.rows[0]['author'], 'Jane Austen')
        self.assertEqual(page.rows[0]['zlib_hash'], 'a1b2c3')
        self.assertTrue(page.has_more)

    def test_zlibrary_needs_a_personal_domain(self):
        with self.assertRaises(SourceError) as caught:
            search_one('z-library', 'x', 'Books', 1, {'z-library': {'base': ''}})
        self.assertEqual(caught.exception.kind, 'config')

    def test_magzdb_lists_issues_of_matching_magazines(self):
        with FakeSite() as site:
            site.route('GET', '/makelist', Reply(fixture('magzdb_search.html')))
            site.route('GET', '/j/501', Reply(fixture('magzdb_journal.html')))
            page = search_one('magzdb', 'popular science', 'Books', 1, configs('magzdb', site.base))
        self.assertEqual([row['title'] for row in page.rows],
                         ['Popular Science Monthly 1872 No. 1', 'Popular Science Monthly 1872 No. 2'])
        self.assertEqual(page.rows[0]['series'], 'Popular Science Monthly')

    def test_vimm_lists_titles_with_platform_and_region(self):
        with FakeSite() as site:
            site.route('GET', '/vault/', Reply(fixture('vimm_list.html')))
            page = search_one('vimm', 'tetromino', 'Games', 1, configs('vimm', site.base))
        self.assertEqual([(row['title'], row['platform'], row['region']) for row in page.rows],
                         [('Homebrew Tetromino', 'NES', 'USA'), ('Homebrew Tetromino DX', 'GBC', 'Europe')])
        self.assertTrue(all(row['resolve_connector'] == 'vimm' for row in page.rows))


class MetadataSearchTest(unittest.TestCase):
    def test_anidb_matches_titles_from_a_cached_dump_and_adds_details(self):
        titles = gzip.compress(fixture('anidb_titles.xml'))
        with tempfile.TemporaryDirectory() as cache, FakeSite() as site:
            site.route('GET', '/api/anime-titles.xml.gz', Reply(titles, content_type='application/gzip'))
            site.route('GET', '/httpapi', Reply(fixture('anidb_anime.xml'), content_type='text/xml'))
            options = {'anidb': {'cache_dir': cache, 'client': 'mediaintake', 'dump_url':
                                 site.base + '/api/anime-titles.xml.gz', 'api_base': site.base}}
            page = search_one('anidb', 'Cowboy Bebop', 'Anime', 1, options)
            again = search_one('anidb', 'crest of the stars', 'Anime', 1, options)
            self.assertEqual(len(site.hits('/api/anime-titles.xml.gz')), 1)  # dump fetched once, then cached
            self.assertTrue((Path(cache) / 'anidb' / '23.xml').exists())
        self.assertEqual(page.rows[0]['title'], 'Cowboy Bebop')
        self.assertEqual(page.rows[0]['external_ids'], [{'provider': 'anidb', 'external_id': '23'}])
        self.assertEqual(page.rows[0]['release_date'], '1998-04-03')
        self.assertEqual(page.rows[0]['video_subtype'], 'anime_series')
        self.assertFalse(page.rows[0]['downloadable'])
        self.assertEqual(page.rows[1]['title'], 'Cowboy Bebop: The Movie')
        self.assertEqual(again.rows[0]['title'], 'Crest of the Stars')

    def test_worldcat_uses_client_credentials_and_reads_bib_records(self):
        with FakeSite() as site:
            site.route('POST', '/token', Reply(fixture('worldcat_token.json'), content_type='application/json'))
            site.route('GET', '/bibs', Reply(fixture('worldcat_bibs.json'), content_type='application/json'))
            options = {'worldcat': {'client_id': 'id', 'client_secret': 'secret', 'token_url': site.base + '/token',
                                    'search_url': site.base + '/bibs'}}
            page = search_one('worldcat', 'pride and prejudice', 'Books', 1, options)
            self.assertEqual(site.hits('/bibs')[0]['headers']['Authorization'], 'Bearer tok123')
        self.assertEqual(page.rows[0]['title'], 'Pride and prejudice')
        self.assertEqual(page.rows[0]['author'], 'Jane Austen')
        self.assertEqual(page.rows[0]['isbn'], '9780141439518')
        self.assertEqual(page.rows[1]['author'], 'BBC')
        self.assertEqual(page.rows[1]['release_date'], '1995')
        self.assertTrue(all(row['source_role'] == 'metadata' for row in page.rows))

    def test_worldcat_and_aio_without_an_api_open_the_browser_instead(self):
        worldcat = search_one('worldcat', 'pride and prejudice', 'Books', 1, {})
        aio = search_one('aiosearch', 'pride and prejudice', 'Books', 1, {})
        for page, host in ((worldcat, 'search.worldcat.org'), (aio, 'www.aiosearch.com')):
            self.assertEqual(page.via, 'browser')
            self.assertEqual(len(page.rows), 1)
            self.assertTrue(page.rows[0]['discovery_only'])
            self.assertIn(host, page.rows[0]['url'])
            self.assertIn('pride%20and%20prejudice', page.rows[0]['url'])


class FailureMessageTest(unittest.TestCase):
    def test_common_failures_read_as_plain_language(self):
        cases = [
            ('unreachable', Reply(b'oops', 502), 'having problems'),
            ('limit', Reply(b'slow down', 429), 'too many requests'),
            ('unreachable', Reply(b'gone', 404), 'moved'),
            ('invalid', Reply(b'<html>not json</html>', content_type='text/html'), 'could not read'),
        ]
        for kind, reply, words in cases:
            with self.subTest(words=words), FakeSite() as site:
                site.route('GET', '/q.php', reply)
                with self.assertRaises(SourceError) as caught:
                    search_one('thepiratebay', 'x', 'Videos', 1, configs('thepiratebay', site.base))
                self.assertEqual(caught.exception.kind, kind)
                self.assertIn(words, str(caught.exception))

    def test_unknown_host_and_refused_connection(self):
        import socket
        import urllib.error
        from media_intake.connectors.base import network_error
        error = network_error('YTS', urllib.error.URLError(socket.gaierror(-2, 'Name or service not known')))
        self.assertEqual(error.kind, 'unreachable')
        self.assertIn('mirror', str(error))
        timeout = network_error('YTS', urllib.error.URLError(socket.timeout('timed out')))
        self.assertIn('did not answer in time', str(timeout))
        site = FakeSite()
        base = site.base
        site.server.server_close()  # nothing listens there now
        with self.assertRaises(SourceError) as caught:
            search_one('yts', 'x', 'Videos', 1, configs('yts', base))
        self.assertEqual(caught.exception.kind, 'unreachable')

    def test_insecure_internet_addresses_are_refused(self):
        with self.assertRaises(SourceError) as caught:
            search_one('yts', 'x', 'Videos', 1, configs('yts', 'http://yts.example.com'))
        self.assertEqual(caught.exception.kind, 'config')

    def test_every_connector_has_a_searcher_and_plain_summary(self):
        from media_intake.connectors import _SEARCHERS
        self.assertEqual(set(_SEARCHERS), set(CONNECTORS))
        self.assertEqual(len(CONNECTORS), 15)
        for spec in CONNECTORS.values():
            self.assertTrue(spec.summary and spec.categories)


class SlowSiteTest(unittest.TestCase):
    def test_slow_connector_times_out_without_holding_back_the_others(self):
        from media_intake.connectors import search_sources
        from media_intake.rights_evidence import ensure_rights_evidence
        import json
        with tempfile.TemporaryDirectory() as folder, FakeSite() as fast, FakeSite() as slow:
            path = ensure_rights_evidence(Path(folder) / 'rights-evidence.json')
            document = json.loads(path.read_text(encoding='utf-8'))
            for key in ('thepiratebay', 'yts'):
                document['sources'][key]['approved_for_download'] = True
            path.write_text(json.dumps(document), encoding='utf-8')
            fast.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            slow.route('GET', '/api/v2/list_movies.json', lambda request: (time.sleep(3), None)[1])
            options = {'rights_evidence_path': str(path), 'source_configs': {
                **configs('thepiratebay', fast.base), **configs('yts', slow.base)}}
            started = time.monotonic()
            found = search_sources('night of the living dead', 'Videos', options, timeout=1)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(found['rows']), 3)
        self.assertEqual(len(found['errors']), 1)
        self.assertIn('YTS: the site took longer', found['errors'][0])


if __name__ == '__main__':
    unittest.main()
