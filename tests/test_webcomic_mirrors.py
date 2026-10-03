"""Mirror reconciliation: proposals, confirmation that keeps numbering, auto links by ID, reversible split."""
import json
import tempfile
import unittest

from media_intake.core import connect
from media_intake.webcomics import mirrors


def add_source(db, series_id, url, title, creator, external_ids=None, entries=3):
    db.execute("INSERT INTO watched_items(kind,title,state,metadata) VALUES('webcomic',?,'watching','{}')", (title,))
    watch_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    db.execute('INSERT INTO webcomic_series(id,metadata) VALUES(?,?)', (series_id, json.dumps({
        'title': title, 'canonical_source_url': url, 'platform': 'feed',
        'creators': [{'name': creator, 'role': 'author'}], 'external_ids': external_ids or {}})))
    db.execute("INSERT INTO webcomic_sources(series_id,watch_id,url,adapter,adapter_version) VALUES(?,?,?,'feed','1')",
               (series_id, watch_id, url))
    source_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    for n in range(1, entries + 1):
        db.execute("""INSERT INTO webcomic_entries(source_id,entry_key,url,ordinal,metadata,fingerprint)
            VALUES(?,?,?,?,'{}','f')""", (source_id, f'{series_id}-{n}', f'{url}{n}', n))
    db.commit()
    return source_id


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_title_and_creator_proposes_confirm_keeps_numbering_and_split_restores(self):
        site = add_source(self.db, 'site', 'https://fixturecomic.example/', 'Fixture Comic', 'Ada Artist', entries=120)
        webtoon = add_source(self.db, 'webtoon', 'https://www.webtoons.com/en/x/list?title_no=1', 'Fixture Comic', 'Ada Artist', entries=95)
        add_source(self.db, 'other', 'https://other.example/', 'Fixture Comic', 'Someone Else')
        found = mirrors.find_candidates(self.db)
        self.assertEqual([(m['series_a'], m['series_b'], m['status']) for m in found], [('site', 'webtoon', 'proposed')])
        self.assertEqual(self.db.execute('SELECT series_id FROM webcomic_sources WHERE id=?', (webtoon,)).fetchone()[0], 'webtoon')
        self.assertEqual(mirrors.find_candidates(self.db), [])  # already proposed
        mirrors.confirm(self.db, found[0]['id'], language='en', edition='WEBTOON vertical edition')
        shared = mirrors.mirrors_of(self.db, 'site')
        self.assertEqual([(m['id'], m['entries']) for m in shared], [(site, 120), (webtoon, 95)])
        ordinals = [r[0] for r in self.db.execute('SELECT MAX(ordinal) FROM webcomic_entries GROUP BY source_id ORDER BY source_id')]
        self.assertEqual(ordinals[:2], [120, 95])  # each source keeps its own numbering
        self.assertEqual(mirrors.link(self.db, found[0]['id'])['edition'], 'WEBTOON vertical edition')
        self.assertTrue(mirrors.split(self.db, webtoon))
        self.assertEqual(self.db.execute('SELECT series_id FROM webcomic_sources WHERE id=?', (webtoon,)).fetchone()[0], 'webtoon')
        self.assertEqual(mirrors.link(self.db, found[0]['id'])['status'], 'split')
        self.assertFalse(mirrors.split(self.db, webtoon))

    def test_shared_catalog_id_links_automatically_and_rejections_stick(self):
        add_source(self.db, 'a', 'https://a.example/', 'Alpha', 'X', external_ids={'anilist': '7'})
        add_source(self.db, 'b', 'https://b.example/', 'Alpha (Remastered)', 'Y', external_ids={'anilist': '7'})
        add_source(self.db, 'c', 'https://c.example/', 'Gamma', 'Z')
        add_source(self.db, 'd', 'https://d.example/', 'Gamma', 'Z')
        found = {(m['series_a'], m['series_b']): m for m in mirrors.find_candidates(self.db)}
        self.assertEqual(found[('a', 'b')]['status'], 'confirmed')
        self.assertEqual(found[('c', 'd')]['status'], 'proposed')
        mirrors.reject(self.db, found[('c', 'd')]['id'])
        self.assertEqual(mirrors.find_candidates(self.db), [])
        self.assertEqual([m['status'] for m in mirrors.links(self.db, 'rejected')], ['rejected'])


if __name__ == '__main__':
    unittest.main()
