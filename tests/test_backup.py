"""Database backup and watch-list export."""
from contextlib import closing
import csv
from pathlib import Path
import sqlite3
import tempfile
import unittest

from media_intake.backup import WATCH_COLUMNS, backup_database, export_watch_list
from media_intake.core import connect, enqueue
from media_intake.watched import add_watch


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_backup_is_a_complete_consistent_copy(self):
        enqueue(self.db, {'title': 'Queued', 'url': 'https://example.org/a.epub', 'format': '.epub', 'source': 'direct'})
        target = Path(self.temp.name) / 'backups' / 'copy.sqlite3'
        self.assertEqual(backup_database(self.db, target), target)
        self.assertFalse(target.with_name('copy.sqlite3.partial').exists())
        with closing(sqlite3.connect(target)) as copy:
            self.assertEqual(copy.execute('SELECT COUNT(*) FROM items').fetchone()[0], 1)
            self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_watch_list_csv_lists_series_and_members(self):
        series = add_watch(self.db, 'series', 'Fixture Saga', 'Ann Author', 'https://openlibrary.org/works/OL1W')
        member = add_watch(self.db, 'book', 'Book, "One"', 'Ann Author', 'https://openlibrary.org/works/OL2W',
                           '2024-01-02')
        self.db.execute('UPDATE watched_items SET parent_id=? WHERE id=?', (series, member))
        self.db.commit()
        target = Path(self.temp.name) / 'watch.csv'
        self.assertEqual(export_watch_list(self.db, target), 2)
        with open(target, encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(tuple(rows[0]), WATCH_COLUMNS)
        book = next(row for row in rows if row['kind'] == 'book')
        self.assertEqual((book['title'], book['series'], book['release_date']), ('Book, "One"', 'Fixture Saga', '2024-01-02'))


if __name__ == '__main__':
    unittest.main()
