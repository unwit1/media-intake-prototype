"""Pause/resume, one-time backlog archiving with automation off, single-source checks and panel filters."""
import json
import tempfile
import unittest
from unittest.mock import patch

from media_intake.core import connect
from media_intake.webcomics import controls
from media_intake.webcomics.http import Response
from media_intake.webcomics.watcher import add_series, process_watch

try:
    from tests.test_webcomic_feed_archive import FEED, SiteClient, atom
except ImportError:
    from test_webcomic_feed_archive import FEED, SiteClient, atom


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        self.site = SiteClient({FEED: atom(range(5, 0, -1))})
        self.watch_id = add_series(self.db, FEED, client=self.site, archive_existing=False,
                                   config={'auto_archive': False, 'follow_feed_archive': False})
        self.source_id = self.db.execute('SELECT id FROM webcomic_sources').fetchone()[0]

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def watch(self):
        return dict(self.db.execute('SELECT * FROM watched_items WHERE id=?', (self.watch_id,)).fetchone())

    def states(self):
        return [row[0] for row in self.db.execute('SELECT state FROM webcomic_entries ORDER BY ordinal')]

    def run_check(self, downloaded):
        def fake_download(db, item_id, root, destination=None):
            downloaded.append(item_id)
            db.execute("UPDATE items SET state='downloaded' WHERE id=?", (item_id,))
            entry_id = json.loads(db.execute('SELECT metadata FROM items WHERE id=?', (item_id,)).fetchone()[0])['webcomic_job']['entry_id']
            db.execute("UPDATE webcomic_entries SET state='archived' WHERE id=?", (entry_id,))
            db.commit()
        with patch('media_intake.webcomics.watcher.HttpClient', return_value=self.site), \
             patch('media_intake.core.download', side_effect=fake_download):
            process_watch(self.db, self.watch(), self.temp.name)

    def test_paused_source_never_polls(self):
        controls.set_paused(self.db, self.source_id, True)
        self.assertEqual(self.watch()['next_check_at'][:4], '9999')
        self.site.calls.clear()
        self.run_check([])
        self.assertEqual(self.site.calls, [])
        controls.set_paused(self.db, self.source_id, False)
        self.assertLess(self.watch()['next_check_at'][:4], '9999')
        self.run_check([])
        self.assertIn(FEED, self.site.calls)

    def test_backlog_runs_once_with_automation_off(self):
        self.assertEqual(self.states(), ['known'] * 5)  # archive existing = off
        downloaded = []
        self.run_check(downloaded)
        self.assertEqual(downloaded, [])  # automation off, no backlog requested
        self.assertEqual(controls.archive_backlog(self.db, self.source_id), 5)
        for _ in range(2):
            self.run_check(downloaded)
        self.assertEqual(len(downloaded), 5)
        self.assertEqual(self.states(), ['archived'] * 5)
        # A new release afterwards stays metadata-only.
        self.site.site[FEED] = atom(range(6, 0, -1))
        self.run_check(downloaded)
        self.assertEqual(len(downloaded), 5)
        self.assertEqual(self.states()[-1], 'pending')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM items').fetchone()[0], 5)

    def test_check_source_only_checks_that_comic(self):
        other = SiteClient({'https://other.example/feed': atom(range(2, 0, -1))})
        other_watch = add_series(self.db, 'https://other.example/feed', client=other)
        with patch('media_intake.webcomics.watcher.HttpClient', return_value=self.site):
            result = controls.check_source(self.db, self.watch_id, self.temp.name)
        self.assertEqual(result['id'], self.watch_id)
        self.assertIsNone(self.db.execute('SELECT last_checked_at FROM watched_items WHERE id=?', (other_watch,)).fetchone()[0])

    def test_panel_counts_and_filters(self):
        import tkinter as tk
        from media_intake.webcomics.ui import WebcomicsPanel
        root = tk.Tk()
        root.withdraw()
        try:
            app = type('App', (), {'db': self.db, 'root': root, 'data_root': self.temp.name,
                                   '_worker': lambda *a, **k: None, '_done': lambda *a: None})()
            controls.archive_backlog(self.db, self.source_id)
            panel = WebcomicsPanel(app, root)
            row = panel.view.get_children()[0]
            self.assertEqual(panel.view.set(row, 'waiting'), '5')
            panel.filter_status.set('Paused')
            self.assertEqual(panel.view.get_children(), ())
            panel.filter_status.set('Has waiting entries')
            self.assertEqual(len(panel.view.get_children()), 1)
            panel.filter_text.set('no such comic')
            self.assertEqual(panel.view.get_children(), ())
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main()
