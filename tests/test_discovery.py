import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from media_intake.discovery import (CATEGORIES, DOWNLOAD_CONNECTORS,
                                    METADATA_CONNECTORS, _expand_book_series_results,
                                    _promote_matching_series_results,
                                    _foss_download_files,
                                    _irc_files,
                                    _registered_download_site_files,
                                    _merge_series_member, _merge_same_title,
                                    _query_wikidata,
                                    connected_download_sources,
                                    get_download_priority, save_download_priority,
                                    search_media_category)
from media_intake.core import connect
from media_intake.rights_evidence import ensure_rights_evidence


class CategoryDiscoveryTest(unittest.TestCase):
    def test_irc_downloads_keep_software_kind_and_software_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_rights_evidence(Path(tmp) / 'rights-evidence.json')
            document = json.loads(path.read_text(encoding='utf-8'))
            document['sources']['irc']['approved_for_download'] = True
            path.write_text(json.dumps(document), encoding='utf-8')
            options = {
                'rights_evidence_path': str(path),
                'irc_sources': [{'name': 'Builds', 'server': 'irc.example.test',
                                 'channel': '#software', 'nick': 'Reader',
                                 'bot': 'IndexBot', 'categories': ['Software']}],
            }
            with patch('media_intake.irc.search_configured_sources', return_value=([{
                    'title': 'Example Tool', 'format': '.zip',
                    'url': 'https://files.example/tool.zip'}], [])) as search:
                results, errors = _irc_files('Example Tool', 'Software', options)

        self.assertEqual(errors, [])
        self.assertEqual(results[0]['kind'], 'software')
        self.assertIn('.zip', search.call_args.args[3])
        self.assertNotIn('.epub', search.call_args.args[3])

    def test_each_configured_irc_server_has_an_independent_download_priority(self):
        sources = [
            {'name': 'Primary', 'server': 'irc.primary.test', 'channel': '#books',
             'nick': 'reader', 'bot': 'SearchBot', 'categories': ['Books']},
            {'name': 'Backup', 'server': 'irc.backup.test', 'channel': '#ebooks',
             'nick': 'reader', 'bot': 'SearchBot', 'categories': ['Books']},
            {'name': 'Comics only', 'server': 'irc.comics.test', 'channel': '#comics',
             'nick': 'reader', 'bot': 'SearchBot', 'categories': ['Comics']},
        ]
        options = {'irc_sources': sources}
        labels = [source for source in connected_download_sources('Books', options)
                  if source.startswith('IRC: ')]
        self.assertEqual(labels, [
            'IRC: Primary (irc.primary.test #books)',
            'IRC: Backup (irc.backup.test #ebooks)',
        ])

        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                available = connected_download_sources('Books', options)
                requested = [source for source in available if source not in labels]
                requested += list(reversed(labels))
                save_download_priority(db, 'Books', requested, options)
                saved = get_download_priority(db, 'Books', options)
        self.assertEqual(saved[:len(labels)], list(reversed(labels)))
        self.assertEqual(saved[len(labels):], [source for source in requested if source not in labels])

    def test_general_download_connectors_are_available_in_every_media_priority(self):
        for category in CATEGORIES:
            with self.subTest(category=category):
                if category == 'Software':
                    self.assertEqual(DOWNLOAD_CONNECTORS[category],
                                     ('IRC', 'GitHub (FOSS)', 'SourceForge', 'MediaFireTrend',
                                      'XDCC.eu', 'WarezOmen', 'The Pirate Bay', '1337x', 'RuTracker',
                                      'NZBGeek', 'DrunkenSlug'))
                    continue
                self.assertEqual(DOWNLOAD_CONNECTORS[category][0], 'IRC')
                self.assertIn('Internet Archive', DOWNLOAD_CONNECTORS[category])
                self.assertIn('Meawfy', DOWNLOAD_CONNECTORS[category])
                self.assertIn('SoftArchive', DOWNLOAD_CONNECTORS[category])
                self.assertNotIn('Internet Archive', METADATA_CONNECTORS.get(category, ()))

    def test_game_download_sites_are_registered_only_for_games(self):
        for source in ('MyAbandonware', 'OldGamesDownload', 'AbandonwareGames',
                       'GOG Revived', 'AnkerGames'):
            with self.subTest(source=source):
                self.assertIn(source, DOWNLOAD_CONNECTORS['Games'])
                for category in CATEGORIES:
                    if category != 'Games':
                        self.assertNotIn(source, DOWNLOAD_CONNECTORS[category])

    def test_new_general_sites_are_yes_gated_before_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_rights_evidence(Path(tmp) / 'rights-evidence.json')
            options = {'rights_evidence_path': str(path)}
            with patch('media_intake.discovery.search_registered_download_site') as searcher:
                results, errors = _registered_download_site_files(
                    'Example', 'Games', options)

        self.assertEqual(results, [])
        self.assertEqual(errors, [])
        searcher.assert_not_called()

    def test_approved_new_game_source_search_resolves_and_keeps_rights_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_rights_evidence(Path(tmp) / 'rights-evidence.json')
            document = json.loads(path.read_text(encoding='utf-8'))
            document['sources']['oldgamesdownload.com']['approved_for_download'] = True
            path.write_text(json.dumps(document), encoding='utf-8')
            options = {'rights_evidence_path': str(path)}
            hit = {'title': 'Sample Game', 'detail_url':
                   'https://oldgamesdownload.com/game/sample-game/',
                   'url': 'https://oldgamesdownload.com/game/sample-game/',
                   'format': 'link', 'provider': 'OldGamesDownload',
                   'source_key': 'oldgamesdownload.com', 'discovery_only': True}
            file_item = {'title': 'Sample Game', 'url':
                         'https://files.example.test/sample-game.zip',
                         'format': '.zip', 'source': hit['detail_url'],
                         'source_key': 'oldgamesdownload.com'}
            with patch('media_intake.discovery.search_registered_download_site',
                       return_value=[hit]) as searcher, \
                    patch('media_intake.discovery.resolve_site_downloads',
                          return_value=[dict(file_item)]):
                results, errors = _registered_download_site_files(
                    'Sample Game', 'Games', options)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['downloadable'])
        self.assertEqual(results[0]['rights_evidence_path'], str(path))
        self.assertEqual(results[0]['category'], 'Games')
        self.assertEqual(searcher.call_args.args[1], 'oldgamesdownload.com')

    def test_anime_download_sites_are_registered_only_for_anime(self):
        for source in ('Anime Tosho', 'Tokyo Insider', 'AnimeOut', 'NoobSubs', 'Chauthanh'):
            with self.subTest(source=source):
                self.assertIn(source, DOWNLOAD_CONNECTORS['Anime'])
                for category in CATEGORIES:
                    if category != 'Anime':
                        self.assertNotIn(source, DOWNLOAD_CONNECTORS[category])

    def test_google_books_is_metadata_only_not_a_download_connector(self):
        self.assertIn('Google Books', METADATA_CONNECTORS['Books'])
        self.assertNotIn('Google Books', DOWNLOAD_CONNECTORS['Books'])

    def test_foss_software_sources_are_split_between_metadata_and_download(self):
        self.assertIn('FOSShub', METADATA_CONNECTORS['Software'])
        self.assertEqual(DOWNLOAD_CONNECTORS['Software'][:3],
                         ('IRC', 'GitHub (FOSS)', 'SourceForge'))
        self.assertIn('MediaFireTrend', DOWNLOAD_CONNECTORS['Software'])
        self.assertIn('XDCC.eu', DOWNLOAD_CONNECTORS['Software'])
        self.assertIn('WarezOmen', DOWNLOAD_CONNECTORS['Software'])
        self.assertNotIn('FOSShub', DOWNLOAD_CONNECTORS['Software'])

    def test_foss_download_source_requires_yes_then_attaches_local_evidence_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ensure_rights_evidence(Path(tmp) / 'rights-evidence.json')
            options = {'rights_evidence_path': str(path)}
            searcher = Mock(return_value=[{
                'title': 'Example Tool', 'author': 'Example', 'url':
                'https://github.com/example/tool/releases/download/v1/tool.zip',
                'format': '.zip', 'source': 'https://github.com/example/tool',
                'source_key': 'github_foss',
            }])
            blocked, _ = _foss_download_files(
                'Example Tool', options, 'github_foss', 'GitHub (FOSS)', searcher)
            self.assertEqual(blocked, [])
            searcher.assert_not_called()

            document = json.loads(path.read_text(encoding='utf-8'))
            document['sources']['github_foss']['approved_for_download'] = True
            path.write_text(json.dumps(document), encoding='utf-8')
            allowed, errors = _foss_download_files(
                'Example Tool', options, 'github_foss', 'GitHub (FOSS)', searcher)

        self.assertEqual(errors, [])
        self.assertEqual(len(allowed), 1)
        self.assertTrue(allowed[0]['downloadable'])
        self.assertEqual(allowed[0]['rights_evidence_path'], str(path))

    def test_book_search_merges_catalog_title_variants_by_shared_isbn(self):
        merged = _merge_same_title([
            {'provider': 'Goodreads', 'kind': 'book', 'title': 'Example Novel',
             'author': 'Example Author', 'source_url': 'https://goodreads.com/book/1',
             'catalog_metadata': {'isbns': ['0-306-40615-2']}},
            {'provider': 'Google Books', 'kind': 'book',
             'title': 'Example Novel: Revised Edition', 'author': 'Example Author',
             'source_url': 'https://books.google.com/books?id=2',
             'catalog_metadata': {'isbns': ['9780306406157']}},
        ], 'Books', ())

        self.assertEqual(len(merged), 1)
        self.assertEqual({item['provider'] for item in merged[0]['metadata_variants']},
                         {'Goodreads', 'Google Books'})

    def test_book_search_does_not_merge_isbn_conflicts_with_different_authors(self):
        merged = _merge_same_title([
            {'provider': 'Open Library', 'kind': 'book', 'title': 'Example Novel',
             'author': 'First Author', 'source_url': 'https://openlibrary.org/works/OL1W',
             'catalog_metadata': {'isbns': ['9780306406157']}},
            {'provider': 'Google Books', 'kind': 'book', 'title': 'Different Novel',
             'author': 'Second Author', 'source_url': 'https://books.google.com/books?id=2',
             'catalog_metadata': {'isbns': ['9780306406157']}},
        ], 'Books', ())

        self.assertEqual(len(merged), 2)

    def test_book_search_collapses_common_format_title_without_isbn(self):
        merged = _merge_same_title([
            {'provider': 'Goodreads', 'kind': 'book', 'title': 'Example Novel',
             'author': 'Example Author',
             'source_url': 'https://www.goodreads.com/book/show/1'},
            {'provider': 'Open Library', 'kind': 'book',
             'title': 'Example Novel (Paperback)', 'author': 'Example Author',
             'source_url': 'https://openlibrary.org/works/OL1W'},
        ], 'Books', ())

        self.assertEqual(len(merged), 1)
        self.assertEqual({item['provider'] for item in merged[0]['metadata_variants']},
                         {'Goodreads', 'Open Library'})

    def test_book_search_keeps_common_format_title_when_authors_conflict(self):
        merged = _merge_same_title([
            {'provider': 'Goodreads', 'kind': 'book', 'title': 'Example Novel',
             'author': 'First Author',
             'source_url': 'https://www.goodreads.com/book/show/1'},
            {'provider': 'Open Library', 'kind': 'book',
             'title': 'Example Novel (Paperback)', 'author': 'Second Author',
             'source_url': 'https://openlibrary.org/works/OL1W'},
        ], 'Books', ())

        self.assertEqual(len(merged), 2)

    def test_series_search_holds_packaging_titles_out_of_the_book_rows(self):
        series = {'provider': 'Goodreads', 'kind': 'series',
                  'title': 'The Stormlight Archive', 'books': [
                      {'title': 'The Stormlight Archive Part One',
                       'author': 'Brandon Sanderson',
                       'source_url': 'https://www.goodreads.com/book/show/9001'},
                      {'title': 'The Way of Kings', 'author': 'Brandon Sanderson',
                       'source_url': 'https://www.goodreads.com/book/show/7235533'},
                  ]}
        rows, bundle = _expand_book_series_results([series], 'Stormlight Archive')

        row_titles = [row['title'] for row in rows]
        self.assertEqual(row_titles[0], 'The Stormlight Archive')
        self.assertIn('The Way of Kings', row_titles)
        self.assertNotIn('The Stormlight Archive Part One', row_titles)
        self.assertEqual(len(bundle['review_members']), 1)
        self.assertEqual(bundle['review_members'][0]['title'],
                         'The Stormlight Archive Part One')

    def test_anime_movie_matches_merge_with_tmdb_movie_metadata(self):
        rows = [
            {'provider': 'AniList', 'kind': 'anime', 'title': 'Example Film',
             'video_subtype': 'anime_movie', 'release_date': '2025-04-03',
             'source_url': 'https://anilist.co/anime/43'},
            {'provider': 'TMDB', 'kind': 'anime', 'title': 'Example Film',
             'video_subtype': 'movie', 'release_date': '2025-07-01',
             'source_url': 'https://www.themoviedb.org/movie/99'},
        ]
        merged = _merge_same_title(rows, 'Anime', ())

        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]['metadata_variants']), 2)

    def test_wikidata_video_search_classifies_short_and_independent_animation(self):
        candidates = [
            {'provider': 'Wikidata', 'kind': 'entity', 'qid': 'Q1',
             'title': 'Short Film', 'description': 'American short film',
             'source_url': 'https://www.wikidata.org/entity/Q1'},
            {'provider': 'Wikidata', 'kind': 'entity', 'qid': 'Q2',
             'title': 'OAD', 'description': 'Japanese original animation DVD',
             'source_url': 'https://www.wikidata.org/entity/Q2'},
            {'provider': 'Wikidata', 'kind': 'entity', 'qid': 'Q3',
             'title': 'Independent Animation',
             'description': 'Independent animation short',
             'source_url': 'https://www.wikidata.org/entity/Q3'},
        ]
        with patch('media_intake.discovery.search_wikidata', return_value=candidates):
            results = _query_wikidata('short animation', 'Videos')

        self.assertEqual({row['title']: row['video_subtype'] for row in results}, {
            'Short Film': 'short_film',
            'OAD': 'anime_oad',
            'Independent Animation': 'independent_animation',
        })

    def test_wikidata_video_search_classifies_series_subtypes_without_losing_anime(self):
        candidates = [
            {'provider': 'Wikidata', 'kind': 'entity', 'qid': 'Q4',
             'title': 'Limited Show', 'description': 'British television miniseries'},
            {'provider': 'Wikidata', 'kind': 'entity', 'qid': 'Q5',
             'title': 'Web Show', 'description': 'Canadian web series'},
            {'provider': 'Wikidata', 'kind': 'entity', 'qid': 'Q6',
             'title': 'Anime Show', 'description': 'Japanese animated television series'},
        ]
        with patch('media_intake.discovery.search_wikidata', return_value=candidates):
            results = _query_wikidata('series', 'Videos')

        self.assertEqual({row['title']: (row['entity_type'], row['video_subtype'])
                          for row in results}, {
            'Limited Show': ('series', 'miniseries'),
            'Web Show': ('series', 'web_series'),
            'Anime Show': ('series', 'anime'),
        })

    def test_series_member_merge_keeps_provider_date_conflicts_and_evidence(self):
        members = []
        _merge_series_member(members, {
            'title': 'Book One', 'author': 'Example Author', 'provider': 'Goodreads',
            'source_url': 'https://www.goodreads.com/book/show/1',
            'release_date': '2020-01-01', 'series_index': 1,
            'catalog_metadata': {'catalog_id': 'gr:1'},
        }, 'Goodreads')
        _merge_series_member(members, {
            'title': 'Book One', 'author': 'Example Author', 'provider': 'Open Library',
            'source_url': 'https://openlibrary.org/works/OL1W',
            'release_date': '2021-02-03', 'series_index': 1,
            'catalog_metadata': {'catalog_id': 'OL1W'},
        }, 'Open Library')

        self.assertEqual(len(members), 1)
        self.assertEqual([item['provider'] for item in members[0]['metadata_variants']],
                         ['Goodreads', 'Open Library'])
        self.assertEqual(
            [(item['provider'], item['release_date'])
             for item in members[0]['metadata_variants']],
            [('Goodreads', '2020-01-01'), ('Open Library', '2021-02-03')])
        self.assertEqual(members[0]['metadata_conflicts'][0]['field'], 'release_date')

    def test_series_member_merge_collapses_same_isbn_with_catalog_title_variant(self):
        members = []
        _merge_series_member(members, {
            'title': 'Example Novel', 'author': 'Example Author',
            'provider': 'Goodreads',
            'source_url': 'https://www.goodreads.com/book/show/1',
            'catalog_metadata': {'catalog_id': 'gr:1', 'isbns': ['9780306406157']},
        }, 'Goodreads')
        _merge_series_member(members, {
            'title': 'Example Novel: Revised Edition', 'author': 'Example Author',
            'provider': 'Open Library',
            'source_url': 'https://openlibrary.org/works/OL1W',
            'catalog_metadata': {'catalog_id': 'OL1W', 'isbns': ['0-306-40615-2']},
        }, 'Open Library')

        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]['catalog_ids'], {
            'Goodreads': ['gr:1'], 'Open Library': ['OL1W']})
        self.assertEqual([item['title'] for item in members[0]['metadata_variants']],
                         ['Example Novel', 'Example Novel: Revised Edition'])

    def test_series_member_merge_collapses_format_variant_without_isbn(self):
        members = []
        _merge_series_member(members, {
            'title': 'Example Novel', 'author': 'Example Author',
            'provider': 'Goodreads', 'series_index': 1,
            'source_url': 'https://www.goodreads.com/book/show/1',
        }, 'Goodreads')
        _merge_series_member(members, {
            'title': 'Example Novel (Paperback)', 'author': 'Example Author',
            'provider': 'Open Library', 'series_index': 1,
            'source_url': 'https://openlibrary.org/works/OL1W',
        }, 'Open Library')

        self.assertEqual(len(members), 1)
        self.assertEqual([item['title'] for item in members[0]['metadata_variants']],
                         ['Example Novel', 'Example Novel (Paperback)'])

    def test_series_member_position_title_conflict_is_held_for_review(self):
        first = {'provider': 'Goodreads', 'kind': 'series', 'title': 'Example Series',
                 'source_url': 'https://www.goodreads.com/series/1',
                 'books': [{'title': 'Example Novel', 'author': 'Example Author',
                            'series_index': 1,
                            'source_url': 'https://www.goodreads.com/book/show/1'}]}
        second = {'provider': 'Open Library', 'kind': 'series', 'title': 'Example Series',
                  'source_url': 'https://openlibrary.org/series/OL1S',
                  'books': [{'title': 'The Other Novel', 'author': 'Example Author',
                             'series_index': '1.0',
                             'source_url': 'https://openlibrary.org/works/OL2W'}]}

        rows, bundle = _expand_book_series_results([first, second], 'Example Series')

        self.assertEqual([row['title'] for row in rows],
                         ['Example Series', 'Example Novel'])
        self.assertEqual(len(bundle['review_members']), 1)
        candidate = bundle['review_members'][0]
        self.assertEqual(candidate['title'], 'The Other Novel')
        self.assertEqual(candidate['identity_conflict']['title'], 'Example Novel')
        self.assertIn('author and series position match', candidate['review_reason'])

    def test_series_position_conflict_with_different_authors_keeps_both_members(self):
        members, review = [], []
        _merge_series_member(members, {
            'title': 'First Book', 'author': 'First Author',
            'series_index': 1, 'provider': 'Goodreads'}, 'Goodreads', review)
        _merge_series_member(members, {
            'title': 'Second Book', 'author': 'Second Author',
            'series_index': 1, 'provider': 'Open Library'}, 'Open Library', review)

        self.assertEqual([member['title'] for member in members],
                         ['First Book', 'Second Book'])
        self.assertEqual(review, [])

    def test_books_search_metadata_and_download_connectors_together(self):
        file_hit = {'title': 'Example Book', 'author': 'Writer', 'format': '.epub',
                    'url': 'https://files.example/book.epub', 'source': 'Book shelf'}
        with patch('media_intake.discovery.search_goodreads', return_value=[{
                'provider': 'Goodreads', 'kind': 'series', 'title': 'Example Series',
                'source_url': 'https://www.goodreads.com/series/123'}]) as goodreads, \
             patch('media_intake.discovery.search_openlibrary_metadata', return_value=[{
                 'provider': 'Open Library', 'kind': 'book', 'title': 'Example Book',
                 'source_url': 'https://openlibrary.org/works/OL1W'}]) as openlibrary, \
             patch('media_intake.discovery.search_google_books_metadata', return_value=[{
                 'provider': 'Google Books', 'kind': 'book', 'title': 'Example Book',
                 'source_url': 'https://books.google.com/books?id=vol1'}]) as googlebooks, \
             patch('media_intake.discovery._search_candidates', return_value=([file_hit], [])) as files, \
             patch('media_intake.discovery._registered_download_site_files') as extra_sites:
            response = search_media_category('Example', 'Books', {'openlibrary': True})

        self.assertEqual(CATEGORIES[0], 'Books')
        self.assertEqual(goodreads.call_count, 1)
        self.assertEqual(openlibrary.call_count, 1)
        self.assertEqual(googlebooks.call_count, 1)
        self.assertEqual(files.call_count, 1)
        extra_sites.assert_not_called()
        self.assertEqual(len(response['results']), 2)
        self.assertIn('book download sources', response['searched'])
        file_result = next(item for item in response['results'] if item['format'] == '.epub')
        self.assertFalse(file_result.get('discovery_only', False))
        self.assertEqual(file_result.get('metadata_provider'), 'Open Library')
        self.assertEqual({item['provider'] for item in file_result['metadata_variants']},
                         {'Open Library', 'Google Books'})

    def test_google_books_series_members_expand_from_category_search(self):
        google_series = {'provider': 'Google Books', 'kind': 'series',
                         'title': 'Example Series',
                         'source_url': 'https://books.google.com/books?q=series',
                         'books': [
                             {'title': 'Book One', 'author': 'Example Author',
                              'source_url': 'https://books.google.com/books?id=one',
                              'series_index': 1},
                             {'title': 'Book Two', 'author': 'Example Author',
                              'source_url': 'https://books.google.com/books?id=two',
                              'series_index': 2},
                         ]}
        with patch('media_intake.discovery.search_goodreads', return_value=[]), \
             patch('media_intake.discovery.search_openlibrary_metadata', return_value=[]), \
             patch('media_intake.discovery.search_google_books_metadata',
                   return_value=[google_series]), \
             patch('media_intake.discovery._search_wikidata_series', return_value=[]), \
             patch('media_intake.discovery._search_candidates', return_value=([], [])):
            response = search_media_category('Example Series', 'Books')

        self.assertEqual([row['title'] for row in response['results']],
                         ['Example Series', 'Book One', 'Book Two'])
        self.assertEqual([row['series_order'] if 'series_order' in row else row['series_index']
                          for row in response['results'][1:]], [1, 2])
        self.assertIn('Google Books', response['results'][0]['metadata_sources'])
        self.assertIn('Google Books', response['searched'])

    def test_matching_series_is_promoted_ahead_of_other_category_results(self):
        rows = [
            {'kind': 'book', 'title': 'The Way of Kings', 'category': 'Books'},
            {'kind': 'video', 'entity_type': 'series',
             'title': 'The Stormlight Archive', 'category': 'Videos'},
            {'kind': 'book', 'title': 'Stormlight Archive Part One', 'category': 'Books'},
        ]
        promoted = _promote_matching_series_results(rows, 'Stormlight Archive')
        self.assertEqual(promoted[0]['title'], 'The Stormlight Archive')

    def test_manga_searches_all_manga_catalogs_and_filters_other_media(self):
        with patch('media_intake.discovery.search_anilist', return_value=[{
                'provider': 'AniList', 'kind': 'manga', 'title': 'Example Manga',
                'source_url': 'https://anilist.co/manga/1'}]) as anilist, \
             patch('media_intake.discovery.search_mangadex', return_value=[{
                 'provider': 'MangaDex', 'kind': 'manga', 'title': 'Example Manga',
                 'source_url': 'https://mangadex.org/title/id'}]) as mangadex, \
             patch('media_intake.discovery.search_mangaupdates', return_value=[{
                 'provider': 'MangaUpdates', 'title': 'Example Manga',
                 'source_url': 'https://www.mangaupdates.com/series/1'}]) as updates:
            response = search_media_category('Example Manga', 'Manga')

        self.assertEqual(anilist.call_count, 1)
        self.assertEqual(mangadex.call_count, 1)
        self.assertEqual(updates.call_count, 1)
        self.assertEqual(len(response['results']), 1)
        self.assertEqual({item['kind'] for item in response['results']}, {'manga'})
        self.assertEqual(set(response['results'][0]['metadata_sources']),
                         {'AniList', 'MangaDex', 'MangaUpdates'})
        self.assertEqual(len(response['results'][0]['metadata_variants']), 3)
        self.assertEqual(len(response['searched']), 4)

    def test_anime_searches_anilist_as_anime_and_wikidata(self):
        with patch('media_intake.discovery.search_anilist', return_value=[{
                'provider': 'AniList', 'kind': 'anime', 'title': 'Example Anime',
                'source_url': 'https://anilist.co/anime/1'}]) as anilist, \
             patch('media_intake.discovery._query_wikidata', return_value=[{
                 'provider': 'Wikidata', 'kind': 'video', 'entity_type': 'work',
                 'video_subtype': 'anime', 'title': 'Example Anime',
                 'source_url': 'https://www.wikidata.org/entity/Q1', 'format': 'catalog',
                 'discovery_only': True}]) as wikidata:
            response = search_media_category('Example Anime', 'Anime')

        anilist.assert_called_once_with('Example Anime', media_type='ANIME')
        wikidata.assert_called_once_with('Example Anime', 'Anime')
        self.assertEqual(len(response['results']), 1)
        self.assertEqual(set(response['results'][0]['metadata_sources']), {'AniList', 'Wikidata'})
        self.assertEqual(len(response['results'][0]['metadata_variants']), 2)

    def test_fanfiction_category_accepts_a_story_url(self):
        response = search_media_category(
            'https://archiveofourown.org/works/123', 'Fanfiction')
        self.assertEqual(len(response['results']), 1)
        self.assertEqual(response['results'][0]['kind'], 'fanfiction')
        self.assertTrue(response['results'][0]['discovery_only'])

    def test_search_validates_category_and_query(self):
        with self.assertRaises(ValueError):
            search_media_category('', 'Books')
        with self.assertRaises(ValueError):
            search_media_category('example', 'Uncatalogued')


if __name__ == '__main__':
    unittest.main()
