"""Podcast following: parsing, back catalogue choices, new-episode checks, downloads and housekeeping."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_intake import podcasts
from media_intake.core import connect, download
from media_intake.rights_evidence import ensure_rights_evidence, load_rights_evidence
from media_intake.webcomics.http import Response

FEED = 'https://pod.example/feed.xml'


def rss(episodes, title='Fixture Show'):
    items = ''.join(
        f'<item><title>Episode {n}</title><guid isPermaLink="false">ep-{n}</guid>'
        f'<pubDate>Mon, {n:02d} Jan 2024 10:00:00 +0000</pubDate>'
        f'<enclosure url="{base}/ep{n}.mp3" length="{1000 + n}" type="audio/mpeg"/>'
        f'<itunes:episode>{n}</itunes:episode><itunes:duration>00:30:00</itunes:duration></item>'
        for n, base in episodes)
    return (f'<?xml version="1.0"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
            f'<channel><title>{title}</title><itunes:author>Host</itunes:author>'
            f'<itunes:image href="https://pod.example/art.jpg"/>'
            f'<item><title>Show notes only</title><guid>text-post</guid></item>{items}</channel></rss>').encode()


class FeedClient:
    def __init__(self, body):
        self.body, self.unchanged, self.calls = body, False, 0

    def get(self, url, **kwargs):
        self.calls += 1
        return Response(self.body, url, {'etag': f'"v{self.calls}"'}, 304 if self.unchanged else 200, self.unchanged)


class ParsingTests(unittest.TestCase):
    def test_parse_feed_and_formats(self):
        show = podcasts.parse_podcast_feed(rss([(1, 'https://cdn.example'), (2, 'https://cdn.example')]), FEED)
        self.assertEqual((show['title'], show['author'], show['image_url']), ('Fixture Show', 'Host', 'https://pod.example/art.jpg'))
        self.assertEqual([(e['guid'], e['episode'], e['published'][:10]) for e in show['episodes']],
                         [('ep-1', 1, '2024-01-01'), ('ep-2', 2, '2024-01-02')])  # text-only post skipped
        self.assertEqual(podcasts.episode_format('https://x.example/a.m4a?x=1'), '.m4a')
        self.assertEqual(podcasts.episode_format('https://x.example/stream', 'audio/mpeg; charset=x'), '.mp3')
        self.assertEqual(podcasts.episode_format('https://x.example/v', 'video/mp4'), '.mp4')
        with self.assertRaises(ValueError):
            podcasts.parse_podcast_feed(b'<html></html>', FEED)

    def test_directory_search(self):
        payload = json.dumps({'results': [
            {'collectionName': 'Fixture Show', 'artistName': 'Host', 'feedUrl': FEED, 'trackCount': 120,
             'primaryGenreName': 'History', 'artworkUrl600': 'https://pod.example/a.jpg'},
            {'collectionName': 'No feed'}]}).encode()
        calls = []
        shows = podcasts.search_directory('fixture', fetch=lambda url: calls.append(url) or (payload, url))
        self.assertIn('media=podcast', calls[0])
        self.assertEqual([(s['title'], s['feed_url'], s['episodes']) for s in shows], [('Fixture Show', FEED, 120)])


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def states(self, show_id):
        return [tuple(r) for r in self.db.execute(
            'SELECT guid, state FROM podcast_episodes WHERE show_id=? ORDER BY published', (show_id,))]

    def test_back_catalogue_choices(self):
        episodes = [(n, 'https://cdn.example') for n in range(1, 6)]
        for backlog, expected in (('none', 0), ('latest', 2), ('all', 5)):
            with self.subTest(backlog=backlog):
                feed = f'https://pod.example/{backlog}.xml'
                show_id = podcasts.follow(self.db, feed, backlog=backlog, latest=2, client=FeedClient(rss(episodes)))
                queued = [guid for guid, state in self.states(show_id) if state == 'queued']
                self.assertEqual(len(queued), expected)
                if backlog == 'latest':
                    self.assertEqual(queued, ['ep-4', 'ep-5'])
        self.assertEqual(podcasts.follow(self.db, 'https://pod.example/all.xml', client=FeedClient(b'')),
                         self.db.execute("SELECT id FROM podcast_shows WHERE feed_url LIKE '%all.xml'").fetchone()[0])

    def test_new_episodes_queue_once_and_unchanged_feeds_do_nothing(self):
        client = FeedClient(rss([(1, 'https://cdn.example'), (2, 'https://cdn.example')]))
        show_id = podcasts.follow(self.db, FEED, backlog='none', client=client)
        client.body = rss([(n, 'https://cdn.example') for n in range(1, 5)])
        self.assertEqual(podcasts.check_show(self.db, show_id, client), 2)
        self.assertEqual(podcasts.check_show(self.db, show_id, client), 0)  # same feed again
        client.unchanged = True
        self.assertEqual(podcasts.check_show(self.db, show_id, client), 0)
        self.assertEqual(self.states(show_id), [('ep-1', 'skipped'), ('ep-2', 'skipped'), ('ep-3', 'queued'), ('ep-4', 'queued')])
        items = [json.loads(r[0]) for r in self.db.execute('SELECT metadata FROM items ORDER BY id')]
        self.assertEqual([(i['title'], i['author'], i['format'], i['library_subfolder']) for i in items],
                         [('2024-01-03 - Episode 3', 'Fixture Show', '.mp3', str(Path('Podcasts') / 'Fixture Show')),
                          ('2024-01-04 - Episode 4', 'Fixture Show', '.mp3', str(Path('Podcasts') / 'Fixture Show'))])
        # Auto-download off: new episodes are recorded but not queued.
        podcasts.update_settings(self.db, show_id, auto_download=False)
        client.unchanged, client.body = False, rss([(n, 'https://cdn.example') for n in range(1, 6)])
        self.assertEqual(podcasts.check_show(self.db, show_id, client), 1)
        self.assertEqual(self.states(show_id)[-1], ('ep-5', 'new'))
        self.assertEqual(podcasts.due_shows(self.db), [])  # rescheduled for later

    def test_download_into_show_folder_keep_latest_and_unfollow(self):
        try:
            from tests.test_transfer import RangeServer
        except ImportError:
            from test_transfer import RangeServer
        with RangeServer(payload=b'ID3' + b'\x00' * 5000, content_type='audio/mpeg') as server:
            base = server.url.rsplit('/', 1)[0]
            client = FeedClient(rss([(n, base) for n in range(1, 4)]))
            show_id = podcasts.follow(self.db, FEED, backlog='all', client=client)
            self.assertTrue(load_rights_evidence(ensure_rights_evidence(Path(self.temp.name) / 'rights-evidence.json'))
                            ['sources']['podcast_rss']['approved_for_download'])
            for (item_id,) in self.db.execute('SELECT item_id FROM podcast_episodes WHERE show_id=?', (show_id,)).fetchall():
                result = download(self.db, item_id, self.temp.name)
                self.assertEqual(Path(result['file_path']).parent, Path(self.temp.name) / 'downloads' / 'Podcasts' / 'Fixture Show')
            self.assertEqual(podcasts.sync_downloads(self.db), [])
            self.assertEqual({state for _guid, state in self.states(show_id)}, {'downloaded'})
            podcasts.update_settings(self.db, show_id, keep_latest=1)
            removed = podcasts.sync_downloads(self.db)
            self.assertEqual(len(removed), 2)
            self.assertFalse(any(Path(path).exists() for path in removed))
            self.assertEqual([state for _guid, state in self.states(show_id)], ['removed', 'removed', 'downloaded'])
            kept = self.db.execute("SELECT file_path FROM items WHERE state='downloaded'").fetchone()[0]
            podcasts.unfollow(self.db, show_id)
            self.assertTrue(Path(kept).exists())
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM podcast_shows').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
