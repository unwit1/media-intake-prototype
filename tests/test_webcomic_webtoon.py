"""WEBTOON lists: full archive paging, catch-up across pages, gap warnings, dates, locked episodes, regions."""
import tempfile
import unittest
import urllib.parse

from media_intake.core import connect
from media_intake.webcomics.adapters import WebtoonAdapter
from media_intake.webcomics.http import Response
from media_intake.webcomics.watcher import add_series, poll_source, queue_pending

LIST = 'https://www.webtoons.com/en/fantasy/fixture/list?title_no=99'


def list_page(newest, page, per_page=10, locked=()):
    first = newest - (page - 1) * per_page
    items = ''.join(
        f'<li class="_episodeItem{" ico_dailypass" if n in locked else ""}" data-episode-no="{n}">'
        f'<a href="https://www.webtoons.com/en/fantasy/fixture/ep-{n}/viewer?title_no=99&amp;episode_no={n}">'
        f'<span class="subj"><span>Episode {n}</span></span><span class="date">Jan {1 + n % 28}, 2024</span></a></li>'
        for n in range(first, max(0, first - per_page), -1))
    more = '<div class="paginate"><a class="pg_next" href="?title_no=99&amp;page=2">Next</a></div>' if first - per_page > 0 else ''
    return (f'<html><h1 class="subj">Fixture Saga</h1><div class="author_area">Ada Artist / Bo Writer author info</div>'
            f'<ul id="_listUl">{items}</ul>{more}</html>').encode()


class WebtoonSite:
    def __init__(self, newest, locked=()):
        self.newest, self.locked, self.calls = newest, locked, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        page = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get('page', 1))
        return Response(list_page(self.newest, page, locked=self.locked), url, {})


class WebtoonTests(unittest.TestCase):
    def test_full_archive_catch_up_gap_and_locked(self):
        site = WebtoonSite(150, locked=(150,))
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                add_series(db, LIST, client=site, archive_existing=True, config={'auto_archive': False})
                rows = db.execute('SELECT entry_key, published_at, metadata FROM webcomic_entries ORDER BY ordinal').fetchall()
                self.assertEqual([int(r['entry_key']) for r in rows], list(range(1, 151)))
                self.assertEqual(rows[0]['published_at'], '2024-01-02')
                series = db.execute('SELECT metadata FROM webcomic_series').fetchone()[0]
                self.assertIn('Ada Artist', series)
                # Locked episodes are recorded but never queued.
                source = dict(db.execute('SELECT * FROM webcomic_sources').fetchone())
                queued = queue_pending(db, source, 100) + queue_pending(db, source, 100)  # at most 100 per call
                self.assertEqual(len(queued), 149)
                # 25 new episodes: catch-up reads three pages, stopping at the known episode.
                site.newest, site.calls = 175, []
                poll_source(db, 1, site)
                self.assertEqual(len(site.calls), 3)
                self.assertEqual(db.execute('SELECT MAX(CAST(entry_key AS INTEGER)) FROM webcomic_entries').fetchone()[0], 175)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM webcomic_entries').fetchone()[0], 175)
                # A gap larger than the catch-up limit is reported, not silently missed.
                site.newest = 500
                snapshot = WebtoonAdapter().resolve_series(LIST, site, {'latest_source_entry_id': '175'},
                                                           {'webtoon_catchup_pages': 5})
                self.assertTrue(any('More than 5 list pages' in warning for warning in snapshot.warnings))
            finally:
                db.close()

    def test_regional_lists_keep_their_path(self):
        url = 'https://www.webtoons.com/fr/fantasy/fixture/list?title_no=99&page=3'
        self.assertEqual(WebtoonAdapter().canonicalize_url(url), 'https://www.webtoons.com/fr/fantasy/fixture/list?title_no=99')


if __name__ == '__main__':
    unittest.main()
