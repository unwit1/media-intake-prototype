import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from media_intake.gui import MediaIntakeApp


class WatchViewTests(unittest.TestCase):
    def test_web_video_refresh_does_not_depend_on_add_dialog_locals(self):
        app = SimpleNamespace(db=Mock(), watched_view=Mock(), webcomics_panel=Mock())
        app.watched_view.get_children.return_value = []
        watch = {'id': 1, 'kind': 'video', 'title': 'Creator videos', 'state': 'watching',
                 'metadata': json.dumps({'video_subtype': 'web_series'})}
        with patch('media_intake.gui.list_watches', return_value=[watch]):
            MediaIntakeApp.refresh_watches(app)
        self.assertEqual(app.watched_view.insert.call_args.kwargs['values'][0], 'Web video series')
        app.webcomics_panel.refresh.assert_called_once()


if __name__ == '__main__':
    unittest.main()
