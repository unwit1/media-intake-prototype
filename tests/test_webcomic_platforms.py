"""Blogger, Substack and Tumblr comic archives through their public JSON endpoints (recorded shapes)."""
import json
import tempfile
import unittest
import urllib.parse

from media_intake.core import connect
from media_intake.webcomics.http import Response
from media_intake.webcomics.platforms import BloggerAdapter, SubstackAdapter, TumblrAdapter
from media_intake.webcomics.watcher import add_series, poll_source


def blogger_post(n, comic=True, images=1):
    body = ''.join(f'<a href="#"><img src="https://blogger.googleusercontent.com/img/{n}-{i}.png"></a>' for i in range(images))
    return {'id': {'$t': f'tag:blogger.com,1999:blog-1.post-{n}'}, 'title': {'$t': f'Strip #{n}' if comic else f'News {n}'},
            'published': {'$t': f'2023-01-{1 + n // 24:02d}T{n % 24:02d}:00:00.000-08:00'},
            'content': {'$t': body if comic else '<p>Convention schedule</p>'},
            'link': [{'rel': 'alternate', 'href': f'https://fixture.blogspot.com/2023/01/post-{n}.html'}],
            'category': [{'term': 'comic' if comic else 'news'}]}


class BloggerSite:
    """Newest first, paged by start-index, like Blogger."""

    def __init__(self, posts):
        self.posts, self.calls = posts, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        newest_first = list(reversed(self.posts))
        start, count = int(query.get('start-index', 1)) - 1, int(query.get('max-results', 25))
        feed = {'title': {'$t': 'Fixture Strips'}, 'openSearch$totalResults': {'$t': str(len(self.posts))},
                'entry': newest_first[start:start + count]}
        return Response(json.dumps({'feed': feed}).encode(), url, {})


class PlatformTests(unittest.TestCase):
    def test_blogger_archive_comic_label_multi_panel_and_backfill(self):
        posts = [blogger_post(n, comic=n % 5 != 0, images=3 if n == 7 else 1) for n in range(1, 251)]
        site = BloggerSite(posts)
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                add_series(db, 'https://fixture.blogspot.com/', client=site, config={'platform_pages_per_check': 1})
                self.assertEqual(db.execute('SELECT adapter FROM webcomic_sources').fetchone()[0], 'blogger')
                poll_source(db, 1, site)
                poll_source(db, 1, site)  # older pages arrive on later checks
                keys = [r[0] for r in db.execute('SELECT entry_key FROM webcomic_entries ORDER BY ordinal')]
                self.assertEqual(keys, [f'blogger:{n}' for n in range(1, 251) if n % 5])  # news posts excluded
                multi = json.loads(db.execute("SELECT metadata FROM webcomic_entries WHERE entry_key='blogger:7'").fetchone()[0])
                self.assertEqual([a['source_url'] for a in multi['assets']],
                                 [f'https://blogger.googleusercontent.com/img/7-{i}.png' for i in range(3)])
                self.assertEqual(json.loads(db.execute('SELECT cursor FROM webcomic_sources').fetchone()[0])['blogger']['comic_tag'], 'comic')
            finally:
                db.close()

    def test_tumblr_photosets_reblogs_and_tags(self):
        def post(n, photos=1, reblog=False, tags=('comic',)):
            return {'id': n, 'url-with-slug': f'https://fixture.tumblr.com/post/{n}/page-{n}', 'date-gmt': f'2022-05-{n:02d} 10:00:00 GMT',
                    'type': 'photo', 'photo-caption': f'<p>Page {n}</p>', 'tags': list(tags),
                    'photos': [{'photo-url-1280': f'https://64.media.tumblr.com/{n}-{i}_1280.png'} for i in range(photos)],
                    **({'reblogged-from-url': 'https://other.tumblr.com/post/1'} if reblog else {})}
        data = {'tumblelog': {'title': 'Fixture Tumblr Comic'}, 'posts-total': 5,
                'posts': [post(5), post(4, reblog=True), post(3, photos=4), post(2, tags=('art',)), post(1)]}
        body = ('var tumblr_api_read = ' + json.dumps(data) + ';').encode()
        client = type('C', (), {'get': lambda self, url, **k: Response(body, url, {})})()
        snapshot = TumblrAdapter().resolve_series('https://fixture.tumblr.com/', client)
        self.assertEqual([e.source_id for e in snapshot.entries], ['tumblr:1', 'tumblr:3', 'tumblr:5'])
        self.assertEqual(len(snapshot.entries[1].assets), 4)  # photoset in order
        self.assertEqual(snapshot.series.title, 'Fixture Tumblr Comic')
        with_reblogs = TumblrAdapter().resolve_series('https://fixture.tumblr.com/', client, config={'include_reblogs': True})
        self.assertIn('tumblr:4', [e.source_id for e in with_reblogs.entries])

    def test_substack_skips_paid_posts(self):
        archive = [{'id': 2, 'slug': 'page-2', 'title': 'Page 2', 'post_date': '2024-02-01T00:00:00Z', 'audience': 'only_paid'},
                   {'id': 1, 'slug': 'page-1', 'title': 'Page 1', 'post_date': '2024-01-01T00:00:00Z', 'audience': 'everyone',
                    'canonical_url': 'https://fixture.substack.com/p/page-1'}]
        calls = []
        def get(self, url, **kwargs):
            calls.append(url)
            if '/api/v1/archive' in url:
                return Response(json.dumps(archive).encode(), url, {})
            if url.endswith('/api/v1/posts/page-1'):
                return Response(json.dumps({'body_html': '<img src="https://substackcdn.com/image/p1.png">'}).encode(), url, {})
            if url.endswith('/api/v1/publication'):
                return Response(json.dumps({'name': 'Fixture Substack Comic'}).encode(), url, {})
            raise AssertionError(url)
        snapshot = SubstackAdapter().resolve_series('https://fixture.substack.com/', type('C', (), {'get': get})())
        self.assertEqual([(e.source_id, e.assets[0].source_url) for e in snapshot.entries],
                         [('substack:1', 'https://substackcdn.com/image/p1.png')])
        self.assertFalse(any('page-2' in url for url in calls))  # paid post never requested

    def test_probe_by_host(self):
        self.assertEqual(BloggerAdapter().probe('https://x.blogspot.com/'), 95)
        self.assertEqual(TumblrAdapter().probe('https://example.com/'), 0)


if __name__ == '__main__':
    unittest.main()
