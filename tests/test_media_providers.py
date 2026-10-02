import unittest
from unittest.mock import patch

from media_intake.media_providers import (fetch_tmdb_release_dates, fetch_tmdb_season,
                                          fetch_youtube_playlist_items, search_tmdb,
                                          search_tvmaze)


class SharedMediaProviderTest(unittest.TestCase):
    def test_tvmaze_search_normalizes_series_provider_identity(self):
        fixture = [{
            'score': 0.98,
            'show': {'id': 17, 'name': 'Example Web Show', 'type': 'Scripted',
                     'status': 'Running', 'premiered': '2025-04-03',
                     'url': 'https://www.tvmaze.com/shows/17/example-web-show',
                     'webChannel': {'name': 'Example Channel'}, 'genres': ['Drama'],
                     'network': None, 'schedule': {'time': '20:00', 'days': ['Monday']}},
        }]
        with patch('media_intake.media_providers._json_response', return_value=fixture):
            result = search_tvmaze('Example Web Show')[0]
        self.assertEqual(result['entity_type'], 'series')
        self.assertEqual(result['video_subtype'], 'web_series')
        self.assertEqual(result['release_date'], '2025-04-03')
        self.assertEqual(result['external_ids'][0]['provider'], 'tvmaze')
        self.assertEqual(result['external_ids'][0]['external_id'], '17')

    def test_tmdb_search_keeps_video_subtype_and_release_date(self):
        fixture = {'results': [{
            'id': 99, 'title': 'Example Film', 'release_date': '2031-05-12',
            'overview': 'A catalog fixture.', 'original_language': 'en',
            'genre_ids': [18], 'vote_count': 20,
        }]}
        with patch('media_intake.media_providers._json_response', return_value=fixture):
            result = search_tmdb('Example Film', 'fixture-key', media_type='movie')[0]
        self.assertEqual(result['video_subtype'], 'movie')
        self.assertEqual(result['release_date'], '2031-05-12')
        self.assertEqual(result['publication_status'], 'upcoming')
        self.assertEqual(result['external_ids'][0]['provider'], 'tmdb_movie')
        self.assertEqual(result['external_ids'][0]['external_id'], '99')

    def test_tmdb_movie_release_dates_use_the_region_endpoint(self):
        fixture = {'results': [{'iso_3166_1': 'US', 'country': 'United States',
                                'release_dates': [{'type': 3,
                                                   'release_date': '2030-06-01T00:00:00.000Z'}]}]}
        with patch('media_intake.media_providers._json_response', return_value=fixture) as request:
            results = fetch_tmdb_release_dates(99, 'fixture-key')

        self.assertEqual(results, fixture['results'])
        self.assertEqual(request.call_args.args[0],
                         'https://api.themoviedb.org/3/movie/99/release_dates?api_key=fixture-key')
        self.assertEqual(request.call_args.args[1], {'api.themoviedb.org'})

    def test_tmdb_tv_season_fetch_returns_episode_list(self):
        fixture = {'id': 501, 'season_number': 2, 'episodes': [
            {'id': 7001, 'episode_number': 1, 'name': 'The Start'}]}
        with patch('media_intake.media_providers._json_response', return_value=fixture) as request:
            season = fetch_tmdb_season(99, 2, 'fixture-key')

        self.assertEqual(season, fixture)
        self.assertEqual(request.call_args.args[0],
                         'https://api.themoviedb.org/3/tv/99/season/2?api_key=fixture-key&language=en-US')
        self.assertEqual(request.call_args.args[1], {'api.themoviedb.org'})

    def test_tmdb_tv_season_rejects_a_different_season(self):
        with patch('media_intake.media_providers._json_response', return_value={
                'id': 501, 'season_number': 1, 'episodes': []}):
            with self.assertRaisesRegex(ValueError, 'different or malformed'):
                fetch_tmdb_season(99, 2, 'fixture-key')

    def test_youtube_playlist_fetch_paginates_and_retains_native_item_metadata(self):
        pages = [
            {'items': [{
                'id': 'PLitem1',
                'snippet': {'title': 'Episode One', 'position': 0,
                            'publishedAt': '2026-01-02T03:04:05Z',
                            'videoOwnerChannelId': 'UC' + ('a' * 22),
                            'resourceId': {'videoId': 'video_1'}},
                'contentDetails': {'videoId': 'video_1',
                                   'videoPublishedAt': '2026-01-02T03:04:05Z'},
                'status': {'privacyStatus': 'public'},
            }], 'nextPageToken': 'page-2'},
            {'items': [{'id': 'PLitem2',
                        'snippet': {'title': 'Unavailable item', 'position': 1,
                                    'resourceId': {}},
                        'contentDetails': {}}],
             'pageInfo': {'totalResults': 2}},
            {'items': [{'id': 'video_1',
                        'snippet': {'liveBroadcastContent': 'upcoming'},
                        'contentDetails': {'duration': 'PT1H30M'},
                        'liveStreamingDetails': {
                            'scheduledStartTime': '2026-10-04T18:00:00Z',
                            'actualStartTime': '2026-10-04T18:01:00Z'}}]},
        ]
        with patch('media_intake.media_providers._json_response', side_effect=pages) as request:
            result = fetch_youtube_playlist_items('PLfixture123', 'fixture-key')

        self.assertTrue(result['snapshot_complete'])
        self.assertEqual(result['youtube_playlist_id'], 'PLfixture123')
        self.assertEqual([item['playlist_item_id'] for item in result['youtube_playlist_items']],
                         ['PLitem1', 'PLitem2'])
        self.assertEqual(result['chapters'][0]['url'],
                         'https://www.youtube.com/watch?v=video_1')
        self.assertEqual(result['chapters'][0]['provider_metadata']['position'], 0)
        self.assertEqual(result['chapters'][0]['published'], '2026-10-04T18:00:00Z')
        self.assertEqual(result['youtube_playlist_items'][0]['scheduled_start_at'],
                         '2026-10-04T18:00:00Z')
        self.assertEqual(result['youtube_playlist_items'][0]['duration'], 'PT1H30M')
        self.assertEqual(result['youtube_playlist_items'][0]['live_broadcast_content'],
                         'upcoming')
        self.assertEqual(result['youtube_playlist_items'][0]['actual_start_at'],
                         '2026-10-04T18:01:00Z')
        self.assertEqual(result['youtube_playlist_items'][0]['privacy_status'], 'public')
        self.assertEqual(request.call_count, 3)
        self.assertIn('pageToken=page-2', request.call_args_list[1].args[0])
        self.assertIn('videos?', request.call_args_list[2].args[0])

    def test_youtube_schedule_failure_does_not_invalidate_playlist_snapshot(self):
        page = {'items': [{'id': 'playlist-item', 'snippet': {
            'title': 'Public video', 'position': 0,
            'resourceId': {'videoId': 'video_1'}}, 'contentDetails': {}}]}
        with patch('media_intake.media_providers._json_response',
                   side_effect=[page, ValueError('schedule endpoint unavailable')]):
            result = fetch_youtube_playlist_items('PLfixture123', 'fixture-key')

        self.assertTrue(result['snapshot_complete'])
        self.assertIn('schedule endpoint unavailable', result['youtube_schedule_error'])
        self.assertEqual(len(result['youtube_playlist_items']), 1)

    def test_youtube_playlist_cap_marks_snapshot_incomplete(self):
        rows = [{'id': f'item-{index}', 'snippet': {'resourceId': {}}}
                for index in range(50)]
        with patch('media_intake.media_providers._json_response', return_value={
                'items': rows, 'nextPageToken': 'more'}):
            result = fetch_youtube_playlist_items('PLfixture123', 'fixture-key', max_items=50)
        self.assertFalse(result['snapshot_complete'])
        self.assertEqual(len(result['youtube_playlist_items']), 50)


if __name__ == '__main__':
    unittest.main()
