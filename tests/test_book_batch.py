"""Stress real SQLite/graph/queue/HTTP downloads with many series and shared works."""
import collections
from contextlib import closing
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from media_intake import book_batch
from media_intake.book_batch import (add_book_series_batch, book_batch_summary, cancel_book_batch,
                                     create_book_batch, pause_book_batch, resumable_book_batches,
                                     retry_book_batch, run_book_batch)
from media_intake.core import connect, enqueue, enqueue_for_watch
from media_intake.watched import process_due_watches


def epub_bytes():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('mimetype', b'application/epub+zip')
        archive.writestr('META-INF/container.xml', '<container/>')
        archive.writestr('book.xhtml', '<html><body>Stress test book</body></html>')
    return out.getvalue()


class BatchStressTests(unittest.TestCase):
    def test_multiple_series_download_restart_duplicates_future_and_failure(self):
        counts = collections.Counter()
        epub = epub_bytes()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                counts[self.path] += 1
                if self.path == '/1000.epub' and counts[self.path] == 1:
                    self.send_error(500)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/epub+zip')
                self.send_header('Content-Length', str(len(epub)))
                self.end_headers()
                self.wfile.write(epub)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        series = []
        future = (dt.date.today() + dt.timedelta(days=365)).isoformat()
        for index in range(8):
            books = []
            for number in range(6):
                identifier = 1000 + index * 10 + number
                books.append({'title': f'Fixture Novel {identifier}', 'author': 'Fixture Author',
                    'source_url': f'https://openlibrary.org/works/OL{identifier}W',
                    'provider': 'Open Library', 'series_index': number + 1,
                    'release_date': '2000-01-01'})
            books.extend([{'title': 'Shared Fixture Novel', 'author': 'Fixture Author',
                'source_url': 'https://openlibrary.org/works/OL9999W', 'provider': 'Open Library',
                'release_date': '2000-01-01'},
                {'title': f'Future Fixture {index}', 'author': 'Fixture Author',
                 'source_url': f'https://openlibrary.org/works/OL{2000+index}W',
                 'provider': 'Open Library', 'release_date': future}])
            series.append({'kind': 'series', 'title': f'Fixture Saga {index}', 'author': 'Fixture Author',
                           'source_url': f'https://openlibrary.org/series/fixture-{index}',
                           'provider': 'Open Library', 'books': books})
        by_title = {row['title']: row for row in series}
        active, peak = 0, 0
        lock = threading.Lock()
        def candidates(watch, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(.01)
                identifier = watch['source_url'].rsplit('/', 1)[-1][2:-1]
                return ([{'title': watch['title'], 'author': watch['author'], 'format': '.epub',
                          'url': f'{base}/{identifier}.epub', 'source': watch['source_url']}], [])
            finally:
                with lock:
                    active -= 1
        try:
            with tempfile.TemporaryDirectory() as root:
                with closing(connect(root)) as db:
                    result = add_book_series_batch(db, series)
                    self.assertEqual(result['added'], 8)
                    self.assertEqual(result['books_added'], 57)  # 48 + shared + 8 future
                    repeated = add_book_series_batch(db, series)
                    self.assertEqual(repeated['existing'], 8)
                    self.assertEqual(repeated['books_added'], 0)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0], 65)
                    start = time.monotonic()
                    with patch('media_intake.watched.search_openlibrary_metadata', side_effect=lambda title, **_: [by_title[title]]), \
                         patch('media_intake.watched._search_candidates', side_effect=candidates):
                        process_due_watches(db, root + '/library', force=True)
                    # The transfer engine retries the transient HTTP 500 itself.
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE state='downloaded'").fetchone()[0], 49)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE state='upcoming'").fetchone()[0], 8)
                    self.assertLessEqual(peak, 4)
                    # SQLite setup can serialize tiny local transfers. The
                    # dedicated watcher concurrency test verifies overlap.
                    self.assertFalse(db.execute("SELECT last_error FROM watched_items WHERE source_url LIKE '%OL1000W'").fetchone()[0])
                # Restart the application connection; successful downloads and
                # duplicate series are not downloaded again.
                with closing(connect(root)) as db:
                    with patch('media_intake.watched.search_openlibrary_metadata', side_effect=lambda title, **_: [by_title[title]]), \
                         patch('media_intake.watched._search_candidates', side_effect=candidates):
                        process_due_watches(db, root + '/library', force=True)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE state='downloaded'").fetchone()[0], 49)
                    self.assertEqual(len(list(Path(root + '/library').glob('*.epub'))), 49)
                    self.assertEqual(sum(counts.values()), 50)
                    self.assertEqual(counts['/1000.epub'], 2)
                    self.assertFalse(db.execute("SELECT last_error FROM watched_items WHERE source_url LIKE '%OL1000W'").fetchone()[0])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM items').fetchone()[0], 57)
                    self.assertLess(time.monotonic() - start, 60)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_partial_batch_failure_does_not_hide_success(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            result = add_book_series_batch(db, [
                {'kind': 'series', 'title': 'Good', 'source_url': 'https://example.com/series/1', 'books': []},
                {'kind': 'book', 'title': 'Wrong kind'},
                {'kind': 'series', 'title': 'Missing URL'}])
            self.assertEqual(result['added'], 1)
            self.assertEqual(result['failed'], 2)
            self.assertEqual(len(result['results']), 3)

    def test_large_queue_lookup_reuses_request_and_uses_indexes(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            rows = [(f'https://example.com/{i}.epub', json.dumps({'title': f'Book {i}'})) for i in range(2000)]
            db.executemany('INSERT INTO items(url,metadata) VALUES(?,?)', rows)
            db.execute("INSERT INTO items(url,metadata) VALUES('watch:test',?)", (json.dumps({'watch_search': True, 'watch_id': 42}),))
            request_id = db.execute("SELECT id FROM items WHERE url='watch:test'").fetchone()[0]
            db.commit()
            result = enqueue_for_watch(db, {'title': 'Resolved book', 'author': 'Writer',
                'url': 'https://example.com/resolved.epub', 'format': '.epub', 'source': 'https://example.com/book'}, 42)
            self.assertEqual(result['id'], request_id)
            plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM items WHERE CAST(CASE WHEN json_valid(metadata) THEN json_extract(metadata,'$.watch_request_id') END AS TEXT)=?", ('42',)).fetchall()
            self.assertTrue(any('items_watch_request' in row['detail'] for row in plan))


def fixture_series(count, books=1, prefix='Resume'):
    return [{'kind': 'series', 'title': f'{prefix} Saga {index}', 'author': 'Fixture Author',
             'source_url': f'https://openlibrary.org/series/{prefix.lower()}-{index}',
             'provider': 'Open Library', 'books': [
                 {'title': f'{prefix} Novel {index}-{number}', 'author': 'Fixture Author',
                  'source_url': f'https://openlibrary.org/works/OL{prefix}{index}x{number}W',
                  'provider': 'Open Library', 'series_index': number + 1, 'release_date': '2000-01-01'}
                 for number in range(books)]}
            for index in range(count)]


class PersistentBatchTests(unittest.TestCase):
    def test_hundred_series_batch_resumes_after_restart_without_duplicates(self):
        with tempfile.TemporaryDirectory() as root:
            with closing(connect(root)) as db:
                batch_id = create_book_batch(db, fixture_series(100))
                seen = []
                # Simulate the application closing after 37 series.
                summary = run_book_batch(db, batch_id, progress=lambda index, total, row: seen.append(index),
                                         should_stop=lambda: len(seen) >= 37)
                self.assertEqual(summary['state'], 'paused')
                self.assertEqual((summary['added'], summary['remaining']), (37, 63))
            with closing(connect(root)) as db:
                self.assertEqual(book_batch_summary(db, batch_id)['state'], 'paused')
                summary = run_book_batch(db, batch_id)
                self.assertEqual(summary['state'], 'completed')
                self.assertEqual((summary['added'], summary['existing'], summary['failed']), (100, 0, 0))
                self.assertEqual(summary['books_added'], 100)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='series'").fetchone()[0], 100)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='book'").fetchone()[0], 100)
                self.assertEqual(resumable_book_batches(db), [])

    def test_crashed_claim_is_reclaimed_and_reported_as_added(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            records = fixture_series(2, prefix='Crash')
            batch_id = create_book_batch(db, records)
            run_book_batch(db, batch_id, should_stop=lambda: db.execute(
                "SELECT COUNT(*) FROM book_batch_items WHERE state='added'").fetchone()[0] >= 1)
            # Leave the batch as a crashed process would: running, with the next
            # series' watch created but its claim never released.
            book_batch._add_series(db, records[1], None, None)
            db.execute("""UPDATE book_batch_items SET state='processing', claimed_at='2000-01-01 00:00:00'
                WHERE batch_id=? AND position=2""", (batch_id,))
            db.execute("UPDATE book_batches SET state='running' WHERE id=?", (batch_id,))
            db.commit()
            self.assertEqual(resumable_book_batches(db), [batch_id])
            summary = run_book_batch(db, batch_id)
            self.assertEqual(summary['state'], 'completed')
            self.assertEqual([row['status'] for row in summary['results']], ['added', 'added'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='series'").fetchone()[0], 2)

    def test_fresh_claim_from_another_process_is_not_taken(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            batch_id = create_book_batch(db, fixture_series(2, prefix='Lease'))
            db.execute("""UPDATE book_batch_items SET state='processing', claimed_at=?
                WHERE batch_id=? AND position=1""", (book_batch._now(), batch_id))
            db.commit()
            summary = run_book_batch(db, batch_id)
            self.assertEqual([row['status'] for row in summary['results']], ['processing', 'added'])
            self.assertEqual((summary['state'], summary['remaining']), ('running', 1))

    def test_two_connections_share_one_batch_without_double_processing(self):
        with tempfile.TemporaryDirectory() as root:
            with closing(connect(root)) as db:
                batch_id = create_book_batch(db, fixture_series(20, prefix='Pair'))
            def worker():
                with closing(connect(root)) as db:
                    run_book_batch(db, batch_id)
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            with closing(connect(root)) as db:
                summary = book_batch_summary(db, batch_id)
                self.assertEqual((summary['state'], summary['added'], summary['existing']), ('completed', 20, 0))
                attempts = db.execute('SELECT SUM(attempts) FROM book_batch_items WHERE batch_id=?',
                                      (batch_id,)).fetchone()[0]
                self.assertEqual(attempts, 20)

    def test_pause_from_another_connection_stops_before_next_series(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            batch_id = create_book_batch(db, fixture_series(5, prefix='Pause'))
            def progress(index, total, row):
                if index == 2:
                    with closing(connect(root)) as other:
                        pause_book_batch(other, batch_id)
            summary = run_book_batch(db, batch_id, progress=progress)
            self.assertEqual((summary['state'], summary['added'], summary['remaining']), ('paused', 2, 3))
            summary = run_book_batch(db, batch_id)
            self.assertEqual((summary['state'], summary['added']), ('completed', 5))

    def test_cancel_keeps_added_series_and_retry_reprocesses_failures(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            records = fixture_series(4, prefix='Cancel')
            fixed = records[1]
            records[1] = {'kind': 'series', 'title': 'Missing URL'}
            batch_id = create_book_batch(db, records)
            summary = run_book_batch(db, batch_id, progress=lambda index, total, row:
                                     index == 2 and cancel_book_batch(db, batch_id))
            self.assertEqual(summary['state'], 'cancelled')
            self.assertEqual([row['status'] for row in summary['results']],
                             ['added', 'failed', 'cancelled', 'cancelled'])
            self.assertEqual(summary['results'][1]['error'], 'Series has no catalog URL')
            self.assertEqual(run_book_batch(db, batch_id)['state'], 'cancelled')
            db.execute('UPDATE book_batch_items SET record=? WHERE batch_id=? AND position=2',
                       (json.dumps(fixed), batch_id))
            db.commit()
            self.assertEqual(retry_book_batch(db, batch_id, include_cancelled=True), 3)
            summary = run_book_batch(db, batch_id)
            self.assertEqual((summary['state'], summary['added'], summary['failed']), ('completed', 4, 0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watched_items WHERE kind='series'").fetchone()[0], 4)

    def test_series_progress_aggregates_child_states(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            summary = add_book_series_batch(db, fixture_series(1, books=4, prefix='Progress'))
            watch_id = summary['results'][0]['watch_id']
            children = [row['id'] for row in db.execute(
                "SELECT id FROM watched_items WHERE parent_id=? AND kind='book' ORDER BY id", (watch_id,))]
            self.assertEqual(len(children), 4)
            db.execute("UPDATE watched_items SET state='downloaded' WHERE id=?", (children[0],))
            db.execute("UPDATE watched_items SET state='upcoming' WHERE id=?", (children[1],))
            db.execute("UPDATE watched_items SET state='watching', last_error='HTTP 500' WHERE id=?", (children[2],))
            db.execute("UPDATE watched_items SET state='watching', last_error=NULL WHERE id=?", (children[3],))
            db.commit()
            summary = book_batch_summary(db, summary['batch_id'])
            expected = {'books': 4, 'downloaded': 1, 'upcoming': 1, 'failed': 1, 'pending': 1}
            self.assertEqual(summary['results'][0]['progress'], expected)
            self.assertEqual(summary['books'], expected)

    def test_batch_size_limit(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            with self.assertRaises(ValueError):
                create_book_batch(db, [])
            with self.assertRaises(ValueError):
                create_book_batch(db, fixture_series(101))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM book_batches').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
