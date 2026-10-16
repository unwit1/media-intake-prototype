"""Per-source health history: recording, status rules, download/search hooks and ranking."""
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from media_intake import source_health, transfer
from media_intake.core import ShortlinkPending, connect, download, enqueue


class StatusTests(unittest.TestCase):
    def test_status_rules_and_summary_fields(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            record = source_health.record
            record(db, 'Good.example', 'download', True, bytes=4_000_000, seconds=2.0)
            record(db, 'good.example', 'download', True, bytes=1_000_000, seconds=1.0)
            record(db, 'good.example', 'search', True, detail='3 result(s)')
            for n in range(3):
                record(db, 'bad.example', 'download', False, detail=f'HTTP 50{n}')
            for ok in (True, False, False, True, False, False):
                record(db, 'flaky.example', 'search', ok, detail=None if ok else 'timeout')
            record(db, 'new.example', 'search', False, detail='timeout')
            rows = {row['source_key']: row for row in source_health.summaries(db)}
            self.assertEqual([row['source_key'] for row in source_health.summaries(db)],
                             ['bad.example', 'flaky.example', 'new.example', 'good.example'])
            self.assertEqual({key: row['status'] for key, row in rows.items()},
                             {'good.example': 'healthy', 'bad.example': 'failing',
                              'flaky.example': 'degraded', 'new.example': 'untested'})
            good = rows['good.example']
            self.assertEqual((good['downloads_ok'], good['searches_ok'], good['median_speed']), (2, 1, 1_500_000))
            bad = rows['bad.example']
            self.assertEqual((bad['failure_streak'], bad['last_error'], bad['last_ok_at']), (3, 'HTTP 502', None))
            record(db, 'bad.example', 'download', True)
            recovering = source_health.summary(db, 'bad.example')
            self.assertEqual((recovering['status'], recovering['failure_streak']), ('degraded', 0))  # 1 of 4
            # Searches that work do not hide downloads that keep failing.
            for n in range(3):
                record(db, 'cdn.example', 'download', False, detail='HTTP 403')
                record(db, 'cdn.example', 'search', True)
            self.assertEqual(source_health.summary(db, 'cdn.example')['status'], 'failing')

    def test_history_is_capped_and_clearable(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            for _ in range(source_health.KEEP_PER_SOURCE + 25):
                source_health.record(db, 'busy.example', 'search', True)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_events').fetchone()[0],
                             source_health.KEEP_PER_SOURCE)
            source_health.record(db, '', 'search', True)  # ignored
            source_health.clear(db, 'busy.example')
            self.assertEqual(source_health.summaries(db), [])


class DownloadHookTests(unittest.TestCase):
    def queue(self, db, url='https://files.example/a.zip', **extra):
        return enqueue(db, {'title': 'A', 'author': 'B', 'url': url, 'format': '.zip', 'source': url,
                            'source_key': 'softarchive.download', **extra})

    def test_download_outcomes_are_recorded_but_user_actions_are_not(self):
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db:
            item = self.queue(db)
            with patch('media_intake.core._download', return_value={'file_path': None}):
                download(db, item['id'], root)
            with patch('media_intake.core._download', side_effect=ValueError('HTTP 404')), self.assertRaises(ValueError):
                download(db, item['id'], root)
            for quiet in (transfer.TransferPaused('p'), transfer.TransferCancelled('c'), ShortlinkPending('s'),
                          ValueError('This item is already downloading')):
                with patch('media_intake.core._download', side_effect=quiet), self.assertRaises(type(quiet)):
                    download(db, item['id'], root)
            summary = source_health.summary(db, 'softarchive.download')
            self.assertEqual((summary['downloads_ok'], summary['downloads_failed'], summary['last_error']),
                             (1, 1, 'HTTP 404'))


class SearchHookTests(unittest.TestCase):
    def test_watched_searches_record_and_failing_sources_rank_last(self):
        from media_intake import watched
        watch = {'title': 'Book', 'kind': 'book', 'metadata': '{}'}
        def fake_sequential(watch, options, db, report):
            key = next(iter(options['_resolved_approvals']))
            if key == 'meawfy.com':
                return [{'title': 'Book', 'url': 'https://meawfy.example/book.epub', 'format': '.epub',
                         'source_key': 'meawfy.com'}], []
            if key == 'softarchive.download':
                return [{'title': 'Book', 'url': 'https://soft.example/book.epub', 'format': '.epub',
                         'source_key': 'softarchive.download'}], []
            return [], ['libgen: timed out']
        with tempfile.TemporaryDirectory() as root, closing(connect(root)) as db, \
                patch('media_intake.watched._download_search_plan',
                      return_value=('Books', ['softarchive.download', 'meawfy.com', 'libgen'])), \
                patch('media_intake.watched._approved_source_record', return_value={'approved_for_download': True}), \
                patch('media_intake.watched._search_candidates_sequential', side_effect=fake_sequential), \
                patch('media_intake.discovery._download_sort_key', side_effect=lambda item, *_: item['url']):
            results, errors = watched._search_candidates(watch, {'download_priority': ['x']}, db)
            self.assertEqual([r['source_key'] for r in results], ['meawfy.com', 'softarchive.download'])
            self.assertEqual(errors, ['libgen: timed out'])
            self.assertEqual(source_health.summary(db, 'libgen')['searches_failed'], 1)
            self.assertEqual(source_health.summary(db, 'meawfy.com')['searches_ok'], 1)
            # After three failed downloads, MeAwfy's equally good match is tried last.
            for _ in range(3):
                source_health.record(db, 'meawfy.com', 'download', False, detail='HTTP 500')
            results, _errors = watched._search_candidates(watch, {'download_priority': ['x']}, db)
            self.assertEqual([r['source_key'] for r in results], ['softarchive.download', 'meawfy.com'])


if __name__ == '__main__':
    unittest.main()
