import unittest
import json
from types import SimpleNamespace
from unittest.mock import Mock

from media_intake.gui import MediaIntakeApp


class SearchQueueRoutingTest(unittest.TestCase):
    def _app(self, item):
        results_view = Mock()
        results_view.selection.return_value = ['0']
        return SimpleNamespace(
            results_view=results_view,
            results=[item],
            _search_result_category=Mock(return_value='Books'),
            _add_search_item_to_watched=Mock(),
        )

    def test_queueing_metadata_catalog_result_adds_watch_for_immediate_scan(self):
        item = {'title': 'Example Book', 'kind': 'book', 'category': 'Books',
                'source_role': 'metadata', 'format': 'catalog',
                'discovery_only': True}
        app = self._app(item)

        MediaIntakeApp.queue_result(app)

        app._add_search_item_to_watched.assert_called_once_with(item, 'Books')

    def test_queueing_series_result_adds_full_series_watch(self):
        item = {'title': 'Example Series', 'kind': 'series', 'category': 'Books',
                'series_bundle': {'title': 'Example Series', 'books': []}}
        app = self._app(item)

        MediaIntakeApp.queue_result(app)

        app._add_search_item_to_watched.assert_called_once_with(item, 'Books')

    def test_queue_source_label_shows_the_configured_irc_server(self):
        row = {'metadata': json.dumps({
            'provider': 'IRC', 'source_key': 'irc',
            'download_priority_label': 'IRC: Primary (irc.example.test #books)',
            'irc': {'server': 'irc.example.test', 'channel': '#books'},
        })}

        self.assertEqual(MediaIntakeApp._queue_source_label(row),
                         'IRC: Primary (irc.example.test #books)')

    def test_completed_downloads_are_selected_for_the_library_tab(self):
        rows = [
            {'id': 1, 'state': 'queued', 'file_path': None},
            {'id': 2, 'state': 'downloaded', 'file_path': 'C:/library/book.epub'},
            {'id': 3, 'state': 'imported', 'file_path': 'C:/library/comic.cbz'},
            {'id': 4, 'state': 'failed', 'file_path': None},
            {'id': 5, 'state': 'downloaded', 'file_path': None},
        ]

        self.assertEqual([row['id'] for row in MediaIntakeApp._completed_library_items(rows)],
                         [2, 3])


if __name__ == '__main__':
    unittest.main()
