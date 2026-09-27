import unittest
from unittest.mock import patch

from media_intake.discovery import CATEGORIES, search_media_category


class CategoryDiscoveryTest(unittest.TestCase):
    def test_books_search_metadata_and_download_connectors_together(self):
        file_hit = {'title': 'Example Book', 'author': 'Writer', 'format': '.epub',
                    'url': 'https://files.example/book.epub', 'source': 'Book shelf'}
        with patch('media_intake.discovery.search_goodreads', return_value=[{
                'provider': 'Goodreads', 'kind': 'series', 'title': 'Example Series',
                'source_url': 'https://www.goodreads.com/series/123'}]) as goodreads, \
             patch('media_intake.discovery.search_openlibrary_metadata', return_value=[{
                 'provider': 'Open Library', 'kind': 'book', 'title': 'Example Book',
                 'source_url': 'https://openlibrary.org/works/OL1W'}]) as openlibrary, \
             patch('media_intake.discovery._search_candidates', return_value=([file_hit], [])) as files:
            response = search_media_category('Example', 'Books', {'openlibrary': True})

        self.assertEqual(CATEGORIES[0], 'Books')
        self.assertEqual(goodreads.call_count, 1)
        self.assertEqual(openlibrary.call_count, 1)
        self.assertEqual(files.call_count, 1)
        self.assertEqual(len(response['results']), 2)
        self.assertIn('book download sources', response['searched'])
        file_result = next(item for item in response['results'] if item['format'] == '.epub')
        self.assertFalse(file_result.get('discovery_only', False))
        self.assertEqual(file_result.get('metadata_provider'), 'Open Library')
        self.assertEqual(len(file_result['metadata_variants']), 1)

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
                 'provider': 'Wikidata', 'kind': 'entity', 'title': 'Example Anime',
                 'source_url': 'https://www.wikidata.org/entity/Q1', 'format': 'catalog',
                 'discovery_only': True}]) as wikidata:
            response = search_media_category('Example Anime', 'Anime')

        anilist.assert_called_once_with('Example Anime', media_type='ANIME')
        wikidata.assert_called_once_with('Example Anime')
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
