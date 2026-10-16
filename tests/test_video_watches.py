import datetime as dt
import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import call, patch

from media_intake.core import connect, list_items
from media_intake.media_graph import (list_graph_reviews, list_media_changes,
                                      set_reviewed_media_release_date, set_reviewed_release_date,
                                      queue_graph_review, resolve_graph_review,
                                      set_watch_policy)
from media_intake.watched import (add_watch, process_due_watches,
                                  _classify_release_announcement,
                                  _extract_announced_release_date,
                                  _feed_episode_position, _normalized_release_status,
                                  _search_candidates, _video_feed_entry_type)


class VideoWatchTest(unittest.TestCase):
    def test_creator_feed_entry_type_uses_explicit_episode_and_post_markers(self):
        self.assertEqual(_video_feed_entry_type('Trailer: Season Two', [], {}), 'trailer')
        self.assertEqual(_video_feed_entry_type('Preview', [], {'episode_type': 'trailer'}),
                         'trailer')
        self.assertEqual(_video_feed_entry_type('Bonus Episode', ['bonus'], {}), 'bonus')
        self.assertEqual(_video_feed_entry_type('Schedule Update: New Date', [], {}),
                         'announcement')
        self.assertEqual(_video_feed_entry_type('Episode 4', [], {}), 'episode')
        self.assertEqual(_video_feed_entry_type(
            'Weekly update', [], {'summary': 'Official trailer for the upcoming season.'}),
            'trailer')
        self.assertEqual(_video_feed_entry_type(
            'Extra material', ['behind the scenes'], {}), 'bonus')
        self.assertEqual(_video_feed_entry_type(
            'Weekly update', [], {'summary': 'News update: the release is delayed.'}),
            'announcement')
        self.assertEqual(_video_feed_entry_type(
            'Studio calendar', [], {'event_type': 'renewal'}), 'announcement')
        self.assertEqual(_video_feed_entry_type(
            'Community screening', [], {'event_type': 'screening'}), 'announcement')
        self.assertEqual(_video_feed_entry_type(
            'Episode 5', [], {'summary': 'The episode includes a trailer for another show.'}),
            'episode')

    def test_wikidata_short_video_subtype_updates_wikidata_primary_watch(self):
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Independent Short', metadata={
                'entity_type': 'work', 'video_subtype': 'movie',
                'catalog_provider': 'Wikidata', 'sources': {},
                'external_ids': [{'provider': 'wikidata', 'external_id': 'Q200'}],
            })
            entity_id = db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                   (watch_id,)).fetchone()['media_entity_id']
            with patch('media_intake.watched.import_wikidata_entity', return_value={
                    'root_entity_id': entity_id, 'video_subtype': 'independent_animation',
                    'relationships': []}):
                process_due_watches(db, folder, force=True)
            metadata = json.loads(db.execute('SELECT metadata FROM watched_items WHERE id=?',
                                             (watch_id,)).fetchone()['metadata'])

        self.assertEqual(metadata['video_subtype'], 'independent_animation')

    def test_video_status_normalization_preserves_lifecycle_states(self):
        cases = {
            'Returning Series': 'ongoing', 'Running': 'ongoing',
            'Ended': 'completed', 'Cancelled': 'cancelled',
            'Canceled': 'cancelled', 'On Hiatus': 'hiatus',
            'In Production': 'in_production', 'To Be Determined': 'unscheduled',
            'Planned': 'upcoming', 'Released': 'released',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(_normalized_release_status(raw), expected)

    def test_tvmaze_status_change_is_kept_in_graph_history(self):
        base_show = {'id': 78, 'name': 'Lifecycle Show', 'premiered': '2022-01-01',
                     'url': 'https://www.tvmaze.com/shows/78/lifecycle-show',
                     'network': {'name': 'Example Network'}, 'webChannel': None,
                     'schedule': {}, 'genres': []}
        first_show = {**base_show, 'status': 'Running'}
        canceled_show = {**base_show, 'status': 'Canceled', 'ended': '2025-06-30'}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Lifecycle Show', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series',
                'catalog_provider': 'TVMaze',
                'external_ids': [{'provider': 'tvmaze', 'external_id': '78'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_tvmaze_show',
                       side_effect=[first_show, canceled_show]), patch(
                    'media_intake.watched.list_tvmaze_seasons', return_value=[]), patch(
                    'media_intake.watched.list_tvmaze_episodes', return_value=[]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            entity = db.execute('''SELECT e.status,e.id AS media_entity_id FROM watched_items w
                JOIN media_entities e ON e.id=w.media_entity_id WHERE w.id=?''', (watch_id,)).fetchone()
            premiere = db.execute('''SELECT status FROM media_releases
                WHERE entity_id=? AND provider='TVMaze' AND release_type='premiere'
                AND provider_release_id='root-release' ''', (entity['media_entity_id'],)).fetchone()
            claims = [json.loads(row['value_json']) for row in db.execute('''SELECT value_json
                FROM media_claim_observations WHERE entity_id=? AND field_name='status'
                AND provider='tvmaze' ORDER BY id''', (entity['media_entity_id'],))]
            changes = list_media_changes(db, entity['media_entity_id'])

        self.assertEqual(entity['status'], 'cancelled')
        self.assertEqual(premiere['status'], 'cancelled')
        self.assertIn('ongoing', claims)
        self.assertIn('cancelled', claims)
        self.assertIn('release_updated', {item['change_type'] for item in changes})

    def test_tvmaze_watch_tracks_seasons_and_schedules_new_episodes(self):
        today = dt.date.today()
        old_date = (today - dt.timedelta(days=30)).isoformat()
        air_date = (today + dt.timedelta(days=14)).isoformat()
        special_date = (today + dt.timedelta(days=7)).isoformat()
        show = {'id': 77, 'name': 'Example TV', 'premiered': '2020-01-01',
                'status': 'Running', 'url': 'https://www.tvmaze.com/shows/77/example-tv',
                'network': {'name': 'Example Network'}, 'webChannel': None,
                'schedule': {'time': '20:00', 'days': ['Friday']}, 'genres': ['Drama']}
        seasons = [{'id': 701, 'number': 1, 'premiereDate': '2020-01-01'}]
        episodes = [
            {'id': 7001, 'name': 'Old Episode', 'season': 1, 'number': 1,
             'airdate': old_date, 'url': 'https://www.tvmaze.com/episodes/7001'},
            {'id': 7002, 'name': 'Future Episode', 'season': 2, 'number': 1,
             'airdate': air_date, 'url': 'https://www.tvmaze.com/episodes/7002'},
            {'id': 7003, 'name': 'Bonus Story', 'season': 0, 'number': 1,
             'type': 'special', 'airdate': special_date,
             'url': 'https://www.tvmaze.com/episodes/7003'},
        ]
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            set_watch_policy(db, 'media_type', 'video', {'episodes': 'auto_add'})
            db.commit()
            root_id = add_watch(db, 'video', 'Example TV', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series',
                'catalog_provider': 'TVMaze',
                'external_ids': [{'provider': 'tvmaze', 'external_id': '77'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_tvmaze_show', return_value=show), patch(
                    'media_intake.watched.list_tvmaze_seasons', return_value=seasons), patch(
                    'media_intake.watched.list_tvmaze_episodes', return_value=episodes):
                process_due_watches(db, folder, force=True)
            rows = [dict(row) for row in db.execute(
                'SELECT id,kind,title,state,parent_id,release_date,metadata FROM watched_items ORDER BY id')]
            releases = [dict(row) for row in db.execute('''SELECT release_type,release_date,status,provider
                FROM media_releases WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND release_type='episode' ORDER BY provider_release_id''', (root_id,))]
            graph_edges = [dict(row) for row in db.execute('''SELECT relationship_type,status,
                    source_entity_id,target_entity_id
                FROM media_relationships WHERE source_entity_id=(SELECT media_entity_id
                    FROM watched_items WHERE id=?) OR target_entity_id=(SELECT media_entity_id
                    FROM watched_items WHERE id=?)''', (root_id, root_id))]
            root_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (root_id,)).fetchone()[0])
            queue = [json.loads(row['metadata']) for row in list_items(db)]

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]['title'], 'Example TV S02E01 - Future Episode')
        self.assertEqual(rows[1]['kind'], 'video_episode')
        self.assertEqual(rows[1]['state'], 'upcoming')
        self.assertEqual(rows[1]['parent_id'], root_id)
        self.assertEqual(rows[1]['release_date'], air_date)
        self.assertEqual(rows[2]['title'], 'Example TV S00E01 - Bonus Story')
        self.assertEqual(json.loads(rows[2]['metadata'])['video_subtype'], 'special')
        self.assertEqual(len(releases), 3)
        self.assertTrue(any(edge['relationship_type'] == 'contains' and
                            edge['status'] == 'accepted' for edge in graph_edges))
        by_type = {edge['relationship_type']: edge for edge in graph_edges}
        self.assertEqual(by_type['season_of']['target_entity_id'], root_entity_id)
        self.assertEqual(by_type['episode_of']['target_entity_id'], root_entity_id)
        self.assertEqual(by_type['special_of']['target_entity_id'], root_entity_id)
        self.assertNotEqual(by_type['episode_of']['source_entity_id'], root_entity_id)
        self.assertNotEqual(by_type['special_of']['source_entity_id'], root_entity_id)
        self.assertTrue(any(item.get('watch_search') and item.get('watch_id') == rows[1]['id']
                            for item in queue))

    def test_specials_policy_can_discover_specials_while_auto_adding_episodes(self):
        future = (dt.date.today() + dt.timedelta(days=12)).isoformat()
        show = {'id': 88, 'name': 'Policy TV', 'premiered': '2020-01-01',
                'status': 'Running', 'url': 'https://www.tvmaze.com/shows/88/policy-tv',
                'network': None, 'webChannel': None, 'schedule': {}, 'genres': []}
        episodes = [
            {'id': 8801, 'name': 'Regular Episode', 'season': 1, 'number': 2,
             'airdate': future, 'url': 'https://www.tvmaze.com/episodes/8801'},
            {'id': 8802, 'name': 'Special Feature', 'season': 0, 'number': 1,
             'type': 'special', 'airdate': future,
             'url': 'https://www.tvmaze.com/episodes/8802'},
        ]
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            set_watch_policy(db, 'media_type', 'video', {
                'episodes': 'auto_add', 'specials': 'discover'})
            db.commit()
            root_id = add_watch(db, 'video', 'Policy TV', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series',
                'catalog_provider': 'TVMaze',
                'external_ids': [{'provider': 'tvmaze', 'external_id': '88'}], 'sources': {},
            })
            with patch('media_intake.watched.fetch_tvmaze_show', return_value=show), patch(
                    'media_intake.watched.list_tvmaze_seasons', return_value=[]), patch(
                    'media_intake.watched.list_tvmaze_episodes', return_value=episodes):
                process_due_watches(db, folder, force=True)
            watches = [dict(row) for row in db.execute('''SELECT kind,title FROM watched_items
                WHERE parent_id=? ORDER BY id''', (root_id,))]
            edges = [dict(row) for row in db.execute('''SELECT r.relationship_type,r.status,e.title
                FROM media_relationships r
                JOIN media_entities e ON e.id=r.source_entity_id
                WHERE r.target_entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)''',
                (root_id,))]

        self.assertEqual([row['title'] for row in watches],
                         ['Policy TV S01E02 - Regular Episode'])
        self.assertIn({'relationship_type': 'episode_of', 'status': 'accepted',
                       'title': 'Policy TV S01E02 - Regular Episode'}, edges)
        self.assertIn({'relationship_type': 'special_of', 'status': 'candidate',
                       'title': 'Policy TV S00E01 - Special Feature'}, edges)

    def test_tmdb_and_tvmaze_merge_the_same_season_episode_watch(self):
        air_date = (dt.date.today() + dt.timedelta(days=14)).isoformat()
        tmdb_show = {
            'id': 99, 'name': 'Cross Catalog TV', 'first_air_date': '2022-01-01',
            'status': 'Returning Series', 'seasons': [
                {'id': 501, 'season_number': 1, 'name': 'Season 1',
                 'air_date': '2022-01-01', 'episode_count': 1}],
        }
        tmdb_season = {'id': 501, 'season_number': 1, 'episodes': [
            {'id': 9001, 'name': 'Shared Episode', 'episode_number': 2,
             'air_date': air_date}]}
        tvmaze_show = {'id': 17, 'name': 'Cross Catalog TV', 'premiered': '2022-01-01',
                       'status': 'Running', 'url': 'https://www.tvmaze.com/shows/17/cross-catalog-tv',
                       'network': None, 'webChannel': None, 'schedule': {}, 'genres': []}
        tvmaze_episode = {'id': 7001, 'name': 'Shared Episode', 'season': 1,
                          'number': 2, 'airdate': air_date,
                          'url': 'https://www.tvmaze.com/episodes/7001'}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            set_watch_policy(db, 'media_type', 'video', {'episodes': 'auto_add'})
            db.commit()
            root_url = 'https://www.themoviedb.org/tv/99'
            root_id = add_watch(db, 'video', 'Cross Catalog TV', source_url=root_url,
                                metadata={'entity_type': 'series', 'video_subtype': 'tv_series',
                                          'catalog_provider': 'TMDB',
                                          'external_ids': [{'provider': 'tmdb_tv',
                                                            'external_id': '99'}],
                                          'sources': {}})
            add_watch(db, 'video', 'Cross Catalog TV', source_url=root_url,
                      metadata={'entity_type': 'series', 'video_subtype': 'tv_series',
                                'catalog_provider': 'TMDB', 'external_ids': [
                                    {'provider': 'tmdb_tv', 'external_id': '99'},
                                    {'provider': 'tvmaze', 'external_id': '17'}],
                                'sources': {}})
            with patch('media_intake.watched.fetch_tmdb_media', return_value=tmdb_show), patch(
                    'media_intake.watched.fetch_tmdb_season', return_value=tmdb_season), patch(
                    'media_intake.watched.fetch_tvmaze_show', return_value=tvmaze_show), patch(
                    'media_intake.watched.list_tvmaze_seasons', return_value=[]), patch(
                    'media_intake.watched.list_tvmaze_episodes', return_value=[tvmaze_episode]):
                process_due_watches(db, folder, force=True,
                                    source_options={'tmdb_api_key': 'fixture-key'})
            child_watches = [dict(row) for row in db.execute('''SELECT id,title,parent_id
                FROM watched_items WHERE kind='video_episode' ''')]
            child_entity = db.execute('''SELECT e.id FROM media_entities e
                JOIN watched_items w ON w.media_entity_id=e.id WHERE w.kind='video_episode'
                ORDER BY w.id LIMIT 1''').fetchone()
            provider_ids = {row['provider'] for row in db.execute('''SELECT provider
                FROM media_external_ids WHERE entity_id=?''', (child_entity['id'],))}
            episode_release_providers = {row['provider'] for row in db.execute('''SELECT provider
                FROM media_releases WHERE entity_id=(SELECT media_entity_id FROM watched_items
                WHERE id=?) AND release_type='episode' ''', (root_id,))}
            root_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (root_id,)).fetchone()[0])
            episode_edge = db.execute('''SELECT source_entity_id,target_entity_id
                FROM media_relationships WHERE relationship_type='episode_of'
                  AND source_entity_id=? AND target_entity_id=?''',
                (child_entity['id'], root_entity_id)).fetchone()
            episode_edge_providers = {row['provider'] for row in db.execute('''SELECT e.provider
                FROM media_relationships r JOIN media_relationship_evidence e
                  ON e.relationship_id=r.id
                WHERE r.relationship_type='episode_of' AND r.source_entity_id=?
                  AND r.target_entity_id=?''', (child_entity['id'], root_entity_id))}

        self.assertEqual(len(child_watches), 1)
        self.assertEqual(child_watches[0]['title'],
                         'Cross Catalog TV S01E02 - Shared Episode')
        self.assertEqual(child_watches[0]['parent_id'], root_id)
        self.assertTrue({'media-intake-episode', 'tmdb_episode', 'tvmaze_episode'}
                        <= provider_ids)
        self.assertEqual(episode_release_providers, {'TMDB', 'TVMaze'})
        self.assertEqual(episode_edge['source_entity_id'], child_entity['id'])
        self.assertEqual(episode_edge['target_entity_id'], root_entity_id)
        self.assertEqual(episode_edge_providers, {'tmdb_episode', 'tvmaze_episode'})

    def test_video_episode_search_uses_video_irc_category_and_formats(self):
        title = 'Example Episode S01E01'
        candidate = {'title': title, 'format': '.mkv', 'provider': 'IRC',
                     'irc': {'server': 'irc.example', 'channel': '#video'}}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video_episode', title,
                                 metadata={'video_subtype': 'episode', 'sources': {}})
            watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?',
                                    (watch_id,)).fetchone())
            with patch('media_intake.watched.has_configured_source', return_value=True), patch(
                    'media_intake.watched._download_search_plan', return_value=('Videos', ['irc'])), patch(
                    'media_intake.watched.search_configured_sources',
                    return_value=([candidate], [])) as search:
                results, errors = _search_candidates(watch, {'irc_servers': [{'name': 'primary'}]})

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['format'], '.mkv')
        self.assertEqual(search.call_args.args[2], 'Videos')
        self.assertIn('.mkv', search.call_args.args[3])

    def test_due_video_episode_automatically_enqueues_and_downloads_irc_result(self):
        title = 'Example Episode S01E01'
        candidate = {'title': title, 'format': '.mkv', 'provider': 'IRC',
                     'irc': {'server': 'irc.example', 'channel': '#video',
                             'request': 'Example Episode'}}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video_episode', title, release_date=dt.date.today().isoformat(),
                                 metadata={'entity_type': 'work', 'video_subtype': 'episode',
                                           'catalog_status': 'released', 'sources': {}})
            with patch('media_intake.watched._search_candidates',
                       return_value=([candidate], [])) as search, patch(
                    'media_intake.watched.enqueue_irc', return_value={'id': 501}) as enqueue_request, patch(
                    'media_intake.watched.download', return_value={
                        'file_path': str(Path(folder) / 'Example Episode.mkv')}) as download_file:
                checked = process_due_watches(db, folder, force=True)
            saved = db.execute('SELECT state,file_path FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()

        self.assertEqual(checked, 1)
        self.assertEqual(search.call_args.args[0]['kind'], 'video_episode')
        enqueue_request.assert_called_once()
        download_file.assert_called_once()
        self.assertEqual(saved['state'], 'downloaded')
        self.assertEqual(saved['file_path'], str(Path(folder) / 'Example Episode.mkv'))

    def test_anilist_series_continuity_keeps_sequel_movie_as_video_work(self):
        details = {
            'id': 707, 'title': {'english': 'Anime Series', 'romaji': 'Anime Series'},
            'type': 'ANIME', 'format': 'TV', 'startDate': {'year': 2020, 'month': 4, 'day': 1},
            'status': 'FINISHED', 'episodes': 12, 'relations': {'edges': [
                {'relationType': 'SEQUEL', 'node': {
                    'id': 808, 'type': 'ANIME', 'format': 'MOVIE',
                    'title': {'english': 'Anime Sequel Film'},
                    'siteUrl': 'https://anilist.co/anime/808',
                    'startDate': {'year': 2025, 'month': 5, 'day': 2},
                    'status': 'FINISHED',
                }},
                {'relationType': 'PREQUEL', 'node': {
                    'id': 809, 'type': 'ANIME', 'format': 'MOVIE',
                    'title': {'english': 'Anime Prequel Film'},
                    'siteUrl': 'https://anilist.co/anime/809',
                    'startDate': {'year': 2019, 'month': 5, 'day': 2},
                    'status': 'FINISHED',
                }},
                {'relationType': 'SPIN_OFF', 'node': {
                    'id': 810, 'type': 'ANIME', 'format': 'TV',
                    'title': {'english': 'Anime Spin-off'},
                    'siteUrl': 'https://anilist.co/anime/810',
                    'startDate': {'year': 2023, 'month': 5, 'day': 2},
                    'status': 'FINISHED',
                }},
                {'relationType': 'SEQUEL', 'node': {
                    'id': 812, 'type': 'ANIME', 'format': 'TV',
                    'title': {'english': 'Anime Sequel Series'},
                    'siteUrl': 'https://anilist.co/anime/812',
                    'startDate': {'year': 2026, 'month': 5, 'day': 2},
                    'status': 'FINISHED',
                }},
                {'relationType': 'PREQUEL', 'node': {
                    'id': 813, 'type': 'ANIME', 'format': 'ONA',
                    'title': {'english': 'Anime Prequel Series'},
                    'siteUrl': 'https://anilist.co/anime/813',
                    'startDate': {'year': 2018, 'month': 5, 'day': 2},
                    'status': 'FINISHED',
                }},
                {'relationType': 'REMAKE', 'node': {
                    'id': 811, 'type': 'ANIME', 'format': 'MOVIE',
                    'title': {'english': 'Anime Remake Film'},
                    'siteUrl': 'https://anilist.co/anime/811',
                    'startDate': {'year': 2026, 'month': 5, 'day': 2},
                    'status': 'FINISHED',
                }},
            ]},
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            set_watch_policy(db, 'media_type', 'video', {
                'direct_sequel_series': 'discover',
                'direct_prequel_series': 'discover',
                'direct_sequel_works': 'auto_add',
                'direct_prequel_works': 'auto_add',
            })
            root_id = add_watch(db, 'video', 'Anime Series', metadata={
                'entity_type': 'series', 'video_subtype': 'anime_series',
                'catalog_provider': 'AniList',
                'external_ids': [{'provider': 'anilist', 'external_id': '707'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_anilist_media', return_value=details):
                process_due_watches(db, folder, force=True)
            rows = [dict(row) for row in db.execute('''SELECT w.id,w.kind,w.title,w.metadata,
                e.id AS entity_id,e.metadata_json FROM watched_items w
                JOIN media_entities e ON e.id=w.media_entity_id ORDER BY w.id''')]
            root_entity_id = next(row['entity_id'] for row in rows if row['id'] == root_id)
            edges = [dict(row) for row in db.execute('''SELECT r.relationship_type,r.status,
                    r.source_entity_id,r.target_entity_id,x.external_id,
                    evidence.raw_relationship_type
                FROM media_relationships r
                JOIN media_external_ids x ON x.entity_id=CASE
                    WHEN r.source_entity_id=? THEN r.target_entity_id
                    ELSE r.source_entity_id END AND x.provider='anilist'
                JOIN media_relationship_evidence evidence ON evidence.relationship_id=r.id
                    AND evidence.provider='anilist'
                WHERE r.source_entity_id=? OR r.target_entity_id=?''',
                (root_entity_id, root_entity_id, root_entity_id))]
            entities_by_anilist_id = {row['external_id']: row['entity_id'] for row in db.execute(
                "SELECT external_id,entity_id FROM media_external_ids WHERE provider='anilist'")}

        sequel = next(row for row in rows if row['title'] == 'Anime Sequel Film')
        prequel = next(row for row in rows if row['title'] == 'Anime Prequel Film')
        self.assertEqual(sequel['kind'], 'video')
        self.assertEqual(prequel['kind'], 'video')
        self.assertEqual(json.loads(sequel['metadata'])['video_subtype'], 'anime_movie')
        self.assertEqual(json.loads(sequel['metadata_json'])['video_subtype'], 'anime_movie')
        self.assertEqual(json.loads(prequel['metadata'])['video_subtype'], 'anime_movie')
        self.assertNotIn('Anime Sequel Series', [row['title'] for row in rows])
        self.assertNotIn('Anime Prequel Series', [row['title'] for row in rows])
        self.assertNotIn('Anime Remake Film', [row['title'] for row in rows])
        self.assertEqual(len(edges), 6)
        expected = {
            '808': ('sequel_to', 'accepted', 'SEQUEL'),
            '809': ('prequel_to', 'accepted', 'PREQUEL'),
            '810': ('spin_off_of', 'candidate', 'SPIN_OFF'),
            '812': ('sequel_to', 'candidate', 'SEQUEL'),
            '813': ('prequel_to', 'candidate', 'PREQUEL'),
            '811': ('remake_of', 'accepted', 'REMAKE'),
        }
        self.assertEqual({edge['external_id']: (edge['relationship_type'], edge['status'],
                                                 edge['raw_relationship_type'])
                          for edge in edges}, expected)
        for edge in edges:
            self.assertEqual(edge['source_entity_id'],
                             entities_by_anilist_id[edge['external_id']])
            self.assertEqual(edge['target_entity_id'], root_entity_id)

    def test_game_refresh_tracks_launch_and_policy_added_dlc(self):
        future = dt.date.today() + dt.timedelta(days=12)
        timestamp = int(dt.datetime.combine(
            future, dt.time.min, tzinfo=dt.timezone.utc).timestamp())
        details = {
            'id': 100, 'name': 'Example Game', 'first_release_date': timestamp,
            'url': 'https://www.igdb.com/games/example-game',
            'release_dates': [
                {'id': 9001, 'date': timestamp, 'platform': {'name': 'Windows'},
                 'release_region': {'name': 'North America'},
                 'status': {'name': 'Full release'}},
                {'id': 9002, 'date': timestamp + 86_400,
                 'platform': {'name': 'PlayStation 5'},
                 'release_region': {'name': 'Europe'},
                 'status': {'name': 'Full release'}},
            ],
            'dlcs': [{'id': 101, 'name': 'Example DLC'}], 'expansions': [],
            'standalone_expansions': [], 'remakes': [], 'remasters': [], 'ports': [],
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            set_watch_policy(db, 'media_type', 'game', {'game_dlc': 'auto_add'})
            db.commit()
            root_id = add_watch(db, 'game', 'Example Game', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game',
                'catalog_provider': 'IGDB',
                'external_ids': [{'provider': 'igdb', 'external_id': '100'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_igdb_game', return_value=details):
                process_due_watches(db, folder, force=True,
                                    source_options={'igdb_client_id': 'fixture',
                                                    'igdb_client_secret': 'fixture'})

            rows = [dict(row) for row in db.execute(
                'SELECT id,kind,title,release_date,parent_id,metadata FROM watched_items ORDER BY id')]
            release = db.execute('''SELECT release_date,status FROM media_releases
                WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='IGDB' AND release_type='game_launch' ''', (root_id,)).fetchone()
            platform_releases = [dict(row) for row in db.execute('''SELECT release_date,region,platform,status
                FROM media_releases WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='IGDB' AND release_type='platform_launch'
                ORDER BY provider_release_id''', (root_id,))]
            queued = list_items(db)

        self.assertEqual(rows[0]['release_date'], future.isoformat())
        self.assertEqual(release['release_date'], future.isoformat())
        self.assertEqual(release['status'], 'upcoming')
        self.assertEqual(platform_releases, [
            {'release_date': future.isoformat(), 'region': 'North America',
             'platform': 'Windows', 'status': 'Full release'},
            {'release_date': (future + dt.timedelta(days=1)).isoformat(),
             'region': 'Europe', 'platform': 'PlayStation 5', 'status': 'Full release'},
        ])
        self.assertEqual(rows[1]['title'], 'Example DLC')
        self.assertEqual(rows[1]['kind'], 'game')
        self.assertEqual(rows[1]['parent_id'], root_id)
        self.assertEqual(json.loads(rows[1]['metadata'])['game_subtype'], 'dlc')
        self.assertTrue(any(json.loads(row['metadata']).get('watch_search') and
                            json.loads(row['metadata']).get('title') == 'Example Game'
                            for row in queued))

    def test_igdb_remakes_remasters_and_ports_use_provider_neutral_graph_edges(self):
        details = {
            'id': 110, 'name': 'Original Game',
            'url': 'https://www.igdb.com/games/original-game',
            'first_release_date': int(dt.datetime(2010, 1, 1, tzinfo=dt.timezone.utc).timestamp()),
            'release_dates': [], 'dlcs': [], 'expansions': [],
            'standalone_expansions': [],
            'remakes': [{'id': 111, 'name': 'Original Game Remake'}],
            'remasters': [{'id': 112, 'name': 'Original Game Remaster'}],
            'ports': [{'id': 113, 'name': 'Original Game Port'}],
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_watch_id = add_watch(db, 'game', 'Original Game', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game',
                'catalog_provider': 'IGDB',
                'external_ids': [{'provider': 'igdb', 'external_id': '110'}],
                'sources': {},
            })
            root_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?',
                (root_watch_id,)).fetchone()[0])
            with patch('media_intake.watched.fetch_igdb_game', return_value=details):
                process_due_watches(db, folder, force=True,
                                    source_options={'igdb_client_id': 'fixture',
                                                    'igdb_client_secret': 'fixture'})
            relationships = {}
            for row in db.execute('''SELECT r.relationship_type,r.source_entity_id,
                    r.target_entity_id,x.external_id,e.raw_relationship_type
                FROM media_relationships r
                JOIN media_external_ids x ON x.entity_id=CASE
                    WHEN r.source_entity_id=? THEN r.target_entity_id ELSE r.source_entity_id END
                    AND x.provider='igdb'
                JOIN media_relationship_evidence e ON e.relationship_id=r.id
                    AND e.provider='igdb'
                WHERE r.source_entity_id=? OR r.target_entity_id=?''',
                                  (root_entity_id, root_entity_id, root_entity_id)):
                relationships[row['external_id']] = dict(row)

        self.assertEqual(set(relationships), {'111', '112', '113'})
        self.assertEqual(relationships['111']['relationship_type'], 'remake_of')
        self.assertEqual(relationships['111']['raw_relationship_type'], 'remakes')
        self.assertNotEqual(relationships['111']['source_entity_id'], root_entity_id)
        self.assertEqual(relationships['111']['target_entity_id'], root_entity_id)
        for external_id, raw_type in (('112', 'remasters'), ('113', 'ports')):
            self.assertEqual(relationships[external_id]['relationship_type'], 'edition_of')
            self.assertEqual(relationships[external_id]['raw_relationship_type'], raw_type)
            self.assertNotEqual(relationships[external_id]['source_entity_id'], root_entity_id)
            self.assertEqual(relationships[external_id]['target_entity_id'], root_entity_id)

    def test_tmdb_movie_refresh_tracks_country_specific_release_windows(self):
        movie = {'id': 990, 'title': 'Example Film', 'release_date': '2024-05-01',
                 'status': 'Released', 'belongs_to_collection': None}
        release_regions = [
            {'iso_3166_1': 'US', 'country': 'United States', 'release_dates': [
                {'type': 3, 'release_date': '2030-06-01T00:00:00.000Z',
                 'certification': 'PG-13', 'note': 'wide release'},
                {'type': 4, 'release_date': '2030-07-10T00:00:00.000Z',
                 'certification': '', 'note': 'digital'},
            ]},
            {'iso_3166_1': 'GB', 'country': 'United Kingdom', 'release_dates': [
                {'type': 5, 'release_date': '2030-08-15T00:00:00.000Z',
                 'certification': '12', 'note': 'home video'},
            ]},
        ]
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Film', metadata={
                'entity_type': 'work', 'video_subtype': 'movie',
                'catalog_provider': 'TMDB',
                'external_ids': [{'provider': 'tmdb_movie', 'external_id': '990'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_tmdb_media', return_value=movie), patch(
                    'media_intake.watched.fetch_tmdb_release_dates',
                    return_value=release_regions) as fetch_dates:
                process_due_watches(db, folder, force=True,
                                    source_options={'tmdb_api_key': 'fixture-key'})
            releases = [dict(row) for row in db.execute('''SELECT release_type,release_date,region,status
                FROM media_releases WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='TMDB' AND release_type LIKE 'video_%'
                ORDER BY region,release_type''', (watch_id,))]

        fetch_dates.assert_called_once_with('990', 'fixture-key')
        self.assertEqual(releases, [
            {'release_type': 'video_physical', 'release_date': '2030-08-15',
             'region': 'GB', 'status': 'upcoming'},
            {'release_type': 'video_digital', 'release_date': '2030-07-10',
             'region': 'US', 'status': 'upcoming'},
            {'release_type': 'video_theatrical', 'release_date': '2030-06-01',
             'region': 'US', 'status': 'upcoming'},
        ])

    def test_tmdb_collection_membership_links_movies_without_auto_adding_them(self):
        movie = {'id': 991, 'title': 'Collection Film One', 'release_date': '2024-05-01',
                 'status': 'Released',
                 'belongs_to_collection': {'id': 701, 'name': 'Example Film Collection'}}
        collection = {'id': 701, 'name': 'Example Film Collection', 'parts': [
            {'id': 992, 'title': 'Collection Film Two', 'release_date': '2026-04-01',
             'overview': 'Second film in this catalog collection.'},
        ]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Collection Film One', metadata={
                'entity_type': 'work', 'video_subtype': 'movie',
                'catalog_provider': 'TMDB',
                'external_ids': [{'provider': 'tmdb_movie', 'external_id': '991'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_tmdb_media', return_value=movie), patch(
                    'media_intake.watched.fetch_tmdb_release_dates', return_value=[]), patch(
                    'media_intake.watched.fetch_tmdb_collection', return_value=collection):
                process_due_watches(db, folder, force=True,
                                    source_options={'tmdb_api_key': 'fixture-key'})
            root_entity = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (watch_id,)).fetchone()[0])
            entities = {row['external_id']: row['entity_id'] for row in db.execute('''
                SELECT external_id,entity_id FROM media_external_ids
                WHERE (provider='tmdb_collection' AND external_id='701')
                   OR (provider='tmdb_movie' AND external_id='992')''')}
            edges = {(row['relationship_type'], row['source_entity_id'],
                      row['target_entity_id']): (row['status'], row['raw_relationship_type'])
                for row in db.execute('''SELECT r.relationship_type,r.source_entity_id,
                        r.target_entity_id,r.status,e.raw_relationship_type
                    FROM media_relationships r JOIN media_relationship_evidence e
                      ON e.relationship_id=r.id
                    WHERE e.provider IN ('tmdb_collection','tmdb_movie') ''')}
            watch_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        collection_entity = entities['701']
        second_film_entity = entities['992']
        self.assertEqual(edges[('part_of', root_entity, collection_entity)],
                         ('candidate', 'belongs_to_collection'))
        self.assertEqual(edges[('contains', collection_entity, second_film_entity)],
                         ('candidate', 'parts'))
        self.assertEqual(watch_count, 1)

    def test_steam_watch_saves_its_us_platform_release_observation(self):
        steam_details = {'name': 'Steam Release', 'type': 'game',
                         'release_date': {'date': 'June 01, 2030',
                                          'coming_soon': True},
                         'dlc': []}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'game', 'Steam Release', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game',
                'catalog_provider': 'Steam',
                'external_ids': [{'provider': 'steam', 'external_id': '501'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_steam_game', return_value=steam_details):
                process_due_watches(db, folder, force=True)
            release = db.execute('''SELECT release_date,region,platform,status
                FROM media_releases WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='Steam' AND release_type='platform_launch' ''', (watch_id,)).fetchone()

        self.assertEqual(dict(release), {
            'release_date': '2030-06-01', 'region': 'US',
            'platform': 'Steam', 'status': 'upcoming',
        })

    def test_steam_month_precision_does_not_schedule_a_fabricated_release_day(self):
        steam_details = {'name': 'Steam Release', 'type': 'game',
                         'release_date': {'date': 'October 2030', 'coming_soon': True},
                         'dlc': []}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'game', 'Steam Release', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game',
                'catalog_provider': 'Steam',
                'external_ids': [{'provider': 'steam', 'external_id': '502'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_steam_game', return_value=steam_details):
                process_due_watches(db, folder, force=True)
            saved = db.execute('SELECT state,release_date,metadata FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()
            queue = db.execute('SELECT state,metadata FROM items WHERE url=?',
                               (f'watch-search://{watch_id}',)).fetchone()
            release = db.execute('''SELECT release_date,status,metadata_json FROM media_releases
                WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='Steam' AND release_type='platform_launch' ''',
                                 (watch_id,)).fetchone()

        self.assertEqual(saved['state'], 'upcoming')
        self.assertIsNone(saved['release_date'])
        self.assertEqual(json.loads(saved['metadata'])['steam_details']['release_text'],
                         'october 2030')
        self.assertEqual(queue['state'], 'upcoming')
        self.assertIsNone(json.loads(queue['metadata'])['release_date'])
        self.assertIsNone(release['release_date'])
        self.assertEqual(release['status'], 'upcoming')

    def test_igdb_and_steam_platform_date_disagreement_enters_review(self):
        igdb_date = dt.datetime(2030, 6, 1, tzinfo=dt.timezone.utc)
        igdb_details = {
            'id': 100, 'name': 'Shared Catalog Game',
            'first_release_date': int(igdb_date.timestamp()),
            'release_dates': [{'id': 9010, 'date': int(igdb_date.timestamp()),
                               'platform': {'name': 'Steam'},
                               'release_region': {'name': 'US'},
                               'status': {'name': 'Full release'}}],
            'dlcs': [], 'expansions': [], 'standalone_expansions': [],
            'remakes': [], 'remasters': [], 'ports': [],
        }
        steam_details = {'name': 'Shared Catalog Game', 'type': 'game',
                         'release_date': {'date': 'June 10, 2030',
                                          'coming_soon': True}, 'dlc': []}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            add_watch(db, 'game', 'Shared Catalog Game', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game',
                'catalog_provider': 'IGDB',
                'external_ids': [
                    {'provider': 'igdb', 'external_id': '100'},
                    {'provider': 'steam', 'external_id': '501'},
                ], 'sources': {},
            })
            with patch('media_intake.watched.fetch_igdb_game', return_value=igdb_details), patch(
                    'media_intake.watched.fetch_steam_game', return_value=steam_details):
                process_due_watches(
                    db, folder, force=True,
                    source_options={'igdb_client_id': 'fixture',
                                    'igdb_client_secret': 'fixture'})
            review = db.execute('''SELECT detail_json FROM media_review_queue
                WHERE issue_type='conflicting_release_dates' AND status='open'
                ORDER BY id DESC LIMIT 1''').fetchone()
            details = json.loads(review['detail_json']) if review else {}
            steam_observation = next((claim for claim in details.get('claims', [])
                                      if claim.get('provider') == 'Steam'), None)
            review_id = db.execute('''SELECT id FROM media_review_queue
                WHERE issue_type='conflicting_release_dates' AND status='open'
                ORDER BY id DESC LIMIT 1''').fetchone()
            selected = set_reviewed_release_date(db, review_id['id'], steam_observation['id'])
            with patch('media_intake.watched.fetch_igdb_game', return_value=igdb_details), patch(
                    'media_intake.watched.fetch_steam_game', return_value=steam_details):
                process_due_watches(
                    db, folder, force=True,
                    source_options={'igdb_client_id': 'fixture',
                                    'igdb_client_secret': 'fixture'})
            entity = db.execute('''SELECT e.release_date,e.metadata_json FROM watched_items w
                JOIN media_entities e ON e.id=w.media_entity_id WHERE w.kind='game'
                ORDER BY w.id LIMIT 1''').fetchone()
            canonical_release = db.execute('''SELECT provider,release_date,is_canonical
                FROM media_releases WHERE entity_id=(SELECT media_entity_id FROM watched_items
                WHERE kind='game' ORDER BY id LIMIT 1) AND release_type='platform_launch'
                AND is_canonical=1''').fetchone()

        self.assertIsNotNone(review)
        self.assertEqual(details['region'], 'US')
        self.assertEqual(details['platform'], 'Steam')
        self.assertEqual({claim['provider'] for claim in details['claims']},
                         {'IGDB', 'Steam'})
        self.assertEqual({claim['release_date'] for claim in details['claims']},
                         {'2030-06-01', '2030-06-10'})
        self.assertEqual(selected['release_date'], '2030-06-10')
        self.assertEqual(entity['release_date'], '2030-06-10')
        self.assertEqual(canonical_release['provider'], 'Steam')
        self.assertEqual(canonical_release['release_date'], '2030-06-10')
        self.assertEqual(canonical_release['is_canonical'], 1)

    def test_steam_watch_can_auto_add_dlc_with_its_own_release_date(self):
        future = (dt.date.today() + dt.timedelta(days=21)).isoformat()
        root_details = {'name': 'Steam Game', 'type': 'game', 'release_date': {
            'date': '2024-01-01', 'coming_soon': False}, 'dlc': [501]}
        dlc_details = {'name': 'Steam Game: Expansion', 'type': 'dlc', 'release_date': {
            'date': future, 'coming_soon': True}}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            set_watch_policy(db, 'media_type', 'game', {'game_dlc': 'auto_add'})
            db.commit()
            root_id = add_watch(db, 'game', 'Steam Game', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game',
                'catalog_provider': 'Steam',
                'external_ids': [{'provider': 'steam', 'external_id': '500'}],
                'sources': {},
            })
            with patch('media_intake.watched.fetch_steam_game',
                       side_effect=[root_details, dlc_details]):
                process_due_watches(db, folder, force=True)
            rows = [dict(row) for row in db.execute(
                'SELECT id,kind,title,state,parent_id,release_date,metadata FROM watched_items ORDER BY id')]
            release = db.execute('''SELECT release_date,status FROM media_releases
                WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE title=?)
                  AND provider='Steam' AND release_type='game_dlc' ''',
                                 ('Steam Game: Expansion',)).fetchone()
            queued = [json.loads(row['metadata']) for row in list_items(db)]

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['title'], 'Steam Game: Expansion')
        self.assertEqual(rows[1]['kind'], 'game')
        self.assertEqual(rows[1]['state'], 'upcoming')
        self.assertEqual(rows[1]['parent_id'], root_id)
        self.assertEqual(rows[1]['release_date'], future)
        self.assertEqual(json.loads(rows[1]['metadata'])['game_subtype'], 'dlc')
        self.assertEqual(release['release_date'], future)
        self.assertEqual(release['status'], 'upcoming')
        self.assertTrue(any(item.get('watch_search') and item.get('watch_id') == rows[1]['id']
                            for item in queued))

    def test_video_feed_baselines_existing_entries_then_adds_new_episodes(self):
        today = dt.date.today()
        old_entry = {'title': 'Existing episode', 'url': 'https://video.example/old',
                     'published': (today - dt.timedelta(days=20)).isoformat()}
        upcoming_entry = {'title': 'Announced episode', 'url': 'https://video.example/upcoming',
                          'published': (today + dt.timedelta(days=20)).isoformat()}
        new_entry = {'title': 'New episode', 'url': 'https://video.example/new',
                     'published': today.isoformat()}
        initial_feed = {'title': 'Example Web Series', 'author': 'Creator',
                        'chapters': [old_entry, upcoming_entry]}
        updated_feed = {'title': 'Example Web Series', 'author': 'Creator',
                        'chapters': [old_entry, upcoming_entry, new_entry]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = add_watch(db, 'video', 'Example Web Series',
                                source_url='https://video.example/feed.xml', metadata={
                                    'entity_type': 'series', 'video_subtype': 'web_series',
                                    'catalog_provider': 'Video feed',
                                    'video_feed_url': 'https://video.example/feed.xml',
                                    'sources': {},
                                })
            with patch('media_intake.watched.discover_feed', side_effect=[initial_feed, updated_feed]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            rows = [dict(row) for row in db.execute(
                'SELECT kind,title,state,release_date,parent_id FROM watched_items ORDER BY id')]
            releases = db.execute('''SELECT COUNT(*) AS count FROM media_releases
                WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='video_feed' ''', (root_id,)).fetchone()['count']
            episode_edges = db.execute('''SELECT COUNT(*) FROM media_relationships
                WHERE relationship_type='episode_of' AND target_entity_id=(
                    SELECT media_entity_id FROM watched_items WHERE id=?)''', (root_id,)).fetchone()[0]

        self.assertEqual([row['title'] for row in rows],
                         ['Example Web Series', 'Announced episode', 'New episode'])
        self.assertEqual([row['kind'] for row in rows], ['video', 'video_episode', 'video_episode'])
        self.assertEqual(rows[1]['state'], 'upcoming')
        self.assertEqual(rows[1]['parent_id'], root_id)
        self.assertEqual(rows[2]['parent_id'], root_id)
        self.assertEqual(releases, 3)
        self.assertEqual(episode_edges, 3)

    def test_video_feed_special_policy_routes_bonus_entries_to_review(self):
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        today = dt.date.today().isoformat()
        old_entry = {'title': 'Existing Episode', 'url': 'https://video.example/old',
                     'published': yesterday}
        bonus_entry = {'title': 'Behind the Scenes', 'url': 'https://video.example/bonus',
                       'published': today, 'categories': ['bonus']}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            set_watch_policy(db, 'media_type', 'video', {
                'episodes': 'auto_add', 'specials': 'discover'})
            db.commit()
            root_id = add_watch(db, 'video', 'Example Creator Series',
                                source_url='https://video.example/feed.xml', metadata={
                                    'entity_type': 'series', 'video_subtype': 'web_series',
                                    'catalog_provider': 'Video feed',
                                    'video_feed_url': 'https://video.example/feed.xml',
                                    'sources': {},
                                })
            with patch('media_intake.watched.discover_feed', side_effect=[
                    {'title': 'Example Creator Series', 'author': 'Creator',
                     'chapters': [old_entry]},
                    {'title': 'Example Creator Series', 'author': 'Creator',
                     'chapters': [old_entry, bonus_entry]},
            ]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            children = list(db.execute(
                'SELECT id FROM watched_items WHERE parent_id=?', (root_id,)))
            relationship = db.execute('''SELECT r.relationship_type,r.status
                FROM media_relationships r
                JOIN media_external_ids x ON x.entity_id=r.source_entity_id
                WHERE r.target_entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND x.provider='video_feed_episode' AND x.external_id=?''',
                (root_id, hashlib.sha256(bonus_entry['url'].encode()).hexdigest())).fetchone()
            review = next(item for item in list_graph_reviews(db)
                          if item['issue_type'] == 'possible_feed_episode')

        self.assertEqual(children, [])
        self.assertEqual(dict(relationship), {'relationship_type': 'special_of',
                                              'status': 'candidate'})
        self.assertEqual(review['detail']['feed_entry_type'], 'bonus')

    def test_explicit_creator_feed_episode_number_merges_with_catalog_episode(self):
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        today = dt.date.today().isoformat()
        baseline_feed = {'title': 'Example Web Series', 'author': 'Creator', 'chapters': [
            {'title': 'Example Web Series S01E01 - Pilot',
             'url': 'https://creator.example/episode-1', 'published': yesterday},
        ]}
        updated_feed = {'title': 'Example Web Series', 'author': 'Creator', 'chapters': [
            *baseline_feed['chapters'],
            {'title': 'The Next Part', 'url': 'https://creator.example/episode-2',
             'published': today, 'provider_metadata': {
                 'provider': 'podcast_rss', 'season_number': 1, 'episode_number': 2,
                 'duration': '22:15',
                 'enclosures': [{'url': 'https://cdn.creator.example/episode-2.mp4',
                                 'mime_type': 'video/mp4'}]}},
        ]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = add_watch(db, 'video', 'Example Web Series',
                                source_url='https://creator.example/feed.xml', metadata={
                                    'entity_type': 'series', 'video_subtype': 'web_series',
                                    'video_feed_url': 'https://creator.example/feed.xml',
                                    'sources': {},
                                })
            root_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?', (root_id,)).fetchone()[0])
            catalog_episode_id = add_watch(
                db, 'video_episode', 'Example Web Series S01E02 - The Next Part',
                source_url='https://catalog.example/episode/102',
                metadata={'entity_type': 'work', 'video_subtype': 'episode', 'sources': {},
                          'external_ids': [{'provider': 'media-intake-episode',
                                            'external_id': f'{root_entity_id}:S01E02'}]},
                parent_id=root_id)
            catalog_entity_id = int(db.execute(
                'SELECT media_entity_id FROM watched_items WHERE id=?',
                (catalog_episode_id,)).fetchone()[0])
            with patch('media_intake.watched.discover_feed',
                       side_effect=[baseline_feed, updated_feed]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            episodes = [dict(row) for row in db.execute('''SELECT id,title,media_entity_id,parent_id,metadata
                FROM watched_items WHERE kind='video_episode' ''')]
            linked_video_id = db.execute('''SELECT 1 FROM media_external_ids
                WHERE entity_id=? AND provider='video_feed_episode' ''',
                                         (episodes[0]['media_entity_id'],)).fetchone()

        self.assertEqual(_feed_episode_position('Example Web Series S01E02 - Pilot'), (1, 2))
        self.assertEqual(_feed_episode_position('Example Web Series 1x02 - Pilot'), (1, 2))
        self.assertEqual(_feed_episode_position('The Next Part', {
            'season_number': 1, 'episode_number': 2}), (1, 2))
        self.assertEqual(_feed_episode_position('Episode 2'), None)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]['id'], catalog_episode_id)
        self.assertEqual(episodes[0]['parent_id'], root_id)
        self.assertEqual(episodes[0]['media_entity_id'], catalog_entity_id)
        self.assertEqual(json.loads(episodes[0]['metadata'])['episode']['provider_metadata'][
            'season_number'], 1)
        self.assertEqual(json.loads(episodes[0]['metadata'])['episode']['provider_metadata'][
            'enclosures'][0]['mime_type'], 'video/mp4')
        self.assertIsNotNone(linked_video_id)

    def test_youtube_channel_uploads_need_review_and_acceptance_adds_episode(self):
        today = dt.date.today()
        channel_id = 'UC' + ('a' * 22)
        channel_feed = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
        existing = {'title': 'Existing upload', 'url': 'https://www.youtube.com/watch?v=old',
                    'published': (today - dt.timedelta(days=15)).isoformat()}
        new_upload = {'title': 'Unrelated channel upload',
                      'url': 'https://www.youtube.com/watch?v=new',
                      'published': today.isoformat()}
        initial = {'title': 'Creator channel', 'chapters': [existing]}
        updated = {'title': 'Creator channel', 'chapters': [existing, new_upload]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = add_watch(db, 'video', 'Creator channel',
                                source_url=channel_feed,
                                metadata={'entity_type': 'series', 'video_subtype': 'web_series',
                                          'catalog_provider': 'Video feed',
                                          'video_feed_url': channel_feed,
                                          'sources': {}})
            with patch('media_intake.watched.discover_feed', side_effect=[initial, updated]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            rows_before_review = [dict(row) for row in db.execute(
                'SELECT kind,title FROM watched_items ORDER BY id')]
            review = dict(db.execute('''SELECT id,issue_type,title,detail_json FROM media_review_queue
                WHERE status='open' ORDER BY id DESC LIMIT 1''').fetchone())
            edge = db.execute('''SELECT r.status,e.payload_json FROM media_relationships AS r
                JOIN media_relationship_evidence AS e ON e.relationship_id=r.id
                WHERE r.source_entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND e.provider='video_feed_episode' ''', (root_id,)).fetchone()
            resolve_graph_review(db, review['id'], 'accepted')
            rows_after_accept = [dict(row) for row in db.execute(
                'SELECT id,kind,title,parent_id FROM watched_items ORDER BY id')]
            accepted_edge = db.execute('''SELECT r.status FROM media_relationships AS r
                JOIN media_relationship_evidence AS e ON e.relationship_id=r.id
                WHERE r.source_entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND r.target_entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND e.provider='video_feed_episode' ''',
                                           (root_id, rows_after_accept[1]['id'])).fetchone()
            queue = [{'state': row['state'], **json.loads(row['metadata'])}
                     for row in list_items(db)]

        self.assertEqual(rows_before_review, [{'kind': 'video', 'title': 'Creator channel'}])
        self.assertEqual(review['issue_type'], 'possible_feed_episode')
        self.assertIn('Unrelated channel upload', review['title'])
        self.assertEqual(edge['status'], 'candidate')
        self.assertEqual(len(rows_after_accept), 2)
        self.assertEqual(rows_after_accept[1]['kind'], 'video_episode')
        self.assertEqual(rows_after_accept[1]['title'], 'Unrelated channel upload')
        self.assertEqual(rows_after_accept[1]['parent_id'], root_id)
        self.assertEqual(accepted_edge['status'], 'accepted')
        self.assertTrue(any(item.get('watch_search') and
                            item.get('watch_id') == rows_after_accept[1]['id']
                            for item in queue))

    def test_creator_feed_native_ids_prevent_duplicate_watches_when_urls_change(self):
        feed_url = 'https://creator.example/feed.json'
        today = dt.date.today().isoformat()
        empty = {'title': 'Creator Show', 'chapters': []}
        original = {'title': 'Creator Show', 'chapters': [{
            'title': 'Episode One', 'url': 'https://creator.example/episodes/1',
            'published': today,
            'provider_metadata': {'provider': 'json_feed', 'entry_id': 'episode-one'},
        }]}
        moved = {'title': 'Creator Show', 'chapters': [{
            'title': 'Episode One', 'url': 'https://creator.example/watch/episode-one',
            'published': today,
            'provider_metadata': {'provider': 'json_feed', 'entry_id': 'episode-one'},
        }]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            parent_id = add_watch(db, 'video', 'Creator Show', metadata={
                'entity_type': 'series', 'video_subtype': 'web_series',
                'video_feed_url': feed_url, 'sources': {},
            })
            set_watch_policy(db, 'media_type', 'video', {'episodes': 'auto_add'})
            with patch('media_intake.watched.discover_feed',
                       side_effect=[empty, original, moved]):
                for _ in range(3):
                    process_due_watches(db, folder, force=True)
            children = list(db.execute('''SELECT id,title,parent_id FROM watched_items
                WHERE parent_id=? AND kind='video_episode' ''', (parent_id,)))
            stable_id = hashlib.sha256(
                (feed_url + '\njson_feed\nepisode-one').encode()).hexdigest()
            linked_ids = db.execute('''SELECT COUNT(*) FROM media_external_ids
                WHERE provider='video_feed_episode' AND external_id=?''',
                                    (stable_id,)).fetchone()[0]

        self.assertEqual(len(children), 1)
        self.assertEqual((children[0]['title'], children[0]['parent_id']),
                         ('Episode One', parent_id))
        self.assertEqual(linked_ids, 1)

    def test_direct_calendar_episode_feed_uses_start_date_not_announcement_timestamp(self):
        scheduled_date = (dt.date.today() + dt.timedelta(days=18)).isoformat()
        event = {'title': 'Episode One', 'url': 'https://creator.example/episode-one',
                 'published': '2026-01-01T12:00:00+00:00',
                 'categories': ['episode'],
                 'provider_metadata': {
                     'provider': 'ical', 'entry_id': 'episode-one',
                     'schedule_date': scheduled_date,
                 }}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            parent_id = add_watch(db, 'video', 'Creator Show', metadata={
                'entity_type': 'series', 'video_subtype': 'web_series',
                'video_feed_url': 'https://creator.example/episodes.ics', 'sources': {},
            })
            set_watch_policy(db, 'media_type', 'video', {'episodes': 'auto_add'})
            with patch('media_intake.watched.discover_feed', return_value={
                    'title': 'Creator Schedule', 'chapters': [event]}):
                process_due_watches(db, folder, force=True)
            child = db.execute('''SELECT id,title,release_date,state,metadata FROM watched_items
                WHERE parent_id=? AND kind='video_episode' ''', (parent_id,)).fetchone()
            queued = [item for item in list_items(db)
                      if json.loads(item['metadata']).get('watch_search') and
                      json.loads(item['metadata']).get('title') == event['title']]

        self.assertEqual(child['release_date'], scheduled_date)
        self.assertEqual(child['state'], 'upcoming')
        self.assertEqual(json.loads(child['metadata'])['episode']['airdate'], scheduled_date)
        self.assertEqual(len(queued), 1)

    def test_podcast_trailer_is_reviewed_instead_of_auto_added_as_episode(self):
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        today = dt.date.today().isoformat()
        episode = {'title': 'Regular Podcast Episode',
                   'url': 'https://podcast.example/episode-1', 'published': yesterday,
                   'provider_metadata': {'provider': 'podcast_rss',
                                         'season_number': 1, 'episode_number': 1}}
        trailer = {'title': 'Trailer: Season Two',
                   'url': 'https://podcast.example/trailer-2', 'published': today,
                   'provider_metadata': {'provider': 'podcast_rss',
                                         'episode_type': 'trailer'}}
        initial = {'title': 'Stagecraft Podcast', 'author': 'Example Studio',
                   'chapters': [episode]}
        updated = {'title': 'Stagecraft Podcast', 'author': 'Example Studio',
                   'chapters': [episode, trailer]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = add_watch(db, 'video', 'Stagecraft Podcast',
                                source_url='https://podcast.example/feed.xml', metadata={
                                    'entity_type': 'series', 'video_subtype': 'video_podcast',
                                    'video_feed_url': 'https://podcast.example/feed.xml',
                                    'sources': {},
                                })
            with patch('media_intake.watched.discover_feed', side_effect=[initial, updated]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            rows_before_accept = [dict(row) for row in db.execute(
                'SELECT kind,title FROM watched_items ORDER BY id')]
            review = dict(db.execute('''SELECT id,issue_type,detail_json FROM media_review_queue
                WHERE status='open' ORDER BY id DESC LIMIT 1''').fetchone())
            resolve_graph_review(db, review['id'], 'accepted')
            trailer_watch = dict(db.execute('''SELECT kind,title,parent_id,metadata FROM watched_items
                WHERE kind='video_episode' ''').fetchone())

        self.assertEqual(rows_before_accept, [{'kind': 'video', 'title': 'Stagecraft Podcast'}])
        self.assertEqual(review['issue_type'], 'possible_feed_episode')
        self.assertEqual(json.loads(review['detail_json'])['feed_entry_type'], 'trailer')
        self.assertEqual(trailer_watch['title'], 'Trailer: Season Two')
        self.assertEqual(trailer_watch['parent_id'], root_id)
        self.assertEqual(json.loads(trailer_watch['metadata'])['episode']['feed_entry_type'],
                         'trailer')

    def test_youtube_playlist_page_is_converted_to_playlist_rss(self):
        playlist_page = 'https://www.youtube.com/playlist?list=PLfixture_123'
        playlist_feed = ('https://www.youtube.com/feeds/videos.xml?'
                         'playlist_id=PLfixture_123')
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Playlist Series',
                                 source_url=playlist_page, metadata={
                                     'entity_type': 'series', 'video_subtype': 'web_series',
                                     'catalog_provider': 'Video feed',
                                     'video_feed_url': playlist_page, 'sources': {},
                                 })
            with patch('media_intake.watched.discover_feed', return_value={
                    'title': 'Example Playlist Series', 'chapters': []}) as discover:
                process_due_watches(db, folder, force=True)
            metadata = json.loads(db.execute(
                'SELECT metadata FROM watched_items WHERE id=?', (watch_id,)).fetchone()['metadata'])

        discover.assert_called_once_with(playlist_feed, 'webfiction', limit=500)
        self.assertEqual(metadata['video_feed_url'], playlist_feed)
        self.assertEqual(metadata['video_feed_membership'], 'youtube_playlist')

    def test_youtube_data_api_tracks_playlist_item_metadata_and_removals(self):
        playlist_id = 'PLfixture_123'
        feed_url = f'https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}'

        def item(item_id, video_id, title, position):
            return {'playlist_item_id': item_id, 'video_id': video_id,
                    'playlist_id': playlist_id, 'title': title, 'position': position,
                    'channel_id': 'UC' + ('a' * 22), 'privacy_status': 'public'}

        first = item('item-1', 'video_1', 'Episode One', 0)
        second = item('item-2', 'video_2', 'Episode Two', 1)
        baseline = {
            'youtube_playlist_id': playlist_id, 'snapshot_complete': True,
            'youtube_playlist_items': [first, second],
            'chapters': [
                {'title': first['title'], 'url': 'https://www.youtube.com/watch?v=video_1',
                 'published': '2025-01-01',
                 'provider_metadata': {key: value for key, value in first.items()
                                       if key not in {'title', 'playlist_id'}}},
                {'title': second['title'], 'url': 'https://www.youtube.com/watch?v=video_2',
                 'published': '2025-02-01',
                 'provider_metadata': {key: value for key, value in second.items()
                                       if key not in {'title', 'playlist_id'}}},
            ],
        }
        refreshed = {**baseline, 'youtube_playlist_items': [second],
                     'chapters': [baseline['chapters'][1]]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Playlist Series',
                                 source_url=feed_url, metadata={
                                     'entity_type': 'series', 'video_subtype': 'web_series',
                                     'catalog_provider': 'Video feed',
                                     'video_feed_url': feed_url, 'sources': {},
                                 })
            with patch('media_intake.watched.fetch_youtube_playlist_items',
                       side_effect=[baseline, refreshed]) as fetch_playlist:
                process_due_watches(db, folder, force=True,
                                    source_options={'youtube_api_key': 'fixture-key'})
                process_due_watches(db, folder, force=True,
                                    source_options={'youtube_api_key': 'fixture-key'})
            add_watch(db, 'video', 'Example Playlist Series',
                      source_url=feed_url, metadata={
                          'entity_type': 'series', 'video_subtype': 'web_series',
                          'catalog_provider': 'Video feed',
                          'video_feed_url': feed_url, 'sources': {},
                      })
            review = db.execute('''SELECT issue_type,detail_json FROM media_review_queue
                WHERE issue_type='playlist_item_removed' AND status='open' ''').fetchone()
            removal = db.execute('''SELECT status,provider_release_id,metadata_json FROM media_releases
                WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND release_type='playlist_item_removed' ''', (watch_id,)).fetchone()
            metadata = json.loads(db.execute(
                'SELECT metadata FROM watched_items WHERE id=?', (watch_id,)).fetchone()['metadata'])

        fetch_playlist.assert_has_calls([
            call(playlist_id, 'fixture-key'),
            call(playlist_id, 'fixture-key'),
        ])
        self.assertIsNotNone(review)
        self.assertEqual(json.loads(review['detail_json'])['playlist_item_id'], 'item-1')
        self.assertIn('complete YouTube Data API playlist snapshot',
                      json.loads(review['detail_json'])['evidence'])
        self.assertEqual((removal['status'], removal['provider_release_id']), ('removed', 'item-1'))
        self.assertEqual(json.loads(removal['metadata_json'])['title'], 'Episode One')
        self.assertEqual(metadata['youtube_playlist_state']['complete'], True)
        self.assertEqual(set(metadata['youtube_playlist_state']['items']), {'item-2'})
        self.assertEqual(metadata['video_feed_item_metadata'][
            'https://www.youtube.com/watch?v=video_2']['provider_metadata']['video_id'], 'video_2')

    def test_youtube_scheduled_premiere_waits_until_release_date_to_search(self):
        playlist_id = 'PLfixture_123'
        feed_url = f'https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}'
        future_date = (dt.date.today() + dt.timedelta(days=12)).isoformat()
        playlist_item = {'playlist_item_id': 'item-future', 'video_id': 'video_future',
                         'playlist_id': playlist_id, 'title': 'Future Premiere',
                         'position': 0, 'scheduled_start_at': future_date + 'T18:00:00Z'}
        manifest = {
            'youtube_playlist_id': playlist_id, 'snapshot_complete': True,
            'youtube_playlist_items': [playlist_item],
            'chapters': [{'title': playlist_item['title'],
                          'url': 'https://www.youtube.com/watch?v=video_future',
                          'published': playlist_item['scheduled_start_at'],
                          'provider_metadata': playlist_item}],
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            root_id = add_watch(db, 'video', 'Premiere Series', source_url=feed_url,
                                metadata={'entity_type': 'series', 'video_subtype': 'web_series',
                                          'video_feed_url': feed_url, 'sources': {}})
            with patch('media_intake.watched.fetch_youtube_playlist_items', return_value=manifest):
                process_due_watches(db, folder, force=True,
                                    source_options={'youtube_api_key': 'fixture-key'})
            episode = dict(db.execute('''SELECT id,state,release_date FROM watched_items
                WHERE kind='video_episode' AND parent_id=?''', (root_id,)).fetchone())
            queue = [{'state': row['state'], **json.loads(row['metadata'])}
                     for row in list_items(db)]

        self.assertEqual(episode['state'], 'upcoming')
        self.assertEqual(episode['release_date'], future_date)
        self.assertTrue(any(item.get('watch_search') and
                            item.get('watch_id') == episode['id'] and
                            item.get('state') == 'upcoming' and
                            item.get('publication_status') == 'upcoming' for item in queue))

    def test_youtube_playlist_api_failure_falls_back_without_inferring_removals(self):
        playlist_page = 'https://www.youtube.com/playlist?list=PLfixture_123'
        playlist_feed = ('https://www.youtube.com/feeds/videos.xml?'
                         'playlist_id=PLfixture_123')
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Playlist',
                                 source_url=playlist_page, metadata={
                                     'entity_type': 'series', 'video_subtype': 'web_series',
                                     'video_feed_url': playlist_page, 'sources': {},
                                     'youtube_playlist_state': {
                                         'complete': True, 'playlist_id': 'PLfixture_123',
                                         'items': {'old-item': {'title': 'Known item'}},
                                     },
                                 })
            with patch('media_intake.watched.fetch_youtube_playlist_items',
                       side_effect=ValueError('fixture API unavailable')), patch(
                    'media_intake.watched.discover_feed', return_value={
                        'title': 'Example Playlist', 'chapters': []}) as discover:
                process_due_watches(db, folder, force=True,
                                    source_options={'youtube_api_key': 'fixture-key'})
            metadata = json.loads(db.execute(
                'SELECT metadata FROM watched_items WHERE id=?', (watch_id,)).fetchone()['metadata'])
            removals = db.execute('''SELECT COUNT(*) FROM media_review_queue
                WHERE issue_type='playlist_item_removed' ''').fetchone()[0]

        discover.assert_called_once_with(playlist_feed, 'webfiction', limit=500)
        self.assertIn('fixture API unavailable', metadata['youtube_playlist_api_error'])
        self.assertEqual(set(metadata['youtube_playlist_state']['items']), {'old-item'})
        self.assertEqual(removals, 0)

    def test_readding_video_feed_watch_keeps_episode_baseline_and_seen_ids(self):
        today = dt.date.today().isoformat()
        old = {'title': 'Already listed', 'url': 'https://video.example/old',
               'published': (dt.date.today() - dt.timedelta(days=30)).isoformat()}
        new = {'title': 'New episode', 'url': 'https://video.example/new',
               'published': today}
        initial = {'title': 'Example Feed', 'chapters': [old]}
        updated = {'title': 'Example Feed', 'chapters': [old, new]}
        feed_url = 'https://video.example/feed.xml'
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            metadata = {'entity_type': 'series', 'video_subtype': 'web_series',
                        'catalog_provider': 'Video feed', 'video_feed_url': feed_url,
                        'sources': {}}
            root_id = add_watch(db, 'video', 'Example Feed', source_url=feed_url,
                                metadata=metadata)
            with patch('media_intake.watched.discover_feed', return_value=initial):
                process_due_watches(db, folder, force=True)
            add_watch(db, 'video', 'Example Feed', source_url=feed_url,
                      metadata={**metadata, 'video_feed_known_episodes': [],
                                'video_feed_watched_episodes': [],
                                'video_feed_seen_at': {}})
            with patch('media_intake.watched.discover_feed', return_value=updated):
                process_due_watches(db, folder, force=True)
            rows = [dict(row) for row in db.execute('''SELECT kind,title,parent_id
                FROM watched_items ORDER BY id''')]
            saved = json.loads(db.execute('SELECT metadata FROM watched_items WHERE id=?',
                                          (root_id,)).fetchone()['metadata'])

        self.assertEqual(rows, [
            {'kind': 'video', 'title': 'Example Feed', 'parent_id': None},
            {'kind': 'video_episode', 'title': 'New episode', 'parent_id': root_id},
        ])
        self.assertTrue(saved['video_feed_baselined_at'])
        self.assertEqual(set(saved['video_feed_known_episodes']), {old['url'], new['url']})
        self.assertEqual(saved['video_feed_seen_at'][old['url']],
                         saved['video_feed_baselined_at'])

    def test_announcement_feed_baselines_old_posts_and_reviews_new_posts(self):
        today = dt.date.today()
        old = {'title': 'Old publisher news', 'url': 'https://studio.example/news/old',
               'published': (today - dt.timedelta(days=30)).isoformat()}
        new = {'title': 'New expansion announced', 'url': 'https://studio.example/news/new',
               'published': today.isoformat()}
        initial = {'title': 'Studio News', 'chapters': [old]}
        refreshed = {'title': 'Studio News', 'chapters': [old, new]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'game', 'Example Game', metadata={
                'entity_type': 'work', 'game_subtype': 'base_game',
                'announcement_feed_urls': ['https://studio.example/news.xml'],
                'sources': {},
            })
            with patch('media_intake.watched.discover_feed',
                       side_effect=[initial, refreshed]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            reviews = [dict(row) for row in db.execute('''SELECT issue_type,title,detail_json
                FROM media_review_queue WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                AND status='open' ORDER BY id''', (watch_id,))]
            announcements = [dict(row) for row in db.execute('''SELECT release_type,status,source_url
                FROM media_releases WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                AND provider='announcement_feed' ORDER BY source_url''', (watch_id,))]
            watch_count = db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0]

        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]['issue_type'], 'release_announcement')
        self.assertIn('New expansion announced', reviews[0]['title'])
        self.assertEqual(json.loads(reviews[0]['detail_json'])['feed_url'],
                         'https://studio.example/news.xml')
        self.assertEqual(announcements, [
            {'release_type': 'announcement', 'status': 'announced',
             'source_url': 'https://studio.example/news/new'},
            {'release_type': 'announcement', 'status': 'announced',
             'source_url': 'https://studio.example/news/old'},
        ])
        self.assertEqual(watch_count, 1)

    def test_readding_watch_preserves_announcement_feed_baseline_state(self):
        today = dt.date.today()
        old = {'title': 'Existing news', 'url': 'https://publisher.example/old',
               'published': (today - dt.timedelta(days=5)).isoformat()}
        new = {'title': 'New sequel announcement', 'url': 'https://publisher.example/new',
               'published': today.isoformat()}
        initial = {'title': 'Publisher feed', 'chapters': [old]}
        updated = {'title': 'Publisher feed', 'chapters': [old, new]}
        second_feed = {'title': 'Developer feed', 'chapters': [
            {'title': 'Old unrelated post', 'url': 'https://developer.example/old',
             'published': (today - dt.timedelta(days=40)).isoformat()}]}
        source_url = 'https://store.steampowered.com/app/600/'
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'game', 'Example Game', source_url=source_url,
                                 metadata={'entity_type': 'work', 'game_subtype': 'base_game',
                                           'announcement_feed_urls': ['https://publisher.example/rss'],
                                           'sources': {}})
            with patch('media_intake.watched.discover_feed', return_value=initial):
                process_due_watches(db, folder, force=True)
            add_watch(db, 'game', 'Example Game', source_url=source_url,
                      metadata={'entity_type': 'work', 'game_subtype': 'base_game',
                                'announcement_feed_urls': [
                                    'https://publisher.example/rss',
                                    'https://developer.example/rss'],
                                'sources': {}})
            with patch('media_intake.watched.discover_feed',
                       side_effect=[updated, second_feed]):
                process_due_watches(db, folder, force=True)
            review = db.execute('''SELECT detail_json FROM media_review_queue
                WHERE issue_type='release_announcement' AND status='open' ''').fetchone()
            metadata = json.loads(db.execute(
                'SELECT metadata FROM watched_items WHERE id=?', (watch_id,)).fetchone()['metadata'])

        self.assertIsNotNone(review)
        self.assertEqual(json.loads(review['detail_json'])['source_url'],
                         'https://publisher.example/new')
        self.assertEqual(set(metadata['announcement_feed_state']), {
            'https://publisher.example/rss', 'https://developer.example/rss'})

    def test_announcement_dates_require_schedule_wording_and_preserve_precision(self):
        full = _classify_release_announcement({
            'title': 'Example Show premieres October 5th, 2027',
        })
        month = _extract_announced_release_date('Season 2 premieres in Oct. 2028')
        year = _extract_announced_release_date('Season 2 premieres in 2029')
        streaming = _classify_release_announcement({
            'title': 'Example Movie streaming update',
            'provider_metadata': {
                'summary': 'Episodes will be available on Netflix starting November 5, 2027.'},
        })
        renewal = _classify_release_announcement({
            'title': 'Example Show renewed for season two in October 2028',
        })

        self.assertEqual(full['announced_date'], '2027-10-05')
        self.assertEqual(full['announced_date_precision'], 'day')
        self.assertEqual(full['announced_date_context'], 'premiere')
        self.assertEqual(month['announced_date'], '2028-10')
        self.assertEqual(month['announced_date_precision'], 'month')
        self.assertEqual(year['announced_date'], '2029')
        self.assertEqual(year['announced_date_precision'], 'year')
        self.assertEqual(streaming['announced_date'], '2027-11-05')
        self.assertEqual(streaming['announced_date_context'], 'availability')
        self.assertEqual(streaming['announced_platform'], 'Netflix')
        self.assertEqual(streaming['matched_text_source'], 'summary')
        self.assertEqual(streaming['announced_date_source'], 'announcement summary')
        self.assertEqual(streaming['confidence'], 0.78)
        self.assertNotIn('announced_date', renewal)

    def test_first_scan_future_calendar_event_is_reviewed_and_schedules_the_watch(self):
        scheduled_date = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        entry = {
            'title': 'Example Show premiere',
            'url': 'https://studio.example/premiere',
            'categories': ['premiere'],
            'provider_metadata': {
                'provider': 'ical', 'entry_id': 'premiere-2027',
                'schedule_date': scheduled_date,
                'summary': 'The next season premieres on this date.',
            },
        }
        manifest = {'title': 'Studio Schedule', 'chapters': [entry]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Show', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series',
                'announcement_feed_urls': ['https://studio.example/schedule.ics'],
                'sources': {},
            })
            with patch('media_intake.watched.discover_feed', return_value=manifest):
                process_due_watches(db, folder, force=True)
            review = db.execute('''SELECT id,detail_json FROM media_review_queue
                WHERE issue_type='release_announcement' AND status='open' ''').fetchone()
            details = json.loads(review['detail_json'])
            resolve_graph_review(db, review['id'], 'accepted')
            release = db.execute('''SELECT release_type,release_date,status,metadata_json
                FROM media_releases WHERE provider='announcement_feed'
                  AND release_type='video_premiere' ''').fetchone()
            watch = db.execute('''SELECT release_date,state,next_check_at FROM watched_items
                                WHERE id=?''', (watch_id,)).fetchone()

        self.assertEqual(details['typed_event_candidate']['announced_date'], scheduled_date)
        self.assertEqual(details['typed_event_candidate']['announced_date_source'],
                         'announcement calendar DTSTART')
        self.assertEqual(release['release_date'], scheduled_date)
        self.assertEqual(release['status'], 'upcoming')
        release_metadata = json.loads(release['metadata_json'])
        self.assertEqual(release_metadata['schedule_date'], scheduled_date)
        self.assertEqual(release_metadata['provider_metadata']['entry_id'], 'premiere-2027')
        self.assertEqual(watch['release_date'], scheduled_date)
        self.assertEqual(watch['state'], 'upcoming')
        self.assertEqual(watch['next_check_at'], f'{scheduled_date}T00:00:00')

    def test_calendar_start_date_is_preserved_when_article_text_disagrees(self):
        candidate = _classify_release_announcement({
            'title': 'Example Show premieres October 17, 2028',
            'provider_metadata': {'provider': 'ical', 'schedule_date': '2027-10-17'},
        })

        self.assertEqual(candidate['announced_date'], '2027-10-17')
        self.assertEqual(candidate['announced_date_source'],
                         'announcement calendar DTSTART')
        self.assertEqual(candidate['calendar_date_conflict'], {
            'text_date': '2028-10-17',
            'text_date_source': 'announcement title',
            'calendar_date': '2027-10-17',
            'calendar_date_source': 'announcement calendar DTSTART',
        })

    def test_structured_json_schedule_event_type_and_date_are_normalized(self):
        candidate = _classify_release_announcement({
            'title': 'Example Show launch',
            'provider_metadata': {
                'provider': 'json_schedule', 'event_type': 'premiere',
                'schedule_date': '2030-10-17',
            },
        })

        self.assertEqual(candidate['event_type'], 'premiere_announcement')
        self.assertEqual(candidate['confidence'], 0.96)
        self.assertEqual(candidate['matched_text_source'], 'event_type')
        self.assertEqual(candidate['announced_date'], '2030-10-17')
        self.assertEqual(candidate['announced_date_source'],
                         'announcement calendar DTSTART')

    def test_accepted_calendar_episode_creates_scheduled_child_watch(self):
        scheduled_date = (dt.date.today() + dt.timedelta(days=14)).isoformat()
        entry = {
            'title': 'Example Show: New installment',
            'url': 'https://studio.example/season-one/episode-three',
            'categories': ['episode'],
            'provider_metadata': {
                'provider': 'ical', 'entry_id': 'episode-3',
                'schedule_date': scheduled_date,
                'summary': 'The next installment in the series.',
            },
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            parent_id = add_watch(db, 'video', 'Example Show', metadata={
                'entity_type': 'series', 'video_subtype': 'tv_series',
                'announcement_feed_urls': ['https://studio.example/episodes.ics'],
                'sources': {},
            })
            with patch('media_intake.watched.discover_feed', return_value={
                    'title': 'Episode Calendar', 'chapters': [entry]}):
                process_due_watches(db, folder, force=True)
            review = db.execute('''SELECT id,detail_json FROM media_review_queue
                WHERE issue_type='release_announcement' AND status='open' ''').fetchone()
            details = json.loads(review['detail_json'])
            resolve_graph_review(db, review['id'], 'accepted')
            episode = db.execute('''SELECT id,kind,title,parent_id,release_date,state,metadata
                FROM watched_items WHERE parent_id=?''', (parent_id,)).fetchone()
            queued = [item for item in list_items(db)
                      if json.loads(item['metadata']).get('watch_search') and
                      json.loads(item['metadata']).get('title') == entry['title']]

        self.assertEqual(details['typed_event_candidate']['event_type'],
                         'episode_announcement')
        self.assertEqual((episode['kind'], episode['title'], episode['parent_id']),
                         ('video_episode', entry['title'], parent_id))
        self.assertEqual(episode['release_date'], scheduled_date)
        self.assertEqual(episode['state'], 'upcoming')
        episode_metadata = json.loads(episode['metadata'])
        self.assertEqual(episode_metadata['video_subtype'], 'episode')
        self.assertEqual(episode_metadata['episode']['provider_metadata']['entry_id'],
                         'episode-3')
        self.assertEqual(len(queued), 1)

    def test_accepted_streaming_announcement_keeps_platform_specific_date(self):
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Movie', metadata={
                'entity_type': 'work', 'video_subtype': 'movie', 'sources': {}})
            entity_id = int(db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                       (watch_id,)).fetchone()[0])
            review_id = queue_graph_review(
                db, 'release_announcement', 'Example Movie streaming on Netflix',
                {'media_type': 'video', 'provider': 'studio_feed',
                 'source_url': 'https://studio.example/streaming',
                 'provider_entry_id': 'streaming-1',
                 'typed_event_candidate': {
                     'event_type': 'availability_announcement',
                     'announced_date': '2030-11-05',
                     'announced_date_precision': 'day',
                     'announced_date_context': 'availability',
                     'announced_platform': 'Netflix',
                 }}, entity_id=entity_id)
            resolve_graph_review(db, review_id, 'accepted')
            release = db.execute('''SELECT release_type,release_date,platform,status
                FROM media_releases WHERE entity_id=? AND provider='studio_feed'
                AND release_type='video_streaming' ''', (entity_id,)).fetchone()

        self.assertEqual(dict(release), {
            'release_type': 'video_streaming', 'release_date': '2030-11-05',
            'platform': 'Netflix', 'status': 'upcoming',
        })

    def test_typed_feed_announcement_becomes_graph_event_when_accepted(self):
        old = {'title': 'Old studio update', 'url': 'https://studio.example/news/old'}
        new = {'title': 'Example Show renewed for season three',
               'url': 'https://studio.example/news/renewed', 'categories': ['renewal'],
               'provider_metadata': {
                   'summary': 'Season three premieres on October 17, 2027.'},
               'published': '2026-09-20'}
        initial = {'title': 'Studio News', 'chapters': [old]}
        updated = {'title': 'Studio News', 'chapters': [old, new]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Show',
                                 metadata={'entity_type': 'series',
                                           'video_subtype': 'tv_series',
                                           'announcement_feed_urls': [
                                               'https://studio.example/news.xml'],
                                           'sources': {}})
            with patch('media_intake.watched.discover_feed',
                       side_effect=[initial, updated]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            review = db.execute('''SELECT id,detail_json FROM media_review_queue
                WHERE issue_type='release_announcement' AND status='open'
                ORDER BY id DESC LIMIT 1''').fetchone()
            details = json.loads(review['detail_json'])
            resolve_graph_review(db, review['id'], 'accepted')
            event = db.execute('''SELECT release_type,status,metadata_json FROM media_releases
                WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='announcement_feed' AND release_type='series_renewal' ''',
                               (watch_id,)).fetchone()
            premiere = db.execute('''SELECT release_type,release_date,is_canonical FROM media_releases
                WHERE entity_id=(SELECT media_entity_id FROM watched_items WHERE id=?)
                  AND provider='announcement_feed' AND release_type='video_premiere' ''',
                                  (watch_id,)).fetchone()
            watch = db.execute('SELECT release_date,state,next_check_at,metadata FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()
            entity = db.execute('SELECT status,metadata_json FROM media_entities WHERE id=?',
                                (db.execute('SELECT media_entity_id FROM watched_items WHERE id=?',
                                            (watch_id,)).fetchone()[0],)).fetchone()

        self.assertEqual(details['typed_event_candidate']['event_type'], 'series_renewal')
        self.assertEqual(details['typed_event_candidate']['confidence'], 0.95)
        self.assertEqual(details['typed_event_candidate']['announced_date'], '2027-10-17')
        self.assertEqual(details['typed_event_candidate']['announced_date_source'],
                         'announcement summary')
        self.assertEqual(details['summary'],
                         'Season three premieres on October 17, 2027.')
        self.assertEqual(event['status'], 'announced')
        event_metadata = json.loads(event['metadata_json'])
        self.assertEqual(event_metadata['title'], new['title'])
        self.assertEqual(event_metadata['summary'], new['provider_metadata']['summary'])
        self.assertEqual((premiere['release_type'], premiere['release_date'],
                          premiere['is_canonical']), ('video_premiere', '2027-10-17', 1))
        self.assertEqual(watch['release_date'], '2027-10-17')
        self.assertEqual(watch['state'], 'upcoming')
        self.assertEqual(watch['next_check_at'], '2027-10-17T00:00:00')
        self.assertEqual(json.loads(watch['metadata'])['release_date_source'], 'review')
        self.assertEqual(entity['status'], 'ongoing')
        self.assertEqual(json.loads(entity['metadata_json'])['status_source'], 'review')
        self.assertEqual(json.loads(watch['metadata'])['catalog_status'], 'ongoing')

    def test_accepted_lifecycle_announcements_apply_canonical_series_status(self):
        cases = {
            'series_renewal': 'ongoing',
            'series_cancellation': 'cancelled',
            'series_hiatus': 'hiatus',
        }
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            for event_type, expected_status in cases.items():
                watch_id = add_watch(db, 'video', f'Series {event_type}', metadata={
                    'entity_type': 'series', 'video_subtype': 'tv_series', 'sources': {}})
                entity_id = int(db.execute(
                    'SELECT media_entity_id FROM watched_items WHERE id=?',
                    (watch_id,)).fetchone()[0])
                review_id = queue_graph_review(
                    db, 'release_announcement', f'Review {event_type}',
                    {'provider': 'studio_feed', 'source_url': f'https://studio.example/{event_type}',
                     'provider_entry_id': event_type,
                     'typed_event_candidate': {'event_type': event_type}},
                    entity_id=entity_id, dedupe_key=f'lifecycle:{event_type}')
                resolve_graph_review(db, review_id, 'accepted')
                entity = db.execute('SELECT status FROM media_entities WHERE id=?',
                                    (entity_id,)).fetchone()
                watch_metadata = json.loads(db.execute(
                    'SELECT metadata FROM watched_items WHERE id=?', (watch_id,)).fetchone()[0])
                claim = db.execute('''SELECT value_json,is_canonical FROM media_claim_observations
                    WHERE entity_id=? AND field_name='status' AND provider='media intake review' ''',
                                   (entity_id,)).fetchone()
                with self.subTest(event_type=event_type):
                    self.assertEqual(entity['status'], expected_status)
                    self.assertEqual(watch_metadata['catalog_status'], expected_status)
                    self.assertEqual(watch_metadata['catalog_status_source'], 'review')
                    self.assertEqual(json.loads(claim['value_json']), expected_status)
                    self.assertEqual(claim['is_canonical'], 1)

    def test_announcement_date_conflict_can_be_reviewed_and_scheduled(self):
        old = {'title': 'Old studio update', 'url': 'https://studio.example/news/old'}
        new = {'title': 'Example Show premieres October 17, 2027',
               'url': 'https://studio.example/news/premiere', 'categories': ['premiere'],
               'published': '2026-09-20'}
        initial = {'title': 'Studio News', 'chapters': [old]}
        updated = {'title': 'Studio News', 'chapters': [old, new]}
        with tempfile.TemporaryDirectory() as folder, closing(connect(folder)) as db:
            watch_id = add_watch(db, 'video', 'Example Show', release_date='2027-09-01',
                                 metadata={'entity_type': 'series', 'video_subtype': 'tv_series',
                                           'announcement_feed_urls': [
                                               'https://studio.example/news.xml'],
                                           'sources': {}})
            with patch('media_intake.watched.discover_feed',
                       side_effect=[initial, updated]):
                process_due_watches(db, folder, force=True)
                process_due_watches(db, folder, force=True)
            announcement = next(item for item in list_graph_reviews(db)
                                if item['issue_type'] == 'release_announcement')
            resolve_graph_review(db, announcement['id'], 'accepted')
            date_review = next(item for item in list_graph_reviews(db)
                               if item['issue_type'] == 'conflicting_metadata' and
                               item['detail']['field'] == 'release_date')
            chosen = next(claim for claim in date_review['detail']['claims']
                          if claim['provider'] == 'announcement_feed')
            result = set_reviewed_media_release_date(db, date_review['id'], chosen['id'])
            watch = db.execute('SELECT release_date,state,next_check_at,metadata FROM watched_items WHERE id=?',
                               (watch_id,)).fetchone()

        self.assertEqual(result['release_date'], '2027-10-17')
        self.assertEqual(watch['release_date'], '2027-10-17')
        self.assertEqual(watch['state'], 'upcoming')
        self.assertEqual(watch['next_check_at'], '2027-10-17T00:00:00')
        self.assertEqual(json.loads(watch['metadata'])['release_date_source'], 'review')


if __name__ == '__main__':
    unittest.main()
