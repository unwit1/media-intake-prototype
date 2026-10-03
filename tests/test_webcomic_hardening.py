"""Cross-process source leases, bounded entry retries, crash reconciliation, aliases and manifest repair."""
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from media_intake.core import connect
from media_intake.webcomics.downloads import download_entry, reconcile_archives, repair_manifest
from media_intake.webcomics.http import Response
from media_intake.webcomics.watcher import (MAX_ENTRY_ATTEMPTS, add_series, claim_source, queue_pending,
                                            record_entry_failure, retry_failed_entries)

try:
    from tests.test_webcomics import BASE, PNG, FakeClient, fixture
except ImportError:
    from test_webcomics import BASE, PNG, FakeClient, fixture


class HardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        self.client = FakeClient({BASE + '/comic': fixture('static.html')})
        self.watch_id = add_series(self.db, BASE + '/comic', client=self.client, archive_existing=True)
        self.source = dict(self.db.execute('SELECT * FROM webcomic_sources WHERE watch_id=?', (self.watch_id,)).fetchone())
        self.client.pages[BASE + '/page/1'] = b'<div id="comic"><img src="/image.png"></div>'
        self.client.pages[BASE + '/image.png'] = Response(PNG, BASE + '/image.png', {'content-type': 'image/png'})

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def queued_row(self):
        item_id = queue_pending(self.db, self.source, 1)[0]
        return self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()

    def test_only_one_window_checks_a_source(self):
        other = connect(self.temp.name)
        try:
            self.assertTrue(claim_source(self.db, self.source['id']))
            self.assertFalse(claim_source(other, self.source['id']))
            self.db.execute('UPDATE webcomic_sources SET lease_until=? WHERE id=?', (time.time() - 1, self.source['id']))
            self.db.commit()
            self.assertTrue(claim_source(other, self.source['id']))  # an expired lease is taken over
        finally:
            other.close()

    def test_failing_entry_backs_off_then_gives_up(self):
        row = self.queued_row()
        entry_id = json.loads(row['metadata'])['webcomic_job']['entry_id']
        delays = []
        for attempt in range(1, MAX_ENTRY_ATTEMPTS + 1):
            record_entry_failure(self.db, self.source['id'], entry_id, OSError('HTTP 503'))
            state = self.db.execute('SELECT attempts, retry_after, state FROM webcomic_entries WHERE id=?', (entry_id,)).fetchone()
            if state['retry_after']:
                delays.append(round((state['retry_after'] - time.time()) / 60))
        self.assertEqual(delays, [10, 20, 40, 80, 160])
        self.assertEqual((state['attempts'], state['state']), (MAX_ENTRY_ATTEMPTS, 'failed'))
        gave_up = self.db.execute("SELECT detail FROM webcomic_events WHERE event_type='ENTRY_GAVE_UP'").fetchone()[0]
        self.assertTrue(json.loads(gave_up)['review_required'])
        self.assertEqual(retry_failed_entries(self.db, self.source['id']), 1)
        self.assertEqual(self.db.execute('SELECT state, attempts FROM webcomic_entries WHERE id=?', (entry_id,)).fetchone()[:],
                         ('queued', 0))

    def test_crash_after_archive_write_is_reconciled(self):
        row = self.queued_row()
        result = download_entry(self.db, row, self.temp.name, 1_000_000, client=self.client)
        job = json.loads(row['metadata'])['webcomic_job']
        # Simulate the crash: the file is in place but the database never recorded completion.
        self.db.execute("UPDATE items SET state='downloading', sha256=NULL WHERE id=?", (row['id'],))
        self.db.execute("UPDATE webcomic_entries SET state='queued' WHERE id=?", (job['entry_id'],))
        self.db.commit()
        self.assertEqual(reconcile_archives(self.db, self.source['id']), 1)
        self.assertEqual(self.db.execute('SELECT state FROM items WHERE id=?', (row['id'],)).fetchone()[0], 'downloaded')
        self.assertEqual(self.db.execute('SELECT state FROM webcomic_entries WHERE id=?', (job['entry_id'],)).fetchone()[0], 'archived')
        self.assertEqual(reconcile_archives(self.db), 0)
        # A file that does not match the revision is not trusted.
        self.db.execute("UPDATE items SET state='failed' WHERE id=?", (row['id'],))
        self.db.commit()
        Path(result['file_path']).write_bytes(b'not a zip')
        self.assertEqual(reconcile_archives(self.db), 0)

    def test_moved_source_keeps_one_watch(self):
        self.db.execute('INSERT INTO webcomic_source_aliases(alias_url, source_id) VALUES(?,?)',
                        (BASE + '/new-home', self.source['id']))
        self.db.commit()
        self.client.pages[BASE + '/new-home'] = fixture('static.html')
        moved = FakeClient({BASE + '/new-home': fixture('static.html')})
        self.assertEqual(add_series(self.db, BASE + '/new-home', client=moved), self.watch_id)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM webcomic_sources').fetchone()[0], 1)

    def test_manifest_repair_keeps_image_bytes(self):
        row = self.queued_row()
        result = download_entry(self.db, row, self.temp.name, 1_000_000, client=self.client)
        entry_id = json.loads(row['metadata'])['webcomic_job']['entry_id']
        path = Path(result['file_path'])
        self.assertFalse(repair_manifest(self.db, entry_id))  # intact archives are left alone
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist() if name != 'manifest.json'}
        with zipfile.ZipFile(path, 'w') as archive:  # manifest lost
            for name, data in members.items():
                archive.writestr(name, data)
        self.assertTrue(repair_manifest(self.db, entry_id))
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(archive.read('0001.png'), PNG)
        self.assertTrue(manifest['repaired'])
        self.assertEqual(manifest['assets'][0]['archive_path'], '0001.png')
        with zipfile.ZipFile(path, 'w') as archive:  # an image changed: refuse to paper over it
            archive.writestr('0001.png', b'other bytes')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            repair_manifest(self.db, entry_id)


if __name__ == '__main__':
    unittest.main()
