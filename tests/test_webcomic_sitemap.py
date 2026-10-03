"""Sitemap discovery for comic sites without feeds: bounds, filters, validation and revisions."""
import gzip
import json
import tempfile
import unittest

from media_intake.core import connect
from media_intake.webcomics.http import ExtractionError, Response, SourceUnavailable
from media_intake.webcomics.sitemap import SitemapAdapter, collect_urls, is_comic_candidate
from media_intake.webcomics.watcher import add_series, poll_source

BASE = 'https://comic.example'
COMIC_PAGE = b'<html><head><title>Fixture Comic</title></head><body><div id="comic"><img src="/img/p.png"></div></body></html>'
BLOG_PAGE = b'<html><head><title>Fixture Comic</title></head><body><p>News</p><img src="/img/banner.png"></body></html>'


def urlset(urls, lastmod='2024-01-01'):
    return ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
            ''.join(f'<url><loc>{u}</loc><lastmod>{lastmod}</lastmod></url>' for u in urls) + '</urlset>').encode()


def index(urls):
    return ('<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
            ''.join(f'<sitemap><loc>{u}</loc></sitemap>' for u in urls) + '</sitemapindex>').encode()


class Site:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.pages:
            raise SourceUnavailable(404, url)
        return Response(self.pages[url], url, {})


def comic_site(count=1000, comic_html=COMIC_PAGE):
    comics = [f'{BASE}/comic/page-{n}/' for n in range(1, count + 1)]
    noise = [f'{BASE}/blog/news-{n}/' for n in range(5)] + [f'{BASE}/tag/fantasy/', f'{BASE}/category/news/',
                                                            f'{BASE}/comic/page-1/cover.png', 'https://other.example/comic/1/']
    pages = {f'{BASE}/robots.txt': f'User-agent: *\nSitemap: {BASE}/sitemap-index.xml\n'.encode(),
             f'{BASE}/sitemap-index.xml': index([f'{BASE}/comics-1.xml.gz', f'{BASE}/comics-2.xml', f'{BASE}/posts.xml',
                                                 f'{BASE}/sitemap-index.xml', 'https://other.example/sitemap.xml']),
             f'{BASE}/comics-1.xml.gz': gzip.compress(urlset(comics[:count // 2])),
             f'{BASE}/comics-2.xml': urlset(comics[count // 2:]),
             f'{BASE}/posts.xml': urlset(noise),
             f'{BASE}/': COMIC_PAGE}
    for url in comics[:5]:
        pages[url] = comic_html
    return Site(pages), comics


class SitemapTests(unittest.TestCase):
    def test_candidates_exclude_non_comic_urls(self):
        self.assertTrue(is_comic_candidate(f'{BASE}/comic/page-1/'))
        self.assertTrue(is_comic_candidate(f'{BASE}/strips/2020-01-01'))
        for url in (f'{BASE}/blog/news/', f'{BASE}/tag/comic/', f'{BASE}/comic/a.png', f'{BASE}/comic/page/2/'):
            self.assertFalse(is_comic_candidate(url), url)
        self.assertTrue(is_comic_candidate(f'{BASE}/p/123', include=r'/p/\d+$'))

    def test_thousand_urls_streamed_with_guards(self):
        site, comics = comic_site()
        found, warnings = collect_urls(site, BASE + '/')
        self.assertEqual(sorted(found), sorted(comics))
        self.assertTrue(any('another site' in warning for warning in warnings))
        self.assertEqual(site.calls.count(f'{BASE}/sitemap-index.xml'), 1)  # self-reference not refetched

    def test_snapshot_requires_comic_images_and_keeps_order(self):
        site, comics = comic_site(count=30)
        snapshot = SitemapAdapter().resolve_series(BASE + '/', site)
        self.assertEqual([e.source_id for e in snapshot.entries], comics)  # natural order: page-2 before page-10
        self.assertEqual(snapshot.series.title, 'Fixture Comic')
        self.assertTrue(snapshot.cursor['sitemap']['validated'])
        blog_site, _ = comic_site(count=30, comic_html=BLOG_PAGE)
        with self.assertRaisesRegex(ExtractionError, 'comic images'):
            SitemapAdapter().resolve_series(BASE + '/', blog_site)
        empty = Site({f'{BASE}/robots.txt': b'User-agent: *\n', f'{BASE}/sitemap.xml': urlset([f'{BASE}/about/'])})
        with self.assertRaisesRegex(ExtractionError, 'no sitemap entries'):
            SitemapAdapter().resolve_series(BASE + '/', empty)

    def test_lastmod_revisions_never_remove_entries(self):
        site, comics = comic_site(count=20)
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                add_series(db, BASE + '/', client=site, config={'adapter': 'sitemap', 'auto_archive': False})
                self.assertEqual(db.execute('SELECT adapter FROM webcomic_sources').fetchone()[0], 'sitemap')
                # One page edited, five pages missing from a partial sitemap.
                site.pages[f'{BASE}/comics-1.xml.gz'] = gzip.compress(urlset(comics[:5], lastmod='2024-02-01'))
                poll_source(db, 1, site)
                rows = {r['entry_key']: dict(r) for r in db.execute('SELECT * FROM webcomic_entries')}
                self.assertEqual(len(rows), 20)
                self.assertFalse(any(row['removed'] for row in rows.values()))
                self.assertEqual(json.loads(rows[comics[0]]['metadata'])['revision'], '2024-02-01')
                updated = db.execute("SELECT COUNT(*) FROM webcomic_events WHERE event_type='UPDATED_ENTRY'").fetchone()[0]
                self.assertEqual(updated, 5)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
