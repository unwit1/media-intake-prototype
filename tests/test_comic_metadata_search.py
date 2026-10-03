import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from media_intake import comic_metadata as cm
from media_intake.comics import list_comicvine_issues
from media_intake.core import connect
from media_intake.discovery import _catalog_rows, _merge_same_title, _query_wikidata, search_media_category
from media_intake.gui import MediaIntakeApp
from media_intake.watched import add_watch, _comic_issue_number_matches, _process_comic_issue_watch


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/comic_metadata/catalogs.json').read_text(encoding='utf-8'))


def issue(provider='Comic Vine', number='1', year='2016', **fields):
    return {'title': f'Sample Hero #{number}', 'provider': provider, 'kind': 'comic_issue',
            'comic_entity': 'issue', 'series_title': 'Sample Hero', 'issue_number': number,
            'series_start_year': year, 'source_url': f'https://catalog.example/{provider}/{year}/{number}',
            **fields}


def merge(rows):
    return _merge_same_title(_catalog_rows(rows, 'Comics'), 'Comics', ('GetComics',))


class IssueIdentityTest(unittest.TestCase):
    def test_query_separates_number_and_run(self):
        parsed = cm.parse_query('Sample Hero (2016) #001.50')
        self.assertEqual((parsed.title, parsed.number, parsed.year), ('Sample Hero', '1.5', '2016'))

    def test_query_keeps_explicit_trade_intent(self):
        self.assertTrue(cm.parse_query('Sample Hero TPB').collections)

    def test_query_rejects_unbounded_and_control_input(self):
        for value in ('', 'x' * 151, 'Hero\n#1'):
            with self.assertRaises(ValueError):
                cm.parse_query(value)

    def test_duplicate_issues_merge_sources_ids_and_field_provenance(self):
        rows = [issue(external_ids=[cm._id('comicvine_issue', 100)], publisher='Example Press'),
                issue('GCD', external_ids=[cm._id('gcd_issue', 400)], language='en', cover='https://covers.example/1.jpg')]
        merged = merge(rows)
        self.assertEqual(len(merged), 1)
        item = merged[0]
        self.assertEqual(item['title'], 'Sample Hero (2016) #1')
        self.assertEqual(set(item['metadata_sources']), {'Comic Vine', 'GCD'})
        self.assertEqual(cm._ids(item), {('comicvine_issue', '100'), ('gcd_issue', '400')})
        self.assertEqual(item['comic_metadata_provenance']['language'][0]['provider'], 'GCD')
        self.assertEqual(item['cover'], 'https://covers.example/1.jpg')
        self.assertEqual(item['metadata_provider'], 'Comic Vine')
        self.assertEqual(len(item['metadata_variants']), 2)

    def test_reboot_runs_remain_separate(self):
        rows = [issue(year='2016'), issue('GCD', year='2024')]
        self.assertEqual(len(merge(rows)), 2)

    def test_ambiguous_undated_issue_is_not_merged_into_a_reboot(self):
        self.assertEqual(len(merge([issue(year='2016'), issue('GCD', year='2024'), issue('GetComics', year='')])), 3)

    def test_undated_issue_joins_unique_run(self):
        self.assertEqual(len(merge([issue(), issue('GCD', year='')])), 1)

    def test_same_provider_conflicting_issue_ids_remain_separate(self):
        self.assertEqual(len(merge([issue(external_ids=[cm._id('comicvine_issue', 100)]),
                                    issue(external_ids=[cm._id('comicvine_issue', 101)])])), 2)

    def test_conflicting_parent_ids_remain_separate(self):
        self.assertEqual(len(merge([issue(series_external_ids=[cm._id('comicvine', 10)]),
                                    issue('GCD', series_external_ids=[cm._id('comicvine', 20)])])), 2)

    def test_known_language_or_publisher_editions_remain_separate(self):
        for field, first, second in [('language', 'en', 'fr'), ('publisher', 'A Press', 'B Press')]:
            self.assertEqual(len(merge([issue(**{field: first}), issue('GCD', **{field: second})])), 2)

    def test_shared_identifier_merges_alternate_catalog_title(self):
        a = issue(external_ids=[cm._id('comicvine_issue', 100)])
        b = issue('Metron', title='A New Start', series_title='Different spelling',
                  external_ids=[cm._id('comicvine_issue', 100), cm._id('metron_issue', 500)])
        self.assertEqual(len(merge([a, b])), 1)

    def test_cover_variants_collapse_and_remain_available(self):
        rows = [issue('GCD', external_ids=[cm._id('gcd_issue', 400)]),
                issue('GCD', source_url='https://www.comics.org/issue/401/',
                      external_ids=[cm._id('gcd_issue', 401)], variant_of='https://www.comics.org/api/issue/400/')]
        merged = merge(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]['edition_variants']), 1)
        self.assertEqual(cm._ids(merged[0]), {('gcd_issue', '400'), ('gcd_issue', '401')})

    def test_issues_precede_series_and_trades(self):
        rows = [{'title': 'Sample Hero TPB', 'kind': 'comic'},
                {'title': 'Sample Hero', 'kind': 'comic'}, issue(number='10'), issue(number='2'), issue()]
        merged = merge(rows)
        self.assertEqual([r['comic_entity'] for r in merged], ['issue', 'issue', 'issue', 'series', 'collection'])
        self.assertEqual([r['issue_number'] for r in merged[:3]], ['1', '2', '10'])
        self.assertEqual(cm.sort_comics(merged, 'Sample Hero #2')[0]['issue_number'], '2')
        self.assertEqual(cm.sort_comics(merged, 'Sample Hero TPB')[0]['comic_entity'], 'collection')

    def test_comicvine_volume_is_not_misclassified_as_trade(self):
        self.assertEqual(cm.normalize({'title': 'Sample Hero', 'comicvine_id': '10', 'volume': 2})['comic_entity'], 'series')

    def test_platform_trade_type_overrides_number(self):
        self.assertEqual(cm.normalize(issue(series_type='Trade Paperback'))['comic_entity'], 'collection')

    def test_multi_issue_packs_do_not_merge_into_issue_one(self):
        for title in ('Sample Hero #1-6', 'Sample Hero #1–#6', 'Sample Hero #1 & #2'):
            rows = merge([issue(), {'title': title, 'provider': 'GetComics', 'format': '.cbz', 'url': 'https://files.example/pack.cbz'}])
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]['comic_entity'], 'collection')

    def test_individual_issue_download_rejects_trades_packs_and_unrelated_numbers(self):
        for title in ('Sample Hero TPB #1', 'Sample Hero #1-6', 'Sample Hero #10', 'Sample Hero Complete Collection', 'Sample Hero'):
            self.assertFalse(_comic_issue_number_matches(title, '1'))
        self.assertTrue(_comic_issue_number_matches('Sample Hero #001', '1'))
        self.assertTrue(_comic_issue_number_matches('Sample Hero #1.5', '1.5'))
        self.assertFalse(_comic_issue_number_matches('Sample Hero #1A', '1'))

    def test_download_mirrors_attach_without_redundant_catalog_rows(self):
        rows = [issue(), issue('GetComics', format='.cbz', url='https://files.example/a.cbz'),
                issue('GetComics', format='.cbz', url='https://files.example/b.cbz')]
        merged = merge(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]['download_variants']), 2)
        self.assertEqual(merged[0]['metadata_provider'], 'Comic Vine')
        self.assertEqual(merged[0]['url'], 'https://files.example/a.cbz')

    def test_merge_does_not_mutate_provider_rows(self):
        rows = [issue('GCD', variant_of='https://www.comics.org/issue/100/')]
        saved = copy.deepcopy(rows)
        merge(rows)
        self.assertEqual(rows, saved)


class CatalogProviderTest(unittest.TestCase):
    def test_comicvine_expands_exact_run_into_issues_and_preserves_parent(self):
        def immediate(_provider, _identity, loader, **_kwargs): return loader()
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, _): return json.dumps(self.payload).encode()
        with patch.object(cm, '_memo', side_effect=immediate), patch('media_intake.comics.open_url', side_effect=[
                Response({'error': 'OK', 'results': FIXTURE['comicvine_series']}),
                Response({'error': 'OK', 'results': FIXTURE['comicvine_issues']})]) as fetch:
            results, errors = cm.search_comicvine_metadata('Sample Hero #1 (2016)', 'fixture-key')
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['comicvine_volume_id'], '10')
        self.assertEqual(cm._ids(results[0]), {('comicvine_issue', '100')})
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(fetch.call_args.args[0].full_url).query)
        self.assertEqual(params['filter'], ['volume:10,issue_number:1'])
        self.assertNotIn('fixture-key', json.dumps(results))

    def test_comicvine_issue_lookup_failure_preserves_series(self):
        with patch.object(cm, '_memo', side_effect=lambda _p, _i, loader, **_k: loader()), \
                patch('media_intake.comics.search_comicvine', return_value=[{'title': 'Sample Hero', 'comicvine_id': '10', 'source_url': 'https://catalog.example/10'}]), \
                patch('media_intake.comics.list_comicvine_issues', side_effect=ValueError('temporarily unavailable')):
            rows, errors = cm.search_comicvine_metadata('Sample Hero', 'key')
        self.assertEqual(len(rows), 1)
        self.assertIn('temporarily unavailable', errors[0])

    def test_comicvine_filter_cannot_inject_another_filter(self):
        with self.assertRaisesRegex(ValueError, 'issue number'):
            list_comicvine_issues('10', 'key', number='1,volume:20')

    def test_gcd_number_search_matches_series_year_not_issue_year(self):
        with patch.object(cm, '_pages', side_effect=[FIXTURE['gcd_series']['results'], FIXTURE['gcd_issues']['results']]) as pages:
            rows, errors = cm.search_gcd('Sample Hero (2016) #1')
        self.assertEqual(errors, [])
        self.assertIn('year/2016/', pages.call_args_list[0].args[1])
        self.assertNotIn('year/', pages.call_args_list[1].args[1])
        self.assertEqual(len(merge(rows)), 2)  # one issue, one series
        self.assertEqual(rows[1]['series_start_year'], '2016')
        self.assertEqual(rows[1]['catalog_metadata']['attribution'], 'Grand Comics Database')

    def test_gcd_overview_discovers_issues_without_per_issue_requests(self):
        with patch.object(cm, '_pages', side_effect=[FIXTURE['gcd_series']['results'], FIXTURE['gcd_overview']['results']]) as pages:
            rows, errors = cm.search_gcd('Sample Hero')
        self.assertEqual(errors, [])
        self.assertEqual(pages.call_count, 2)
        self.assertTrue(pages.call_args.args[1].endswith('/series/40/overview/'))
        self.assertEqual([row['issue_number'] for row in rows[1:]], ['1', '2'])
        self.assertEqual(rows[1]['release_date'], '2016-07-06')

    def test_gcd_failed_overview_keeps_series_and_reports_error(self):
        with patch.object(cm, '_pages', side_effect=[FIXTURE['gcd_series']['results'], ValueError('HTTP 500')]):
            rows, errors = cm.search_gcd('Sample Hero')
        self.assertEqual(len(rows), 1)
        self.assertIn('500', errors[0])

    def test_metron_auth_and_number_filter_use_documented_endpoints(self):
        with patch.object(cm, '_pages', side_effect=[FIXTURE['metron_series']['results'], FIXTURE['metron_issues']['results']]) as pages:
            rows, errors = cm.search_metron('Sample Hero (2016) #1', 'fixture-token')
        self.assertEqual(errors, [])
        self.assertEqual(pages.call_args.args[2], {'Authorization': 'Bearer fixture-token'})
        self.assertIn('number=1', pages.call_args.args[1])
        self.assertIn('series_year_began=2016', pages.call_args.args[1])
        self.assertEqual(rows[1]['comicvine_volume_id'], '10')
        self.assertEqual(rows[1]['series_external_ids'][2]['external_id'], '40')
        self.assertNotIn('fixture-token', json.dumps(rows))

    def test_metron_series_cross_ids_merge_with_cv_and_gcd(self):
        with patch.object(cm, '_pages', side_effect=[FIXTURE['metron_series']['results'], []]):
            rows, _ = cm.search_metron('Sample Hero', 'token')
        rows.append({'title': 'Sample Hero', 'provider': 'Comic Vine', 'external_ids': [cm._id('comicvine', 10)], 'start_year': 2016})
        self.assertEqual(len(merge(rows)), 1)

    def test_metron_failed_issue_query_retains_series(self):
        with patch.object(cm, '_pages', side_effect=[FIXTURE['metron_series']['results'], ValueError('HTTP 429')]):
            rows, errors = cm.search_metron('Sample Hero', 'token')
        self.assertEqual(len(rows), 1)
        self.assertIn('429', errors[0])

    def test_optional_catalogs_require_credentials(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual([name for name, _ in cm.metadata_tasks('Hero')], ['Grand Comics Database'])
            self.assertEqual([name for name, _ in cm.metadata_tasks('Hero', {'comicvine_api_key': 'cv', 'metron_api_token': 'm'})],
                             ['Comic Vine', 'Grand Comics Database', 'Metron'])

    def test_metron_invalid_header_token_is_rejected_without_exposing_it(self):
        token = 'secret-token\nX-Evil: injected'
        with self.assertRaises(ValueError) as raised:
            cm.search_metron('Hero', token)
        self.assertNotIn('secret-token', str(raised.exception))

    def test_wikidata_filters_characters_but_keeps_comics_by_writers(self):
        with patch('media_intake.discovery.search_wikidata', return_value=[
                {'title': 'Hero', 'description': 'fictional comic book character', 'qid': 'Q1'},
                {'title': 'Hero', 'description': 'comic book series by American writer Example', 'qid': 'Q2'}]):
            rows = _query_wikidata('Hero', 'Comics')
        self.assertEqual([row['qid'] for row in rows], ['Q2'])

    def test_wikidata_print_volume_and_numbered_issue_are_classified_separately(self):
        with patch('media_intake.discovery.search_wikidata', return_value=[
                {'title': 'Hero – Volume 1: Awakening', 'description': 'first volume in the Hero comic book series', 'qid': 'Q3'},
                {'title': 'Hero #1', 'description': 'comic book issue', 'qid': 'Q4'}]):
            rows = _query_wikidata('Hero', 'Comics')
        self.assertEqual([row['comic_entity'] for row in rows], ['collection', 'issue'])


class CatalogTransportTest(unittest.TestCase):
    def setUp(self):
        with cm._GUARD:
            cm._CACHE.clear(); cm._HOSTS.clear()

    def test_cache_is_bounded_deep_copied_and_secrets_are_hashed(self):
        load = Mock(return_value=[{'title': 'Hero'}])
        first = cm._memo('Test', ('query', 'secret-token'), load, interval=0)
        first[0]['title'] = 'Changed'
        self.assertEqual(cm._memo('Test', ('query', 'secret-token'), load, interval=0)[0]['title'], 'Hero')
        self.assertEqual(load.call_count, 1)
        for i in range(130):
            cm._memo('Test', i, lambda: [], interval=0)
        self.assertLessEqual(len(cm._CACHE), 128)
        self.assertNotIn('secret-token', repr(cm._CACHE))

    def test_429_backs_off_and_redacts_authenticated_url(self):
        secret_url = 'https://api.example/?api_key=secret-token'
        failure = urllib.error.HTTPError(secret_url, 429, 'Too many', {'Retry-After': '120'}, io.BytesIO())
        load = Mock(side_effect=failure)
        with self.assertRaises(ValueError) as raised:
            cm._memo('Comic Vine', 'search', load, interval=0)
        self.assertNotIn('secret-token', str(raised.exception))
        with self.assertRaisesRegex(ValueError, 'rate limited'):
            cm._memo('Comic Vine', 'other', load, interval=0)
        self.assertEqual(load.call_count, 1)

    def test_same_provider_serializes_concurrent_identical_queries(self):
        results = []
        load = Mock(return_value=[{'title': 'Hero'}])
        threads = [threading.Thread(target=lambda: results.append(cm._memo('Test', 'query', load, interval=0))) for _ in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(len(results), 12)
        self.assertEqual(load.call_count, 1)

    def test_pagination_is_bounded(self):
        with patch.object(cm, '_json', side_effect=[{'results': [{'id': 1}], 'next': '?page=2'},
                                                  {'results': [{'id': 2}], 'next': '?page=3'}]) as request:
            rows = cm._pages('GCD', 'https://www.comics.org/api/series/', max_pages=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(request.call_count, 2)

    def test_pagination_loop_is_not_followed(self):
        url = 'https://www.comics.org/api/series/'
        with patch.object(cm, '_json', return_value={'results': [], 'next': url}):
            with self.assertRaisesRegex(ValueError, 'loop'):
                cm._pages('GCD', url)

    def test_authenticated_pagination_cannot_leave_provider(self):
        with patch('media_intake.comic_metadata.urllib.request.build_opener') as opener:
            for url in ('http://metron.cloud/api/', 'https://other.example/api/', 'https://user:pass@metron.cloud/api/'):
                with self.assertRaises(ValueError):
                    cm._json('Metron', url, {'Authorization': 'Bearer token'})
        opener.assert_not_called()

    def test_json_client_blocks_automatic_redirects_and_sends_auth_header(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"results": []}'
        opener = Mock()
        opener.open.return_value = response
        with patch('media_intake.comic_metadata.urllib.request.build_opener', return_value=opener) as build:
            cm._json('Metron', 'https://metron.cloud/api/series/', {'Authorization': 'Bearer token'}, interval=0)
        self.assertIsInstance(build.call_args.args[0], cm.NoRedirect)
        self.assertEqual(opener.open.call_args.args[0].get_header('Authorization'), 'Bearer token')


class ComicSearchIntegrationTest(unittest.TestCase):
    def test_issue_watch_does_not_persist_search_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            watch_id = cm.add_issue_watch(db, issue(), {'comicvine_api_key': 'private-cv-key',
                'metron_api_token': 'private-metron-key', 'rights_evidence_path': 'rights.json'})
            payload = db.execute('SELECT metadata FROM watched_items WHERE id=?', (watch_id,)).fetchone()[0]
            self.assertNotIn('private-', payload)
            self.assertEqual(json.loads(payload)['sources'], {'rights_evidence_path': 'rights.json'})

    def test_added_issue_downloads_from_another_approved_connector_and_skips_trade(self):
        from media_intake.rights_evidence import ensure_rights_evidence, load_rights_evidence, save_rights_evidence
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('001.png', b'original test page')
        body = content.getvalue()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Type', 'application/zip')
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
                path = ensure_rights_evidence(Path(tmp) / 'rights.json')
                document = load_rights_evidence(path)
                document['sources']['internet_archive']['approved_for_download'] = True
                save_rights_evidence(path, document)
                sources = {'rights_evidence_path': str(path)}
                item = merge([issue(external_ids=[cm._id('comicvine_issue', 100)])])[0]
                watch_id = cm.add_issue_watch(db, item, sources)
                watch = dict(db.execute('SELECT * FROM watched_items WHERE id=?', (watch_id,)).fetchone())
                candidates = [{'title': title, 'author': 'Unknown', 'format': '.cbz', 'url': base + url,
                               'source': 'https://archive.org/details/fixture', 'source_key': 'internet_archive',
                               'rights_evidence_path': str(path)} for title, url in (
                                   ('Sample Hero TPB #1', '/trade.cbz'), ('Sample Hero (2024) #1', '/wrong-run.cbz'),
                                   ('Sample Hero #1', '/issue.cbz'))]
                with patch('media_intake.watched._search_candidates', return_value=(candidates, [])):
                    self.assertEqual(_process_comic_issue_watch(db, watch, str(Path(tmp) / 'library')), 1)
                saved = db.execute('SELECT state,file_path FROM watched_items WHERE id=?', (watch_id,)).fetchone()
                self.assertEqual(saved['state'], 'downloaded')
                self.assertEqual(Path(saved['file_path']).read_bytes(), body)
                self.assertEqual(requests, ['/issue.cbz'])
                queued = json.loads(db.execute('SELECT metadata FROM items WHERE state=\'downloaded\'').fetchone()[0])
                self.assertEqual(queued['watch_request_id'], watch_id)
                self.assertIn(('comicvine_issue', '100'), cm._ids(queued))
                self.assertTrue(queued['comic_metadata_provenance'])
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_concurrent_search_collapses_catalogs_but_retains_trades_and_source_errors(self):
        barrier = threading.Barrier(3)
        def source(rows):
            def search():
                barrier.wait(3)
                return rows
            return search
        tasks = [('Comic Vine', source([issue(), {'title': 'Sample Hero TPB', 'kind': 'comic'}])),
                 ('GCD', source([issue('GCD')])), ('Failing source', source(([], ['HTTP 500'])))]
        with patch('media_intake.discovery._tasks', return_value=tasks):
            response = search_media_category('Sample Hero', 'Comics')
        self.assertEqual(len(response['results']), 2)
        self.assertEqual(response['comic_summary']['issue'], 1)
        self.assertEqual(response['comic_summary']['collection'], 1)
        self.assertEqual(response['errors'], ['Failing source: HTTP 500'])

    def test_merged_issue_adds_once_across_catalog_urls_and_not_as_series(self):
        item = merge([issue(external_ids=[cm._id('comicvine_issue', 100)], comicvine_id='100', comicvine_volume_id='10'),
                      issue('GCD', external_ids=[cm._id('gcd_issue', 400)])])[0]
        with tempfile.TemporaryDirectory() as tmp, closing(connect(tmp)) as db:
            series_id = add_watch(db, 'comic', 'Sample Hero', source_url='https://catalog.example/series',
                                  metadata={'external_ids': [cm._id('comicvine', 10)]})
            first = cm.add_issue_watch(db, item)
            alternate = {**item, 'metadata_url': 'https://www.comics.org/issue/400/'}
            second = cm.add_issue_watch(db, alternate)
            self.assertEqual(first, second)
            self.assertNotEqual(first, series_id)
            row = db.execute('SELECT * FROM watched_items WHERE id=?', (first,)).fetchone()
            self.assertEqual(row['kind'], 'comic_issue')
            saved = json.loads(row['metadata'])
            self.assertEqual(saved['comic_issue']['volume_id'], '10')
            self.assertTrue(saved['comic_metadata_provenance'])
            ids = {tuple(row) for row in db.execute('SELECT provider,external_id FROM media_external_ids WHERE entity_id=?', (row['media_entity_id'],))}
            self.assertIn(('comicvine_issue', '100'), ids)
            self.assertNotIn(('comicvine', '10'), ids)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0], 2)

    def test_gui_add_uses_issue_watch_with_merged_ids(self):
        item = merge([issue(external_ids=[cm._id('comicvine_issue', 100)]), issue('GCD', external_ids=[cm._id('gcd_issue', 400)])])[0]
        with tempfile.TemporaryDirectory() as tmp:
            app = SimpleNamespace(data_root=Path(tmp), _watch_sources=lambda: {}, active_profile_id=Mock(),
                                  refresh_watches=Mock(), _done=Mock(), _run_added_watch_scan=Mock())
            app.active_profile_id.get.return_value = ''
            app._worker = lambda _label, action, done: done(action())
            MediaIntakeApp._add_search_item_to_watched(app, item, 'Comics')
            with closing(connect(tmp)) as db:
                row = db.execute('SELECT kind,metadata FROM watched_items').fetchone()
                self.assertEqual(row['kind'], 'comic_issue')
                self.assertEqual(len(json.loads(row['metadata'])['external_ids']), 2)
            app.refresh_watches.assert_called_once()
            app._run_added_watch_scan.assert_called_once()

    def test_catalog_series_without_a_tracking_backend_is_not_treated_as_a_feed(self):
        app = SimpleNamespace(associate_comic_metadata=Mock())
        item = merge([{'title': 'Sample Hero', 'provider': 'GCD', 'kind': 'comic',
                       'source_url': 'https://www.comics.org/series/40/'}])[0]
        with patch('media_intake.gui.messagebox.showinfo') as notice:
            MediaIntakeApp._add_search_item_to_watched(app, item, 'Comics')
        app.associate_comic_metadata.assert_not_called()
        self.assertIn('individual issue watches', notice.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
