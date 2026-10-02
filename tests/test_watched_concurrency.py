import threading
import time
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from media_intake.core import connect
from media_intake.watched import add_watch, process_due_watches


class WatchedBookConcurrencyTest(unittest.TestCase):
    def test_due_books_search_and_download_in_a_bounded_pool(self):
        with tempfile.TemporaryDirectory() as root:
            with closing(connect(root)) as db:
                for number in range(5):
                    add_watch(db, 'book', f'Book {number}', metadata={})

                lock = threading.Lock()
                active = 0
                peak = 0

                def fake_search(worker_db, watch, library_folder, source_options=None):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    time.sleep(0.08)
                    with lock:
                        active -= 1
                    return False

                with patch('media_intake.watched._search_book', side_effect=fake_search):
                    checked = process_due_watches(db, root + '/library', force=True)

        self.assertEqual(checked, 5)
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)


if __name__ == '__main__':
    unittest.main()
