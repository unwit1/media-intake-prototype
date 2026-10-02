import json
import socketserver
import tempfile
import threading
import textwrap
import unittest
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

from media_intake.core import connect, download
from media_intake.serial import discover_feed, enqueue_serial, validate_manifest


PNG = bytes.fromhex('89504e470d0a1a0a') + b'test-image-content'


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/feed':
            data = b'<rss version="2.0"><channel><title>Sample Serial</title><item><title>Chapter Two</title><guid isPermaLink="false">chapter-2-id</guid><category>series renewal</category><link>http://localhost/chapter-2</link></item><item><title>Chapter One</title><link>http://localhost/chapter-1</link></item></channel></rss>'
            data = data.replace(b'http://localhost', self.server.base.encode())
            mime = 'application/rss+xml'
        elif self.path == '/chapter-1':
            data, mime = b'<html><nav><p>Ignore menu</p></nav><article><p>First paragraph.</p><script>ignore me</script><p>Second paragraph.</p></article></html>', 'text/html'
        elif self.path == '/chapter-2':
            data, mime = b'<main><p>The next chapter.</p></main>', 'text/html'
        elif self.path == '/images':
            data, mime = b'<div class="viewer"><img data-src="/one.png"><img src="/two.png"></div>', 'text/html'
        elif self.path in ('/one.png', '/two.png'):
            data, mime = PNG, 'image/png'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_): pass


class SerialTest(unittest.TestCase):
    def setUp(self):
        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Pages)
        self.server.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_rss_to_epub(self):
        manifest = discover_feed(self.server.base + '/feed', 'fanfiction')
        self.assertEqual([x['title'] for x in manifest['chapters']], ['Chapter One', 'Chapter Two'])
        self.assertEqual(manifest['chapters'][1]['categories'], ['series renewal'])
        self.assertEqual(manifest['chapters'][1]['provider_metadata']['entry_id'],
                         'chapter-2-id')
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                row = enqueue_serial(db, manifest)
                result = download(db, row['id'], tmp)
                with zipfile.ZipFile(result['file_path']) as archive:
                    self.assertIn(b'First paragraph.', archive.read('OEBPS/chapter-0001.xhtml'))
                    self.assertNotIn(b'Ignore menu', archive.read('OEBPS/chapter-0001.xhtml'))
                    self.assertIn(b'The next chapter.', archive.read('OEBPS/chapter-0002.xhtml'))
                self.assertEqual(result['state'], 'downloaded')

    def test_creator_homepage_discovers_advertised_episode_feed(self):
        homepage_url = 'https://creator.example/'
        feed_url = 'https://creator.example/episodes.xml'
        homepage = b'''<!doctype html><html><head>
            <link rel="alternate" type="application/atom+xml"
                  title="Episodes" href="/episodes.xml">
            </head><body>Creator</body></html>'''
        feed = b'''<rss version="2.0"><channel><title>Creator Episodes</title>
            <item><title>Episode One</title><link>https://creator.example/one</link>
            <guid>episode-one</guid></item></channel></rss>'''
        with patch('media_intake.serial.http_get', side_effect=[
                (homepage, homepage_url), (feed, feed_url)]) as fetch:
            manifest = discover_feed(homepage_url, 'webfiction')

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[1].args[0], feed_url)
        self.assertEqual(manifest['title'], 'Creator Episodes')
        self.assertEqual(manifest['source'], feed_url)
        self.assertEqual(manifest['discovered_feed_from'], homepage_url)
        self.assertEqual(manifest['chapters'][0]['provider_metadata']['entry_id'],
                         'episode-one')

    def test_creator_homepage_skips_broken_advertised_feed_and_tries_next(self):
        homepage_url = 'https://creator.example/'
        good_url = 'https://creator.example/episodes.xml'
        homepage = b'''<html><head>
            <link rel="alternate" type="application/rss+xml" title="Episode archive"
                  href="/broken.xml">
            <link rel="alternate" type="application/rss+xml" title="Episodes"
                  href="/episodes.xml"></head></html>'''
        feed = b'''<rss version="2.0"><channel><title>Creator Episodes</title>
            <item><title>Episode One</title><link>https://creator.example/one</link>
            </item></channel></rss>'''
        with patch('media_intake.serial.http_get', side_effect=[
                (homepage, homepage_url), ValueError('broken feed'), (feed, good_url)]) as fetch:
            manifest = discover_feed(homepage_url, 'webfiction')

        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(manifest['source'], good_url)

    def test_ical_schedule_feed_keeps_event_dates_and_provenance(self):
        payload = textwrap.dedent('''\
            BEGIN:VCALENDAR
            VERSION:2.0
            X-WR-CALNAME:Studio Schedule
            BEGIN:VEVENT
            UID:season-three-premiere
            DTSTAMP:20260920T120000Z
            DTSTART;VALUE=DATE:20271017
            SUMMARY:Example Show premiere
            DESCRIPTION:Season three premieres\\nOfficial studio event.
            CATEGORIES:premiere,release\\,schedule
            URL:https://studio.example/season-three
            END:VEVENT
            END:VCALENDAR
        ''').encode('utf-8')
        with patch('media_intake.serial.http_get', return_value=(
                payload, 'https://studio.example/schedule.ics')):
            manifest = discover_feed('https://studio.example/schedule.ics', 'webfiction')

        self.assertEqual(manifest['title'], 'Studio Schedule')
        self.assertEqual(len(manifest['chapters']), 1)
        event = manifest['chapters'][0]
        self.assertEqual(event['title'], 'Example Show premiere')
        self.assertEqual(event['url'], 'https://studio.example/season-three')
        self.assertEqual(event['categories'], ['premiere', 'release,schedule'])
        self.assertEqual(event['provider_metadata']['entry_id'], 'season-three-premiere')
        self.assertEqual(event['provider_metadata']['schedule_date'], '2027-10-17')
        self.assertEqual(event['provider_metadata']['summary'],
                         'Season three premieres\nOfficial studio event.')
        self.assertEqual(event['published'], '2026-09-20T12:00:00+00:00')

    def test_json_feed_preserves_creator_episode_metadata_and_reading_order(self):
        payload = json.dumps({
            'version': 'https://jsonfeed.org/version/1.1',
            'title': 'Creator Video Series',
            'authors': [{'name': 'Example Creator'}],
            'items': [
                {'id': 'episode-two', 'title': 'Episode 2',
                 'url': '/episodes/two', 'content_text': 'The next episode.',
                 'tags': ['episode'], 'date_published': '2026-09-20T12:00:00Z',
                 'season_number': 1, 'episode_number': 2, 'episode_type': 'full',
                 'attachments': [{'url': 'https://cdn.example/episode.mp4',
                                  'mime_type': 'video/mp4', 'size_in_bytes': 2048}]},
                {'id': 'episode-one', 'title': 'Episode 1',
                 'url': '/episodes/one', 'date_published': '2026-09-13T12:00:00Z'},
            ],
        }).encode('utf-8')
        with patch('media_intake.serial.http_get', return_value=(
                payload, 'https://creator.example/feed.json')):
            manifest = discover_feed('https://creator.example/feed.json', 'webfiction')

        self.assertEqual(manifest['provider_metadata']['provider'], 'json_feed')
        self.assertEqual(manifest['author'], 'Example Creator')
        self.assertEqual([item['title'] for item in manifest['chapters']],
                         ['Episode 1', 'Episode 2'])
        episode = manifest['chapters'][1]
        self.assertEqual(episode['url'], 'https://creator.example/episodes/two')
        self.assertEqual(episode['categories'], ['episode'])
        self.assertEqual(episode['provider_metadata']['entry_id'], 'episode-two')
        self.assertEqual(episode['provider_metadata']['season_number'], 1)
        self.assertEqual(episode['provider_metadata']['episode_number'], 2)
        self.assertEqual(episode['provider_metadata']['episode_type'], 'full')
        self.assertEqual(episode['provider_metadata']['enclosures'][0]['length'], 2048)
        self.assertEqual(episode['published'], '2026-09-20T12:00:00+00:00')

    def test_json_media_schedule_preserves_event_type_date_and_source_id(self):
        payload = json.dumps({
            '@type': 'MediaSchedule', 'name': 'Studio Schedule',
            'events': [{
                'id': 'show-premiere-1', 'title': 'Example Show launch',
                'event_type': 'premiere', 'start_date': '2027-10-17T20:00:00-04:00',
                'url': 'https://studio.example/premiere',
                'description': 'The new season premieres on this date.',
            }],
        }).encode('utf-8')
        with patch('media_intake.serial.http_get', return_value=(
                payload, 'https://studio.example/schedule.json')):
            manifest = discover_feed('https://studio.example/schedule.json', 'webfiction')

        event = manifest['chapters'][0]
        self.assertEqual(manifest['provider_metadata']['provider'], 'json_schedule')
        self.assertEqual(event['provider_metadata']['entry_id'], 'show-premiere-1')
        self.assertEqual(event['provider_metadata']['event_type'], 'premiere')
        self.assertEqual(event['provider_metadata']['schedule_date'], '2027-10-17')
        self.assertEqual(event['provider_metadata']['summary'],
                         'The new season premieres on this date.')

    def test_jsonld_itemlist_preserves_typed_dates_and_episode_position(self):
        payload = json.dumps({
            '@context': 'https://schema.org',
            '@type': 'ItemList',
            'name': 'Studio release schedule',
            'itemListElement': [
                {'@type': 'ListItem', 'position': 1, 'item': {
                    '@type': 'ScreeningEvent', 'identifier': 'premiere-42',
                    'name': 'Example Show premiere',
                    'startDate': '2027-10-17T20:00:00-04:00',
                    'url': 'https://studio.example/premiere',
                    'description': '<p>Official season premiere.</p>',
                    'keywords': ['premiere', 'season three'],
                    'location': {'@type': 'Place', 'name': 'Studio Stream'},
                }},
                {'@type': 'ListItem', 'position': 2, 'item': {
                    '@type': 'TVEpisode', 'identifier': 'episode-12',
                    'name': 'The Next Chapter',
                    'startDate': '2027-10-24T20:00:00-04:00',
                    'url': 'https://studio.example/episode-12',
                    'episodeNumber': 12,
                    'partOfSeason': {'@type': 'TVSeason', 'seasonNumber': 3},
                }},
            ],
        }).encode('utf-8')
        with patch('media_intake.serial.http_get', return_value=(
                payload, 'https://studio.example/schedule.jsonld')):
            manifest = discover_feed('https://studio.example/schedule.jsonld', 'webfiction')

        self.assertEqual(manifest['provider_metadata']['provider'], 'jsonld_schedule')
        self.assertEqual(len(manifest['chapters']), 2)
        premiere, episode = manifest['chapters']
        self.assertEqual(premiere['provider_metadata']['entry_id'], 'premiere-42')
        self.assertEqual(premiere['provider_metadata']['event_type'], 'screening')
        self.assertEqual(premiere['provider_metadata']['schedule_date'], '2027-10-17')
        self.assertEqual(premiere['provider_metadata']['location'], 'Studio Stream')
        self.assertEqual(premiere['provider_metadata']['summary'], 'Official season premiere.')
        self.assertEqual(episode['provider_metadata']['entry_id'], 'episode-12')
        self.assertEqual(episode['provider_metadata']['schedule_date'], '2027-10-24')
        self.assertEqual(episode['provider_metadata']['episode_type'], 'episode')
        self.assertEqual(episode['provider_metadata']['season_number'], 3)
        self.assertEqual(episode['provider_metadata']['episode_number'], 12)

    def test_jsonld_tvseries_expands_nested_episodes_and_upload_metadata(self):
        payload = json.dumps({
            '@context': 'https://schema.org',
            '@graph': [{
                '@type': 'WebPage',
                'mainEntity': {
                    '@type': 'TVSeries', 'name': 'Creator Show',
                    'creator': {'@type': 'Organization', 'name': 'Example Studio'},
                    'containsSeason': [{
                '@type': 'TVSeason', 'seasonNumber': 2,
                'episode': [{
                    '@type': 'TVEpisode', 'identifier': 's2e3',
                    'name': 'The Return', 'episodeNumber': 3,
                    'datePublished': '2026-09-20T12:00:00Z',
                    'url': 'https://studio.example/episodes/s2e3',
                    'partOfSeason': {'@type': 'TVSeason', 'seasonNumber': 2},
                    'partOfSeries': {'@type': 'TVSeries', 'name': 'Creator Show',
                                     'url': 'https://studio.example/show'},
                    'video': {'@type': 'VideoObject',
                              'contentUrl': 'https://cdn.example/s2e3.mp4',
                              'duration': 'PT24M',
                              'thumbnailUrl': 'https://cdn.example/s2e3.jpg'},
                }],
                    }],
                },
            }],
        }).encode('utf-8')
        with patch('media_intake.serial.http_get', return_value=(
                payload, 'https://studio.example/show/feed.jsonld')):
            manifest = discover_feed(
                'https://studio.example/show/feed.jsonld', 'webfiction')

        self.assertEqual(manifest['title'], 'Creator Show')
        self.assertEqual(manifest['author'], 'Example Studio')
        self.assertEqual(len(manifest['chapters']), 1)
        episode = manifest['chapters'][0]
        self.assertEqual(episode['provider_metadata']['entry_id'], 's2e3')
        self.assertEqual(episode['provider_metadata']['provider'], 'jsonld_schedule')
        self.assertEqual(episode['provider_metadata']['season_number'], 2)
        self.assertEqual(episode['provider_metadata']['episode_number'], 3)
        self.assertEqual(episode['provider_metadata']['series_title'], 'Creator Show')
        self.assertEqual(episode['provider_metadata']['content_url'],
                         'https://cdn.example/s2e3.mp4')
        self.assertEqual(episode['provider_metadata']['duration'], 'PT24M')
        self.assertEqual(episode['published'], '2026-09-20T12:00:00+00:00')

    def test_webtoon_images_to_cbz(self):
        manifest = {'title': 'Drawn Story', 'author': 'Artist', 'kind': 'webtoon',
                    'selector': '.viewer', 'chapters': [{'title': '1', 'url': self.server.base + '/images'}]}
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(tmp)) as db:
                row = enqueue_serial(db, manifest)
                result = download(db, row['id'], tmp)
                with zipfile.ZipFile(result['file_path']) as archive:
                    self.assertEqual(archive.namelist(), ['0001-0001.png', '0001-0002.png', 'ComicInfo.xml'])
                    self.assertEqual(archive.read('0001-0001.png'), PNG)
                    self.assertIn(b'Drawn Story', archive.read('ComicInfo.xml'))

    def test_bad_manifest(self):
        with self.assertRaises(ValueError):
            validate_manifest({'title': 'Oops', 'kind': 'manga', 'chapters': [{'url': 'file:///etc/passwd'}]})

    def test_youtube_atom_feed_preserves_native_video_and_playlist_ids(self):
        payload = b'''<feed xmlns="http://www.w3.org/2005/Atom"
            xmlns:yt="http://www.youtube.com/xml/schemas/2015">
            <title>Example Playlist</title><yt:playlistId>PLfixture123</yt:playlistId>
            <entry><id>yt:video:video_1</id><yt:videoId>video_1</yt:videoId>
                <yt:channelId>UCaaaaaaaaaaaaaaaaaaaaaa</yt:channelId>
                <title>Episode One</title><link rel="alternate" href="https://www.youtube.com/watch?v=video_1"/>
                <published>2026-01-02T03:04:05Z</published></entry>
            </feed>'''
        with patch('media_intake.serial.http_get', return_value=(
                payload, 'https://www.youtube.com/feeds/videos.xml?playlist_id=PLfixture123')):
            manifest = discover_feed(
                'https://www.youtube.com/feeds/videos.xml?playlist_id=PLfixture123',
                'webfiction')

        self.assertEqual(manifest['provider_metadata']['playlist_id'], 'PLfixture123')
        self.assertEqual(manifest['chapters'][0]['provider_metadata'], {
            'provider': 'youtube', 'video_id': 'video_1',
            'channel_id': 'UCaaaaaaaaaaaaaaaaaaaaaa',
            'entry_id': 'yt:video:video_1'})

    def test_podcast_rss_preserves_episode_position_and_video_enclosure_metadata(self):
        payload = b'''<rss version="2.0"
            xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
            xmlns:media="http://search.yahoo.com/mrss/">
            <channel><title>Stagecraft Video</title><itunes:type>serial</itunes:type>
                <itunes:author>Example Studio</itunes:author>
                <itunes:image href="https://cdn.example/cover.jpg"/>
                <itunes:category text="Arts"><itunes:category text="Performing Arts"/>
                </itunes:category><language>en-us</language>
                <item><title>Rehearsal Notes</title><link>https://studio.example/episode-3</link>
                    <itunes:season>2</itunes:season><itunes:episode>3</itunes:episode>
                    <itunes:episodeType>full</itunes:episodeType>
                    <itunes:duration>45:22</itunes:duration>
                    <itunes:summary>Behind &lt;b&gt;the show&lt;/b&gt;.</itunes:summary>
                    <enclosure url="https://cdn.example/episode.mp4" type="video/mp4" length="2500"/>
                    <media:content url="https://cdn.example/episode.mp4" type="video/mp4" medium="video"/>
                </item>
            </channel></rss>'''
        with patch('media_intake.serial.http_get', return_value=(
                payload, 'https://studio.example/feed.xml')):
            manifest = discover_feed('https://studio.example/feed.xml', 'webfiction')

        self.assertEqual(manifest['author'], 'Example Studio')
        self.assertEqual(manifest['provider_metadata']['provider'], 'podcast_rss')
        self.assertEqual(manifest['provider_metadata']['feed_type'], 'serial')
        self.assertEqual(manifest['provider_metadata']['categories'], ['Arts', 'Performing Arts'])
        episode = manifest['chapters'][0]
        self.assertEqual(episode['provider_metadata']['season_number'], 2)
        self.assertEqual(episode['provider_metadata']['episode_number'], 3)
        self.assertEqual(episode['provider_metadata']['episode_type'], 'full')
        self.assertEqual(episode['provider_metadata']['duration'], '45:22')
        self.assertEqual(episode['provider_metadata']['summary'], 'Behind the show.')
        self.assertIn({'url': 'https://cdn.example/episode.mp4',
                       'mime_type': 'video/mp4', 'length': 2500},
                      episode['provider_metadata']['enclosures'])
        self.assertIn({'url': 'https://cdn.example/episode.mp4',
                       'mime_type': 'video/mp4', 'medium': 'video'},
                      episode['provider_metadata']['enclosures'])


if __name__ == '__main__': unittest.main()
