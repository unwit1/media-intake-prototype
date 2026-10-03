"""One watched item per source URL across processes, without merging older duplicates."""
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest

from media_intake.core import connect
from media_intake.watched import add_watch


class UniqueWatchSourceTests(unittest.TestCase):
    def test_existing_duplicates_are_kept_and_new_ones_refused(self):
        with tempfile.TemporaryDirectory() as root:
            # A database from before the index existed, already holding a duplicate.
            raw = sqlite3.connect(f'{root}/intake.sqlite3')
            raw.execute('''CREATE TABLE watched_items (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'Unknown', source_url TEXT, release_date TEXT,
                state TEXT NOT NULL, next_check_at TEXT, last_checked_at TEXT,
                last_error TEXT, file_path TEXT, parent_id INTEGER, metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
            raw.executemany("INSERT INTO watched_items(kind,title,source_url,state) VALUES(?,?,?,?)", [
                ('book', 'First', 'https://catalog.example/w/1', 'watching'),
                ('book', 'Copy', 'https://catalog.example/w/1', 'watching'),
                ('book', 'Other', 'https://catalog.example/w/2', 'watching'),
                ('book', 'No URL', None, 'watching'), ('book', 'No URL 2', None, 'watching')])
            raw.commit()
            raw.close()
            with closing(connect(root)) as db:
                rows = db.execute('SELECT title, legacy_duplicate FROM watched_items ORDER BY id').fetchall()
                self.assertEqual([tuple(r) for r in rows],
                                 [('First', 0), ('Copy', 1), ('Other', 0), ('No URL', 0), ('No URL 2', 0)])
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO watched_items(kind,title,source_url,state) VALUES('book','x',?,'watching')",
                               ('https://catalog.example/w/2',))
                db.rollback()
                # Adding the duplicated source again updates the canonical row, not the legacy copy.
                watch_id = add_watch(db, 'book', 'First (updated)', source_url='https://catalog.example/w/1')
                self.assertEqual(db.execute('SELECT title FROM watched_items WHERE id=?', (watch_id,)).fetchone()[0],
                                 'First (updated)')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0], 5)
            with closing(connect(root)) as db:  # reopening does not re-flag anything
                self.assertEqual(db.execute('SELECT SUM(legacy_duplicate) FROM watched_items').fetchone()[0], 1)

    def test_a_lost_insert_race_reuses_the_other_process_row(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db, closing(connect(root)) as other:
            url = 'https://catalog.example/series/9'
            original = db.execute

            class Racing:
                """The lookup misses, then another process inserts before this one does."""
                def __init__(self):
                    self.raced = False

                def __getattr__(self, name):
                    return getattr(db, name)

                def execute(self, sql, params=()):
                    if not self.raced and sql.lstrip().startswith('SELECT id,state FROM watched_items WHERE source_url'):
                        self.raced = True
                        other.execute("INSERT INTO watched_items(kind,title,source_url,state) VALUES('series','Theirs',?,'watching')",
                                      (url,))
                        other.commit()
                        return original('SELECT id,state FROM watched_items WHERE 0')
                    return original(sql, params)

            watch_id = add_watch(Racing(), 'series', 'Mine', source_url=url)
            rows = db.execute('SELECT id, title FROM watched_items WHERE source_url=?', (url,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['id'], rows[0]['title']), (watch_id, 'Mine'))

    def test_concurrent_adds_from_separate_connections_make_one_row(self):
        with tempfile.TemporaryDirectory() as root:
            connect(root).close()
            barrier = threading.Barrier(6)
            errors = []
            def add():
                with closing(connect(root)) as db:
                    barrier.wait()
                    try:
                        add_watch(db, 'series', 'Same', source_url='https://catalog.example/series/1')
                    except Exception as exc:
                        errors.append(exc)
            threads = [threading.Thread(target=add) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            with closing(connect(root)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
