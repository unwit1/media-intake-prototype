"""The source connector GUI: Settings → Sources, the release picker, one-source search and Load more."""
import contextlib
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from connector_site import FakeSite, Reply, fixture
from media_intake import gui
from media_intake.connectors import CONNECTORS, load_source_config
from media_intake.sources_ui import (ReleasePicker, format_summary, single_source_choices, source_summary,
                                     spec_for_choice, wants_release_picker)


class SummaryTest(unittest.TestCase):
    def test_result_rows_describe_formats_and_sources_in_plain_words(self):
        group = {'title': 'Night of the Living Dead', 'format': '.torrent', 'display_source': 'Download · TPB',
                 'download_variants': [
                     {'format': '.torrent', 'resolution': '1080p', 'provider': 'The Pirate Bay', 'connector_row': True},
                     {'format': '.torrent', 'resolution': '720p', 'provider': 'YTS', 'connector_row': True},
                     {'format': '.nzb', 'resolution': '1080p', 'provider': 'NZBGeek', 'connector_row': True}]}
        self.assertEqual(format_summary(group), '1080p, 720p · torrent/Usenet')
        self.assertEqual(source_summary(group), '3 releases · The Pirate Bay, YTS, NZBGeek')
        self.assertTrue(wants_release_picker(group))
        book = {'format': '.epub', 'connector_row': True, 'file_format': None}
        self.assertEqual(format_summary(book), 'EPUB · file')
        self.assertEqual(format_summary({'format': 'catalog'}), 'catalog')
        self.assertFalse(wants_release_picker({'download_variants': [{'format': '.epub'}]}))

    def test_single_source_choices_map_back_to_connectors(self):
        choices = single_source_choices()
        self.assertEqual(len(choices), 15)
        self.assertIn('The Pirate Bay (torrent)', choices)
        self.assertIn('NZBGeek (Usenet)', choices)
        self.assertIn('AniDB (metadata)', choices)
        self.assertEqual(spec_for_choice("Vimm's Lair (download)").key, 'vimm')
        self.assertIsNone(spec_for_choice('Jackett (torrents)'))


class SourcesGuiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.stack = contextlib.ExitStack()
        self.info = self.stack.enter_context(patch('tkinter.messagebox.showinfo'))
        self.stack.enter_context(patch('tkinter.messagebox.showerror'))
        self.stack.enter_context(patch('webbrowser.open'))
        self.stack.enter_context(patch('media_intake.kindle._dpapi_crypt', side_effect=RuntimeError('no DPAPI')))
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.stack.close()
            self.skipTest(f'No display: {exc}')
        self.root.withdraw()
        self.app = gui.MediaIntakeApp(self.root, data_root=self.temp.name)
        self.app._worker = self.run_inline  # background work finishes before the assertions
        self.root.update()

    @staticmethod
    def run_inline(_message, action, callback=None, on_error=None):
        try:
            result = action()
        except Exception as exc:
            if on_error is None:
                raise
            on_error(exc)
            return
        if callback:
            callback(result)

    def tearDown(self):
        for job in self.root.tk.splitlist(self.root.tk.call('after', 'info')):
            self.root.after_cancel(job)
        self.root.destroy()
        self.app.db.close()
        self.stack.close()
        self.temp.cleanup()

    def approve(self, *keys):
        path = Path(self.app.rights_evidence_path)
        document = json.loads(path.read_text(encoding='utf-8'))
        for key in keys:
            document['sources'][key]['approved_for_download'] = True
        path.write_text(json.dumps(document), encoding='utf-8')

    def flush(self):
        for _ in range(5):
            self.root.update()

    def test_sources_page_lists_every_source_and_saves_settings(self):
        panel = self.app.sources_panel
        self.assertEqual(len(panel.view.get_children()), 15)
        status = panel.view.item('nzbgeek', 'values')[2]
        self.assertIn('Downloads approved: NO', status)
        self.assertIn('Needs API key', status)
        panel.view.selection_set('nzbgeek')
        self.flush()
        panel.fields['api_key'].set('SECRET')
        panel.fields['base'].set('https://api.nzbgeek.info')
        panel.enabled.set(False)
        panel.save(CONNECTORS['nzbgeek'])
        config = load_source_config(self.app.db, 'nzbgeek')
        self.assertEqual((config['api_key'], config['enabled']), ('SECRET', False))
        self.assertIn('session only', panel.status.get())
        self.assertNotIn('NZBGeek', gui.connected_download_sources(
            'Videos', {'source_configs': {'nzbgeek': config}}))
        self.assertIn('Off', panel.view.item('nzbgeek', 'values')[2])

    def test_test_search_does_not_contact_an_unapproved_source(self):
        panel = self.app.sources_panel
        panel.view.selection_set('thepiratebay')
        self.flush()
        with patch('media_intake.sources_ui.search_one') as searcher:
            panel.test(CONNECTORS['thepiratebay'])
        searcher.assert_not_called()
        self.assertIn('NO in rights-evidence.json', panel.status.get())

    def test_handing_off_a_1337x_release_sends_its_magnet_not_the_page(self):
        from media_intake.sources_ui import hand_off_torrents
        with FakeSite() as site:
            site.route('GET', r'/torrent/1/.*', Reply('<a href="magnet:?xt=urn:btih:' + 'b' * 40 + '">m</a>'))
            from media_intake.connectors import save_source_config
            save_source_config(self.app.db, '1337x', {'base': site.base})
            row = {'title': 'Sintel', 'format': '.torrent', 'url': site.base + '/torrent/1/sintel/',
                   'details_url': site.base + '/torrent/1/sintel/', 'resolve_connector': '1337x',
                   'source_key': '1337x', 'external_download': True}
            with patch.object(self.app, '_send_to_downloader') as send:
                hand_off_torrents(self.app, [row])
        sent = send.call_args.args[0][0]
        self.assertTrue(sent['handoff_url'].startswith('magnet:?xt=urn:btih:bbbb'))

    def test_a_malformed_rights_file_is_reported_not_raised(self):
        Path(self.app.rights_evidence_path).write_text('{not json', encoding='utf-8')
        self.app.query.set('x')
        self.app.source.set('The Pirate Bay (torrent)')
        with patch('tkinter.messagebox.showerror') as error:
            self.app.search()
        self.assertIn('could not be read', error.call_args.args[1])

    def test_torrent_sources_offer_jackett_routing(self):
        panel = self.app.sources_panel
        panel.view.selection_set('1337x')
        self.flush()
        panel.access.set('jackett')
        panel.save(CONNECTORS['1337x'])
        self.assertEqual(load_source_config(self.app.db, '1337x')['access'], 'jackett')
        self.assertIn('via jackett', panel.view.item('1337x', 'values')[2])

    def test_one_source_search_is_refused_until_approved_then_lists_releases(self):
        with FakeSite() as site:
            site.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            from media_intake.connectors import save_source_config
            save_source_config(self.app.db, 'thepiratebay', {'base': site.base})
            self.app.query.set('night of the living dead')
            self.app.search_category.set('Videos')
            self.app.source.set('The Pirate Bay (torrent)')
            self.app.search()
            self.flush()
            self.assertIn('NO in the local rights-evidence.json', self.info.call_args.args[1])
            self.assertEqual(site.requests, [])
            self.approve('thepiratebay')
            self.app.search()
            self.flush()
        rows = self.app.results_view.get_children()
        self.assertEqual(len(rows), 3)
        values = self.app.results_view.item(rows[0], 'values')
        self.assertEqual(values[1], 'Night of the Living Dead')
        self.assertEqual(values[5], '1080p · torrent')
        self.assertIn('120 seeders', values[6])

    def test_category_search_groups_releases_and_the_picker_queues_one(self):
        self.approve('thepiratebay', 'yts')
        with FakeSite() as site, \
                patch('media_intake.discovery.search_tvmaze', return_value=[]), \
                patch('media_intake.discovery._query_wikidata', return_value=[]), \
                patch('media_intake.discovery._internet_archive_files', return_value=([], [])), \
                patch('media_intake.discovery._meawfy_files', return_value=([], [])), \
                patch('media_intake.discovery._softarchive_files', return_value=([], [])):
            site.route('GET', '/q.php', Reply(fixture('tpb_search.json'), content_type='application/json'))
            site.route('GET', '/api/v2/list_movies.json', Reply(fixture('yts_list.json'),
                                                                content_type='application/json'))
            from media_intake.connectors import save_source_config
            for key in ('thepiratebay', 'yts'):
                save_source_config(self.app.db, key, {'base': site.base})
            self.app.query.set('night of the living dead')
            self.app.search_category.set('Videos')
            self.app.source.set('All connected in category')
            self.app.search()
            self.flush()
            self.assertEqual(str(self.app.load_more_button['state']), 'normal')  # YTS has a second page
            index = next(i for i, row in enumerate(self.app.results)
                         if len(row.get('download_variants') or []) == 3)
            self.app.results_view.selection_set(str(index))
            values = self.app.results_view.item(str(index), 'values')
            self.assertEqual(values[6], '3 releases · The Pirate Bay, YTS')
            with patch.object(ReleasePicker, '__init__', return_value=None) as picker:
                self.app.queue_result()
            picker.assert_called_once()
            dialog = ReleasePicker(self.app, self.app.results[index])
            self.flush()
            self.assertEqual(len(dialog.view.get_children()), 3)
            self.assertIn('The Pirate Bay, YTS', dialog.view.item('0', 'values')[4])
            dialog.sort('size')
            dialog.view.selection_set('0')
            dialog.queue()
            self.flush()
        queued = self.app.db.execute('SELECT metadata FROM items').fetchall()
        self.assertEqual(len(queued), 1)
        item = json.loads(queued[0][0])
        self.assertEqual(item['format'], '.torrent')
        self.assertTrue(item['url'].startswith('magnet:?xt=urn:btih:'))
        self.assertEqual(item['rights_evidence_path'], str(self.app.rights_evidence_path))

    def test_category_info_names_sources_waiting_for_approval_or_setup(self):
        self.app.search_category.set('Videos')
        self.app._update_search_category_info()
        info = self.app.search_category_info.get()
        self.assertIn('The Pirate Bay (needs YES in rights file)', info)
        self.approve('nzbgeek')
        self.app._update_search_category_info()
        self.assertIn('NZBGeek (needs setup)', self.app.search_category_info.get())


if __name__ == '__main__':
    unittest.main()
