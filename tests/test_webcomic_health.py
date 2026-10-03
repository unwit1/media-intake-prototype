"""Parser health: metrics history, early warnings, collapse safety and metadata reprocessing."""
import tempfile
import unittest

from media_intake.core import connect
from media_intake.webcomics import health
from media_intake.webcomics.watcher import add_series, poll_source

try:
    from tests.test_webcomic_feed_archive import FEED, SiteClient, atom
except ImportError:
    from test_webcomic_feed_archive import FEED, SiteClient, atom


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        self.site = SiteClient({FEED: atom(range(20, 0, -1))})
        add_series(self.db, FEED, client=self.site, config={'follow_feed_archive': False, 'auto_archive': False})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_collapse_keeps_data_and_warns_before_next_check(self):
        self.assertEqual(health.warnings(self.db, 1), [])
        self.site.site[FEED] = atom(range(20, 15, -1))  # the page suddenly lists 5 of 20 strips
        poll_source(self.db, 1, self.site)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM webcomic_entries WHERE removed=0').fetchone()[0], 20)
        warnings = health.warnings(self.db, 1)
        self.assertTrue(any('5 entries, down from 20' in text for text in warnings), warnings)
        info = health.summary(self.db, 1)
        self.assertEqual([sample['snapshot_entries'] for sample in info['history']], [20, 5])

    def test_reprocess_refreshes_metadata_without_redownloading(self):
        self.db.execute("UPDATE webcomic_entries SET state='archived'")
        self.db.commit()
        # The adapter now reads better titles.
        self.site.site[FEED] = atom(range(20, 0, -1)).replace(b'<title>Strip ', b'<title>Fixture Strip ')
        self.assertEqual(health.reprocess_metadata(self.db, 1, self.site), 20)
        row = self.db.execute("SELECT state, json_extract(metadata, '$.title') FROM webcomic_entries ORDER BY ordinal").fetchone()
        self.assertEqual(tuple(row), ('archived', 'Fixture Strip 1'))
        poll_source(self.db, 1, self.site)  # the next ordinary check sees nothing new
        self.assertEqual({r[0] for r in self.db.execute('SELECT state FROM webcomic_entries')}, {'archived'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0], 0)
        self.assertEqual(health.reprocess_metadata(self.db, 1, self.site), 0)


if __name__ == '__main__':
    unittest.main()
