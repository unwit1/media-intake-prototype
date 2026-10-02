"""WordPress REST API comic post types: detection, paging, chapters, edits and fallbacks."""
import json
import tempfile
import unittest
import urllib.parse

from media_intake.core import connect
from media_intake.webcomics.http import Response
from media_intake.webcomics.watcher import add_series, poll_source
from media_intake.webcomics.wordpress import WordPressAdapter, api_root, endpoint

SITE = 'https://comic.example/blog/'
API = 'https://comic.example/blog/api/'  # subdirectory install with a custom REST prefix


def post(number, chapter=1, images=2, modified=None, kind='comic'):
    content = ''.join(f'<p><img src="/blog/wp-content/uploads/{number}-{n}.png" alt=""></p>' for n in range(images))
    return {'id': number, 'type': kind, 'link': f'{SITE}comic/page-{number}/',
            'title': {'rendered': f'Page {number} &#8211; Fixture'},
            'date_gmt': f'2021-01-{1 + number // 24:02d}T{number % 24:02d}:00:00',
            'modified_gmt': modified or f'2021-01-{1 + number // 24:02d}T{number % 24:02d}:00:00',
            'content': {'rendered': content},
            '_embedded': {'author': [{'name': 'Fixture Artist'}],
                          'wp:term': [[{'id': chapter, 'name': f'Chapter {chapter}', 'taxonomy': 'chapters',
                                        'link': f'{SITE}chapter/{chapter}/'}]]}}


class FakeWordPress:
    """Serves the page, /types, and a paged comic post type; records requests."""

    def __init__(self, comics, rest_route=False, link_header=False):
        self.comics, self.calls, self.rest_route, self.link_header = comics, [], rest_route, link_header
        self.blog_posts = [post(9000 + n, kind='post') for n in range(3)]

    def root(self):
        return SITE + '?rest_route=/' if self.rest_route else API

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url == SITE:
            head = '' if self.link_header else f'<link rel="https://api.w.org/" href="{self.root()}" />'
            headers = {'link': f'<{self.root()}>; rel="https://api.w.org/"'} if self.link_header else {}
            body = (f'<html><head>{head}<link rel="alternate" type="application/rss+xml" href="{SITE}feed/">'
                    '</head><body>Comic</body></html>').encode()
            return Response(body, url, headers)
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        route = query.pop('rest_route', None) if self.rest_route else '/' + parts.path[len('/blog/api/'):]
        if route in ('/', ''):
            return self.json({'name': 'Fixture Comic'})
        if route == '/wp/v2/types':
            return self.json({'post': {'rest_base': 'posts', 'name': 'Posts'},
                              'page': {'rest_base': 'pages', 'name': 'Pages'},
                              'comic': {'rest_base': 'comic', 'name': 'Comics'},
                              'merch': {'rest_base': 'merch', 'name': 'Shop items'}})
        items = {'/wp/v2/comic': self.comics, '/wp/v2/posts': self.blog_posts}.get(route)
        if items is None:
            raise AssertionError(f'unexpected request {url}')
        if 'modified_after' in query:
            items = [item for item in items if item['modified_gmt'] > query['modified_after']]
        per_page, page = int(query.get('per_page', 10)), int(query.get('page', 1))
        items = sorted(items, key=lambda item: item['modified_gmt' if query.get('orderby') == 'modified' else 'date_gmt'])
        pages = max(1, -(-len(items) // per_page))
        return self.json(items[(page - 1) * per_page:page * per_page], {'x-wp-totalpages': str(pages)})

    def json(self, data, headers=None):
        return Response(json.dumps(data).encode(), 'json', headers or {})


class HelperTests(unittest.TestCase):
    def test_api_root_and_endpoints(self):
        self.assertEqual(api_root(Response(b'<link rel="https://api.w.org/" href="https://x.example/wp-json/" />',
                                           'https://x.example/', {})), 'https://x.example/wp-json/')
        self.assertEqual(api_root(Response(b'', 'https://x.example/', {'link': '<https://x.example/wp-json/>; rel="https://api.w.org/"'})),
                         'https://x.example/wp-json/')
        self.assertIsNone(api_root(Response(b'<html></html>', 'https://x.example/', {})))
        self.assertEqual(endpoint('https://x.example/blog/api/', 'wp/v2/comic', {'page': 2}),
                         'https://x.example/blog/api/wp/v2/comic?page=2')
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(
            endpoint('https://x.example/?rest_route=/', 'wp/v2/comic', {'page': 2})).query),
                         {'rest_route': ['/wp/v2/comic'], 'page': ['2']})


class WordPressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def entries(self):
        return [dict(row) for row in self.db.execute(
            'SELECT entry_key, ordinal, state, chapter_key, metadata FROM webcomic_entries ORDER BY ordinal')]

    def test_paged_comic_type_chapters_and_moved_post(self):
        comics = [post(n, chapter=1 if n <= 120 else 2) for n in range(1, 251)]
        site = FakeWordPress(comics)
        add_series(self.db, SITE, client=site, config={'wordpress_pages_per_check': 2})
        source = self.db.execute('SELECT adapter, cursor FROM webcomic_sources').fetchone()
        self.assertEqual(source['adapter'], 'wordpress_api')
        self.assertEqual(len(self.entries()), 200)  # two pages of 100 on the first check
        poll_source(self.db, 1, site)
        entries = self.entries()
        self.assertEqual([row['entry_key'] for row in entries], [f'wp:{n}' for n in range(1, 251)])
        self.assertEqual({row['state'] for row in entries}, {'known'})  # backlog follows "archive existing" = off
        first = json.loads(entries[0]['metadata'])
        self.assertEqual(first['title'], 'Page 1 – Fixture')
        self.assertEqual([a['source_url'] for a in first['assets']],
                         ['https://comic.example/blog/wp-content/uploads/1-0.png',
                          'https://comic.example/blog/wp-content/uploads/1-1.png'])
        self.assertFalse({'wp:9000', 'wp:9001', 'wp:9002'} & {row['entry_key'] for row in entries})  # blog posts excluded
        self.assertEqual(len({row['chapter_key'] for row in entries}), 2)
        chapters = [json.loads(r[0])['title'] for r in self.db.execute('SELECT metadata FROM webcomic_chapters')]
        self.assertEqual(sorted(chapters), ['Chapter 1', 'Chapter 2'])
        # Incremental: page 5 moves to chapter 2 and a new page appears.
        site.calls.clear()
        comics[4] = post(5, chapter=2, modified='2021-02-01T00:00:00')
        comics.append(post(251, chapter=2, modified='2021-02-02T00:00:00'))
        poll_source(self.db, 1, site)
        requested = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for url in site.calls if '/wp/v2/comic' in url]
        self.assertTrue(requested and all('modified_after' in q for q in requested))
        self.assertNotIn(SITE, site.calls)  # detection is remembered
        entries = {row['entry_key']: row for row in self.entries()}
        self.assertEqual(entries['wp:5']['chapter_key'], entries['wp:200']['chapter_key'])
        self.assertIn(entries['wp:251']['state'], ('pending', 'queued'))
        moved = self.db.execute("SELECT COUNT(*) FROM webcomic_events WHERE event_type='CHAPTER_REASSIGNED' AND entry_key='wp:5'").fetchone()[0]
        self.assertEqual(moved, 1)

    def test_rest_route_sites_and_link_headers(self):
        for kwargs in ({'rest_route': True}, {'link_header': True}):
            with self.subTest(**kwargs):
                site = FakeWordPress([post(n) for n in range(1, 4)], **kwargs)
                detected = WordPressAdapter().detect(site.get(SITE), site)
                self.assertEqual(detected['rest_base'], 'comic')
                snapshot = WordPressAdapter().resolve_series(SITE, site)
                self.assertEqual([entry.source_id for entry in snapshot.entries], ['wp:1', 'wp:2', 'wp:3'])
                self.assertEqual(snapshot.series.title, 'Fixture Comic')

    def test_sites_without_a_comic_type_use_their_feed(self):
        site = FakeWordPress([post(n, images=0) for n in range(1, 4)])  # comic type without images
        self.assertIsNone(WordPressAdapter().detect(site.get(SITE), site))


if __name__ == '__main__':
    unittest.main()
