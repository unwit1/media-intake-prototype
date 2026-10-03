"""Asset revalidation: reuse on re-archive, sampled audits, changed art at the same URL."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from media_intake.core import connect
from media_intake.webcomics.audit import audit_source
from media_intake.webcomics.downloads import download_entry
from media_intake.webcomics.http import Response, SourceUnavailable
from media_intake.webcomics.watcher import add_series, queue_pending

try:
    from tests.test_webcomics import BASE, FakeClient, _png, fixture
except ImportError:
    from test_webcomics import BASE, FakeClient, _png, fixture

ART_V1, ART_V2 = _png(2, 3), _png(5, 7)


class ValidatingClient(FakeClient):
    """Answers image requests with 304 when the caller's ETag still matches."""

    def __init__(self, pages):
        super().__init__(pages)
        self.etag, self.full_image_fetches = '"art-v1"', 0

    def get(self, url, **kwargs):
        if url == BASE + '/image.png':
            headers = kwargs.get('headers') or {}
            if headers.get('If-None-Match') == self.etag:
                self.calls.append((url, kwargs))
                raise SourceUnavailable(304, url)
            self.full_image_fetches += 1
        return super().get(url, **kwargs)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        self.client = ValidatingClient({BASE + '/comic': fixture('static.html')})
        watch_id = add_series(self.db, BASE + '/comic', client=self.client, archive_existing=True)
        self.source = dict(self.db.execute('SELECT * FROM webcomic_sources WHERE watch_id=?', (watch_id,)).fetchone())
        self.client.pages[BASE + '/page/1'] = b'<div id="comic"><img src="/image.png"></div>'
        self.serve(ART_V1, '"art-v1"')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def serve(self, data, etag):
        self.client.etag = etag
        self.client.pages[BASE + '/image.png'] = Response(data, BASE + '/image.png',
                                                          {'content-type': 'image/png', 'etag': etag})

    def archive_next(self):
        item_id = queue_pending(self.db, self.source, 1)[0]
        row = self.db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
        return row, download_entry(self.db, row, self.temp.name, 1_000_000, client=self.client)

    def test_rename_reuses_images_and_audit_detects_changed_art(self):
        row, first = self.archive_next()
        entry_id = json.loads(row['metadata'])['webcomic_job']['entry_id']
        self.assertEqual(self.client.full_image_fetches, 1)
        # The entry is renamed on the site: it re-archives, but the image is revalidated, not refetched.
        self.db.execute("""UPDATE webcomic_entries SET state='pending', queue_id=NULL, fingerprint=fingerprint||'-renamed'
            WHERE id=?""", (entry_id,))
        self.db.commit()
        _row, second = self.archive_next()
        self.assertEqual(self.client.full_image_fetches, 1)
        with zipfile.ZipFile(second['file_path']) as archive:
            self.assertEqual(archive.read('0001.png'), ART_V1)
            self.assertEqual(json.loads(archive.read('manifest.json'))['assets_reused'], 1)
        # Audit: unchanged art costs only a 304.
        self.assertEqual(audit_source(self.db, self.source['id'], self.client),
                         {'checked': 1, 'changed': 0, 'unchanged': 1, 'errors': 0})
        # The artist replaces the art at the same URL.
        self.serve(ART_V2, '"art-v2"')
        outcome = audit_source(self.db, self.source['id'], self.client)
        self.assertEqual((outcome['changed'], outcome['unchanged']), (1, 0))
        state, revision = self.db.execute('SELECT state, fingerprint FROM webcomic_entries WHERE id=?', (entry_id,)).fetchone()
        self.assertEqual(state, 'pending')
        self.assertIn(':asset:', revision)
        _row, third = self.archive_next()
        with zipfile.ZipFile(third['file_path']) as archive:
            self.assertEqual(archive.read('0001.png'), ART_V2)
        # Older archives (and their art) are kept.
        self.assertTrue(Path(first['file_path']).exists() and Path(second['file_path']).exists())
        with zipfile.ZipFile(first['file_path']) as archive:
            self.assertEqual(archive.read('0001.png'), ART_V1)
        changed = self.db.execute("SELECT COUNT(*) FROM webcomic_events WHERE event_type='ASSET_CHANGED'").fetchone()[0]
        self.assertEqual(changed, 1)

    def test_scheduled_audit_runs_weekly_and_can_be_turned_off(self):
        from media_intake.webcomics.watcher import run_scheduled_audit
        self.archive_next()
        self.assertEqual(run_scheduled_audit(self.db, self.source['id'], {'audit_sample': 0}, self.client), None)
        outcome = run_scheduled_audit(self.db, self.source['id'], {}, self.client)
        self.assertEqual(outcome['unchanged'], 1)
        config = json.loads(self.db.execute('SELECT config FROM webcomic_sources').fetchone()[0])
        self.assertIsNone(run_scheduled_audit(self.db, self.source['id'], config, self.client))  # not due yet


if __name__ == '__main__':
    unittest.main()
