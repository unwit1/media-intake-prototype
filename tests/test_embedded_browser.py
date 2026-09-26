import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from media_intake.embedded_browser import BrowserActions, _forum_import_button_script


class EmbeddedBrowserTest(unittest.TestCase):
    def test_browser_page_capture_is_saved_inside_app_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = Path(tmp) / 'browser-captures'
            actions = BrowserActions(capture)
            actions.window = Mock()
            actions.window.get_current_url.return_value = 'https://fora.snahp.eu/search.php?keywords=sample'
            status = actions.save_current_page('<html><body>forum results</body></html>')
            saved = list(capture.glob('forum-results-*.html'))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_text(encoding='utf-8'), '<html><body>forum results</body></html>')
            self.assertIn('Saved this forum page', status)

    def test_browser_capture_rejects_wrong_host_and_large_page(self):
        actions = BrowserActions(Path(tempfile.gettempdir()) / 'unused-media-intake-captures')
        actions.window = Mock()
        actions.window.get_current_url.return_value = 'https://example.com/'
        with self.assertRaisesRegex(ValueError, 'Only pages'):
            actions.save_current_page('<html></html>')
        actions.window.get_current_url.return_value = 'https://fora.snahp.eu/'
        with self.assertRaisesRegex(ValueError, 'smaller than 5 MB'):
            actions.save_current_page('x' * (5_000_001))

    def test_import_overlay_calls_only_the_page_capture_api(self):
        script = _forum_import_button_script()
        self.assertIn('location.hostname !== "fora.snahp.eu"', script)
        self.assertIn('window.pywebview.api.save_current_page', script)


if __name__ == '__main__':
    unittest.main()
