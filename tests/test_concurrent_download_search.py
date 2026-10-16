"""Concurrent connector dispatch, best-match ranking and failure isolation."""
import json
import threading
import time
import unittest
from unittest.mock import patch

from media_intake.watched import _search_candidates


class ConcurrentSearchTests(unittest.TestCase):
    def test_every_applicable_source_overlaps_and_best_match_is_not_first_response(self):
        watch = {'kind': 'book', 'title': 'Example Novel', 'author': 'Unknown', 'metadata': '{}'}
        caller = threading.get_ident()
        barrier = threading.Barrier(3, timeout=5)
        calls, progress_threads = [], []
        def approval(_options, key, *_args):
            if '_resolved_approvals' in _options:
                return _options['_resolved_approvals'].get(key)
            self.assertEqual(threading.get_ident(), caller)
            return {'approved_for_download': True} if key in {'internet_archive', 'lnori.com', 'novelarchive.cc'} else None
        def result(source, title, delay):
            calls.append(source)
            barrier.wait()
            time.sleep(delay)
            return [{'title': title, 'author': 'Unknown', 'format': '.epub',
                     'url': f'https://files.example/{source}.epub'}]
        with patch('media_intake.watched._approved_source_record', side_effect=approval), \
             patch('media_intake.watched.search_internet_archive', side_effect=lambda *_a, **_k: result('archive', 'Example Novel Revised', 0)), \
             patch('media_intake.watched.search_lnori', side_effect=lambda *_a, **_k: result('lnori', 'Example Novel', .15)), \
             patch('media_intake.watched.search_novelarchive', side_effect=lambda *_a, **_k: result('novel', 'Example Novel Supplement', .05)), \
             patch('media_intake.watched.resolve_catalog_file_links', side_effect=lambda hit, _approval: [hit]):
            items, errors = _search_candidates(watch, progress_callback=lambda _message: progress_threads.append(threading.get_ident()))
        self.assertEqual(set(calls), {'archive', 'lnori', 'novel'})
        self.assertEqual(items[0]['url'], 'https://files.example/lnori.epub')
        self.assertEqual(errors, [])
        self.assertTrue(progress_threads)
        self.assertEqual(set(progress_threads), {caller})

    def test_failed_source_and_duplicate_file_do_not_hide_success(self):
        watch = {'kind': 'book', 'title': 'Example Novel', 'author': 'Unknown', 'metadata': '{}'}
        file = {'title': 'Example Novel', 'format': '.epub', 'url': 'https://files.example/book.epub'}
        def approval(_options, key, *_args):
            if '_resolved_approvals' in _options:
                return _options['_resolved_approvals'].get(key)
            return {'approved_for_download': True} if key in {'internet_archive', 'lnori.com', 'novelarchive.cc'} else None
        with patch('media_intake.watched._approved_source_record', side_effect=approval), \
             patch('media_intake.watched.search_internet_archive', side_effect=OSError('temporary outage')), \
             patch('media_intake.watched.search_lnori', return_value=[dict(file)]), \
             patch('media_intake.watched.search_novelarchive', return_value=[dict(file)]), \
             patch('media_intake.watched.resolve_catalog_file_links', side_effect=lambda hit, _approval: [hit]):
            items, errors = _search_candidates(watch)
        self.assertEqual(len(items), 1)
        self.assertTrue(any('temporary outage' in error for error in errors))

    def test_sources_without_approval_records_are_still_searched(self):
        watch = {'kind': 'comic', 'title': 'Example Comic', 'author': 'Unknown', 'metadata': '{}'}
        with patch('media_intake.watched.search_getcomics', return_value=[]) as getcomics:
            _search_candidates(watch)
            getcomics.assert_called()

    def test_process_wide_search_limit_across_many_books(self):
        from concurrent.futures import ThreadPoolExecutor
        active = peak = 0
        lock = threading.Lock()
        def approval(_options, key, *_args):
            if '_resolved_approvals' in _options:
                return _options['_resolved_approvals'].get(key)
            return {'approved_for_download': True} if key in {'internet_archive', 'lnori.com', 'novelarchive.cc'} else None
        def search(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(active, peak)
            time.sleep(.03)
            with lock:
                active -= 1
            return []
        with patch('media_intake.watched._approved_source_record', side_effect=approval), \
             patch('media_intake.watched.search_internet_archive', side_effect=search), \
             patch('media_intake.watched.search_lnori', side_effect=search), \
             patch('media_intake.watched.search_novelarchive', side_effect=search):
            with ThreadPoolExecutor(max_workers=6) as clients:
                futures = [clients.submit(_search_candidates, {'kind': 'book', 'title': f'Book {i}', 'metadata': '{}'}) for i in range(20)]
                for future in futures:
                    self.assertEqual(future.result(timeout=10), ([], []))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 8)


if __name__ == '__main__':
    unittest.main()
