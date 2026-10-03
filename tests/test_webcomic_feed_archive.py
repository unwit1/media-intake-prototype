"""Paged/archived feeds (RFC 5005, JSON Feed next_url) discovered across checks."""
import datetime as dt
import json
import tempfile
import unittest

from media_intake.core import connect
from media_intake.webcomics.adapters import FeedAdapter, feed_links
from media_intake.webcomics.http import Response
from media_intake.webcomics.watcher import add_series, poll_source

BASE = 'https://comic.example'
FEED = BASE + '/feed'
TOTAL, PER_PAGE = 1000, 10
PAGES = TOTAL // PER_PAGE


def strip(number):
    published = (dt.date(2020, 1, 1) + dt.timedelta(days=number)).isoformat()
    return (f'<entry><id>tag:comic.example,2020:{number}</id><title>Strip {number}</title>'
            f'<link href="{BASE}/strip/{number}"/><updated>{published}T00:00:00Z</updated></entry>')


def atom(numbers, links=()):
    link_xml = ''.join(f'<link rel="{rel}" href="{href}"/>' for rel, href in links)
    return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Fixture Comic</title>'
            '<author><name>Artist</name></author>' + link_xml +
            ''.join(strip(n) for n in numbers) + '</feed>').encode()


def page_url(page):
    return FEED if page == 1 else f'{FEED}?page={page}'


def paged_feed(newest=TOTAL, pages=PAGES):
    """Page 1 holds the newest strips; each page links to the next older one."""
    site = {}
    for page in range(1, pages + 1):
        high = newest - (page - 1) * PER_PAGE
        links = [('next', page_url(page + 1))] if page < pages else []
        site[page_url(page)] = atom(range(high, high - PER_PAGE, -1), links)
    return site


class SiteClient:
    def __init__(self, site):
        self.site, self.calls, self.head_unchanged = site, [], False

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url not in self.site:
            raise AssertionError(f'unexpected fetch {url}')
        unchanged = self.head_unchanged and url == FEED
        return Response(self.site[url], url, {}, 304 if unchanged else 200, unchanged)


class FeedArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def entries(self):
        return [dict(row) for row in self.db.execute(
            'SELECT entry_key, ordinal, state, removed FROM webcomic_entries ORDER BY ordinal')]

    def cursor(self):
        return json.loads(self.db.execute('SELECT cursor FROM webcomic_sources').fetchone()[0])

    def test_thousand_entry_archive_is_found_across_checks_in_reading_order(self):
        client = SiteClient(paged_feed())
        add_series(self.db, FEED, client=client)  # archive_existing=False
        self.assertEqual(len(self.entries()), 11 * PER_PAGE)  # head + 10 pages on the first check
        client.head_unchanged = True  # 304s on the head must not stall the backlog
        checks = 0
        while not self.cursor()['feed_archive'].get('exhausted'):
            poll_source(self.db, 1, client)
            checks += 1
            self.assertLess(checks, 20)
        entries = self.entries()
        self.assertEqual(len(entries), TOTAL)
        self.assertEqual([row['entry_key'] for row in entries],
                         [f'tag:comic.example,2020:{n}' for n in range(1, TOTAL + 1)])
        self.assertEqual([row['ordinal'] for row in entries], list(range(1, TOTAL + 1)))
        self.assertEqual({row['state'] for row in entries}, {'known'})  # backlog follows "archive existing" = off
        metrics = json.loads(self.db.execute('SELECT metrics FROM webcomic_sources').fetchone()[0])
        self.assertEqual((metrics['archive_pages'], metrics['archive_complete']), (PAGES - 1, True))
        backfills = self.db.execute("SELECT COUNT(*) FROM webcomic_events WHERE event_type='ARCHIVE_BACKFILLED'").fetchone()[0]
        self.assertEqual(backfills, checks)
        # Once exhausted, a check fetches only the head.
        client.calls.clear()
        poll_source(self.db, 1, client)
        self.assertEqual(client.calls, [FEED])
        # The head rotates: two new strips appear and the oldest two drop off the page.
        client.head_unchanged = False
        client.site[FEED] = atom(range(TOTAL + 2, TOTAL + 2 - PER_PAGE, -1), [('next', page_url(2))])
        poll_source(self.db, 1, client)
        entries = self.entries()
        self.assertEqual(len(entries), TOTAL + 2)
        self.assertFalse(any(row['removed'] for row in entries))
        self.assertEqual([row['state'] for row in entries[-2:]], ['queued', 'queued'])  # new strips download
        self.assertEqual(entries[-1]['entry_key'], f'tag:comic.example,2020:{TOTAL + 2}')

    def test_archive_existing_downloads_the_backlog_too(self):
        client = SiteClient(paged_feed(newest=40, pages=4))
        add_series(self.db, FEED, client=client, archive_existing=True,
                   config={'archive_pages_per_check': 1, 'auto_archive': False})
        poll_source(self.db, 1, client)
        poll_source(self.db, 1, client)
        entries = self.entries()
        self.assertEqual([row['ordinal'] for row in entries], list(range(1, 41)))
        self.assertEqual({row['state'] for row in entries}, {'pending'})

    def test_rfc5005_archives_json_next_url_and_guards(self):
        links, complete = feed_links(atom([1], [('prev-archive', BASE + '/archive/2019'), ('self', FEED)]).replace(
            b'</feed>', b'<fh:complete xmlns:fh="http://purl.org/syndication/history/1.0"/></feed>'))
        self.assertEqual((links, complete), ({'prev-archive': BASE + '/archive/2019'}, True))
        self.assertEqual(feed_links(json.dumps({'version': 'https://jsonfeed.org/version/1.1',
                                                'next_url': FEED + '?page=2'}).encode()),
                         ({'next': FEED + '?page=2'}, False))
        rss = (b'<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>T</title>'
               b'<atom:link rel="next" href="https://comic.example/feed?page=2"/></channel></rss>')
        self.assertEqual(feed_links(rss)[0], {'next': FEED + '?page=2'})
        self.assertEqual(feed_links(b'not xml'), ({}, False))
        site = {FEED: atom(range(20, 10, -1), [('prev-archive', BASE + '/archive/1')]),
                BASE + '/archive/1': atom(range(10, 5, -1), [('prev-archive', 'https://elsewhere.example/a')])}
        snapshot = FeedAdapter().resolve_series(FEED, SiteClient(site))
        self.assertEqual(len(snapshot.entries), 15)
        self.assertTrue(snapshot.cursor['feed_archive']['exhausted'])
        self.assertIn('another site', snapshot.warnings[0])
        loop = {FEED: atom([3], [('next', page_url(2))]), page_url(2): atom([2], [('next', FEED)])}
        snapshot = FeedAdapter().resolve_series(FEED, SiteClient(loop))
        self.assertEqual([e.source_id for e in snapshot.entries], ['tag:comic.example,2020:2', 'tag:comic.example,2020:3'])
        self.assertIn('loop', snapshot.warnings[0])
        off = FeedAdapter().resolve_series(FEED, SiteClient(paged_feed(newest=30, pages=3)),
                                           config={'follow_feed_archive': False})
        self.assertEqual(len(off.entries), 10)


if __name__ == '__main__':
    unittest.main()
