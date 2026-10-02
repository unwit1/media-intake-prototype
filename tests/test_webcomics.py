"""Deterministic public-layout fixtures and local end-to-end ingestion tests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from email.message import Message
import zipfile

from media_intake.core import connect, download
from media_intake.webcomics.adapters import (FeedAdapter, HtmlAdapter, WebtoonAdapter,
    XkcdAdapter, default_registry)
from media_intake.webcomics.chapters import resolve_chapter, group_entries
from media_intake.webcomics.downloads import download_entry
from media_intake.webcomics.extraction import Document, Recipe
from media_intake.webcomics.http import HttpClient, Response, ExtractionError, SourceUnavailable, retry_seconds
from media_intake.webcomics.metadata import MetadataCandidate, REGISTRY, match_entities, merge_metadata
from media_intake.webcomics.models import (Snapshot, WebcomicAsset, WebcomicEntry, WebcomicSeries,
                                         canonicalize_url, natural_key, record)
from media_intake.webcomics.resolver import resolve
from media_intake.webcomics.storage import library, migrate, set_override
from media_intake.webcomics.watcher import add_series, apply_snapshot, poll_source, queue_pending

FIXTURES = Path(__file__).parent / 'fixtures' / 'webcomics'
BASE = 'https://comic.example'
PNG = bytes.fromhex('89504e470d0a1a0a0000000d494844520000000200000003') + b'fixture'


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value, url, {})


def fixture(name):
    return (FIXTURES / name).read_bytes()


def make_snapshot(count, complete=True):
    series = WebcomicSeries('canonical', 'Comic', BASE + '/archive', 'static')
    entries = [WebcomicEntry(str(i), BASE + f'/page/{i}', f'Page {i}', ordinal=i,
                            assets=[WebcomicAsset(BASE + f'/image/{i}.png', 1)]) for i in range(1, count + 1)]
    return Snapshot(series, entries, complete=complete, metrics={'entry_count': count})


class ModelTests(unittest.TestCase):
    def test_canonicalization_preserves_identity_query_and_path(self):
        self.assertEqual(canonicalize_url('HTTPS://COMIC.EXAMPLE:443/a/?title_no=2&episode_no=3&utm_source=x#frag'),
                         'https://comic.example/a/?title_no=2&episode_no=3')
        for value in ('file:///a', 'https://user:secret@example.com/', 'javascript:alert(1)', 'https://a/\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonicalize_url(value)

    def test_natural_order(self):
        self.assertEqual(sorted(['Page 10', 'Page 2', 'Page 1'], key=natural_key), ['Page 1', 'Page 2', 'Page 10'])

    def test_chapter_evidence_and_episode_distinction(self):
        for title in ('Chapter 1', 'Ch. 1', 'Book One', 'Volume 2', 'Arc IV', 'Part 3', 'Prologue', 'Epilogue', 'Interlude'):
            with self.subTest(title=title):
                self.assertEqual(resolve_chapter(title, BASE).organization, 'INFERRED')
        self.assertIsNone(resolve_chapter('Episode 12', BASE))
        self.assertEqual(resolve_chapter('Chapter 99', BASE, declared='Creator Arc').title, 'Creator Arc')
        self.assertIsNone(resolve_chapter('Chapter 99', BASE, override=''))

    def test_metadata_merge_keeps_disagreement_and_locks(self):
        series = make_snapshot(1).series
        merge_metadata(series, [MetadataCandidate('title', 'Low', 'og', BASE, .6),
                                MetadataCandidate('title', 'High', 'api', BASE, 1)])
        self.assertEqual(series.title, 'High')
        self.assertEqual(len(series.metadata_provenance['title']['candidates']), 2)
        merge_metadata(series, [MetadataCandidate('title', 'Locked', 'manual', BASE, 1.1)], locks={'title'})
        self.assertEqual(series.title, 'High')

    def test_duplicate_match_never_auto_merges_fuzzy_titles(self):
        left, right = make_snapshot(1).series, make_snapshot(1).series
        self.assertTrue(match_entities(left, right)['auto_merge'])
        right.canonical_source_url = 'https://mirror.example/comic'
        left.creators = right.creators = [{'name': 'Creator', 'role': 'author'}]
        self.assertFalse(match_entities(left, right)['auto_merge'])
        self.assertEqual(match_entities(left, right)['confidence'], .85)


class AdapterTests(unittest.TestCase):
    def test_static_archive_chapters_duplicates(self):
        result = HtmlAdapter().resolve_series(BASE, FakeClient({BASE: fixture('static.html')}))
        self.assertEqual(len(result.entries), 3)
        self.assertEqual([c.organization for c in result.chapters], ['SOURCE_DECLARED'] * 2)
        self.assertEqual(result.entries[-1].content_type, 'bonus')

    def test_wordpress_multiple_images(self):
        adapter = default_registry().adapters['wordpress']
        client = FakeClient({BASE: fixture('wordpress.html'), BASE + '/comic/hello': fixture('wordpress-entry.html')})
        snapshot = adapter.resolve_series(BASE, client)
        entry = adapter.resolve_assets(snapshot.entries[0], client)
        self.assertEqual(len(entry.assets), 2)
        self.assertEqual(entry.assets[0].ordinal, 1)
        self.assertEqual(snapshot.chapters[0].title, 'Book One')

    def test_rss_atom_order_and_native_ids(self):
        for name in ('rss.xml', 'atom.xml'):
            result = FeedAdapter().resolve_series(BASE + '/feed', FakeClient({BASE + '/feed': fixture(name)}))
            self.assertEqual(len(result.entries), 2)
            self.assertTrue(result.entries[0].source_url.endswith(('/2', '/1')))
            self.assertFalse(result.complete)
            self.assertFalse(result.chapters)
            self.assertNotEqual(result.entries[0].source_id, result.entries[0].source_url)

    def test_vertical_scroll_and_viewer_to_series(self):
        adapter = WebtoonAdapter()
        url = 'https://www.webtoons.com/en/canvas/scroll/episode-1/viewer?title_no=42&episode_no=1'
        canonical = adapter.canonicalize_url(url)
        client = FakeClient({canonical: fixture('webtoon.html')})
        snapshot = adapter.resolve_series(url, client)
        entry = snapshot.entries[0]
        client.pages[entry.source_url] = fixture('webtoon-entry.html')
        self.assertEqual([e.source_id for e in snapshot.entries], ['1', '2'])
        self.assertEqual(len(adapter.resolve_assets(entry, client).assets), 2)
        self.assertFalse(snapshot.chapters)

    def test_js_only_is_explicit_failure(self):
        with self.assertRaises(ExtractionError):
            resolve(BASE, FakeClient({BASE + '/': fixture('js-only.html')}))

    def test_structured_metadata(self):
        candidates = REGISTRY.extract(Document(fixture('structured.html')), BASE)
        series = merge_metadata(make_snapshot(1).series, candidates)
        self.assertEqual(series.title, 'Structured Comic')
        self.assertEqual(series.creators[0]['name'], 'Creator')
        self.assertEqual(series.alternate_titles, ['Alt Comic'])

    def test_feed_autodiscovery(self):
        client = FakeClient({BASE + '/': b'<link rel="alternate" type="application/rss+xml" href="/feed">', BASE + '/feed': fixture('rss.xml')})
        adapter, snapshot, config = resolve(BASE, client)
        self.assertEqual(adapter.id, 'feed')
        self.assertEqual(config['discovered_from'], BASE + '/')

    def test_recipe_validation_and_pagination_loop(self):
        with self.assertRaises(ValueError):
            Recipe.from_dict({'image_selector': 'img[href=x]'})
        recipe = Recipe(next_selector='a.next')
        body = b'<div id="archive"><a href="/page/1">Page 1</a></div><a class="next" href="/">Next</a>'
        with self.assertRaises(ExtractionError):
            HtmlAdapter(recipe).resolve_series(BASE + '/', FakeClient({BASE + '/': body}))

    def test_xkcd_incremental_and_known_missing_404(self):
        data = {'num': 405, 'title': 'Latest', 'year': '2026', 'month': '1', 'day': '1', 'img': BASE + '/image.png'}
        client = FakeClient({'https://xkcd.com/info.0.json': json.dumps(data).encode()})
        initial = XkcdAdapter().resolve_series('https://xkcd.com/', client)
        self.assertEqual(len(initial.entries), 404)
        self.assertNotIn('404', [e.source_id for e in initial.entries])
        updated = XkcdAdapter().resolve_series('https://xkcd.com/', client, {'latest_number': 403})
        self.assertEqual([e.source_id for e in updated.entries], ['405'])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        self.db.execute("INSERT INTO watched_items(kind,title,state,metadata) VALUES('webcomic','Comic','watching','{}')")
        self.db.execute("INSERT INTO webcomic_series(id,metadata) VALUES('canonical','{}')")
        self.db.execute("INSERT INTO webcomic_sources(series_id,watch_id,url,adapter,adapter_version) VALUES('canonical',1,?,'static','1')", (BASE + '/archive',))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_migration_is_additive_idempotent(self):
        migrate(self.db)
        migrate(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM watched_items').fetchone()[0], 1)

    def test_initial_baseline_and_bounded_backlog(self):
        apply_snapshot(self.db, 1, make_snapshot(12), baseline=True)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM webcomic_entries WHERE state='known'").fetchone()[0], 12)
        apply_snapshot(self.db, 1, make_snapshot(25))
        source = dict(self.db.execute('SELECT * FROM webcomic_sources').fetchone())
        self.assertEqual(len(queue_pending(self.db, source, 10)), 10)
        self.assertEqual(len(queue_pending(self.db, source, 10)), 3)
        self.assertEqual(len(queue_pending(self.db, source, 10)), 0)

    def test_parser_collapse_preserves_entries_and_cursor(self):
        apply_snapshot(self.db, 1, make_snapshot(100), baseline=True)
        self.db.commit()
        cursor = self.db.execute('SELECT cursor FROM webcomic_sources').fetchone()[0]
        with self.assertRaises(ExtractionError):
            apply_snapshot(self.db, 1, make_snapshot(0))
        with self.assertRaises(ExtractionError):
            apply_snapshot(self.db, 1, make_snapshot(2))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM webcomic_entries').fetchone()[0], 100)
        self.assertEqual(self.db.execute('SELECT cursor FROM webcomic_sources').fetchone()[0], cursor)

    def test_rotated_feed_never_removes_old_entries(self):
        apply_snapshot(self.db, 1, make_snapshot(20), baseline=True)
        snapshot = make_snapshot(21, complete=False)
        snapshot.entries = snapshot.entries[-2:]
        apply_snapshot(self.db, 1, snapshot)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM webcomic_entries').fetchone()[0], 21)
        self.assertEqual(self.db.execute('SELECT SUM(removed) FROM webcomic_entries').fetchone()[0], 0)

    def test_revision_rename_and_chapter_override(self):
        initial = make_snapshot(1)
        apply_snapshot(self.db, 1, initial, baseline=True)
        changed = copy.deepcopy(initial)
        changed.entries[0].title = 'New name'
        changed.entries[0].assets[0].source_url = BASE + '/new.png'
        set_override(self.db, 1, '1', 'chapter', 'Manual arc')
        apply_snapshot(self.db, 1, changed)
        events = {row[0] for row in self.db.execute('SELECT event_type FROM webcomic_events')}
        self.assertTrue({'UPDATED_ENTRY', 'RENAMED_ENTRY', 'CHAPTER_REASSIGNED', 'CHAPTER_ADDED'} <= events)
        chapter = json.loads(self.db.execute('SELECT metadata FROM webcomic_chapters').fetchone()[0])
        self.assertEqual(chapter['organization'], 'MANUAL')

    def test_complete_archive_removal_is_soft(self):
        apply_snapshot(self.db, 1, make_snapshot(3), baseline=True)
        apply_snapshot(self.db, 1, make_snapshot(2))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM webcomic_entries').fetchone()[0], 3)
        self.assertEqual(self.db.execute('SELECT SUM(removed) FROM webcomic_entries').fetchone()[0], 1)

    def test_failure_records_health_without_advancing_cursor(self):
        apply_snapshot(self.db, 1, make_snapshot(1), baseline=True)
        self.db.commit()
        cursor = self.db.execute('SELECT cursor FROM webcomic_sources').fetchone()[0]
        with self.assertRaises(SourceUnavailable):
            poll_source(self.db, 1, FakeClient({BASE + '/archive': SourceUnavailable(500, BASE)}))
        source = self.db.execute('SELECT * FROM webcomic_sources').fetchone()
        self.assertEqual(source['health'], 'SOURCE_UNREACHABLE')
        self.assertEqual(source['failures'], 1)
        self.assertEqual(source['cursor'], cursor)

    def test_add_watch_to_archive_and_ui_library(self):
        url = BASE + '/comic'
        client = FakeClient({url: fixture('static.html')})
        watch_id = add_series(self.db, url, client=client, archive_existing=True)
        source = dict(self.db.execute('SELECT * FROM webcomic_sources WHERE watch_id=?', (watch_id,)).fetchone())
        ids = queue_pending(self.db, source, 1)
        row = self.db.execute('SELECT * FROM items WHERE id=?', (ids[0],)).fetchone()
        client.pages[BASE + '/page/1'] = b'<div id="comic"><img src="/image.png"></div>'
        client.pages[BASE + '/image.png'] = Response(PNG, BASE + '/image.png', {'content-type': 'image/png'})
        result = download_entry(self.db, row, self.temp.name, 1000000, client=client)
        with zipfile.ZipFile(result['file_path']) as archive:
            self.assertEqual(archive.read('0001.png'), PNG)
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(manifest['assets'][0]['checksum'], hashlib.sha256(PNG).hexdigest())
            self.assertEqual(manifest['chapter']['organization'], 'SOURCE_DECLARED')
            self.assertIn('ComicInfo.xml', archive.namelist())
            self.assertEqual(archive.getinfo('0001.png').compress_type, zipfile.ZIP_STORED)
        view = next(row for row in library(self.db) if row['watch_id'] == watch_id)
        self.assertEqual(view['archived'], 1)
        self.assertEqual(view['assets'], 1)
        self.assertEqual(len(queue_pending(self.db, source, 1)), 1)  # remaining backlog resumes

    def test_shared_scheduler_detects_downloads_and_preserves_manual_chapter(self):
        from media_intake.watched import process_due_watches, remove_watch
        url = BASE + '/comic'
        client = FakeClient({url: fixture('static.html')})
        watch_id = add_series(self.db, url, client=client)
        source_id = self.db.execute('SELECT id FROM webcomic_sources WHERE watch_id=?', (watch_id,)).fetchone()[0]
        client.pages[url] = fixture('static.html') + b'<div id="archive"><h2>Chapter 3</h2><a href="/page/4">Page 4</a></div>'
        client.pages[BASE + '/page/4'] = b'<div id="comic"><img src="/four.png"></div>'
        client.pages[BASE + '/four.png'] = Response(PNG, BASE + '/four.png', {'content-type': 'image/png'})
        with patch('media_intake.webcomics.watcher.HttpClient', return_value=client), \
             patch('media_intake.webcomics.downloads.HttpClient', return_value=client):
            process_due_watches(self.db, str(Path(self.temp.name) / 'library'), force=True)
        view = next(row for row in library(self.db) if row['watch_id'] == watch_id)
        self.assertEqual(view['archived'], 1)
        self.assertEqual(view['entries'], 4)
        key = self.db.execute("SELECT entry_key FROM webcomic_entries WHERE source_id=? AND state='archived'", (source_id,)).fetchone()[0]
        set_override(self.db, source_id, key, 'chapter', 'Corrected arc')
        remove_watch(self.db, watch_id)
        restored_id = add_series(self.db, url, client=client)
        restored = next(row for row in library(self.db) if row['watch_id'] == restored_id)
        self.assertEqual(restored['archived'], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM webcomic_sources WHERE url=?', (url,)).fetchone()[0], 1)

    def test_failed_asset_does_not_block_later_entry_in_same_batch(self):
        from media_intake.watched import process_due_watches
        url = BASE + '/comic'
        client = FakeClient({url: fixture('static.html')})
        watch_id = add_series(self.db, url, client=client, archive_existing=True)
        for number in (1, 2, 10):
            client.pages[BASE + f'/page/{number}'] = f'<div id="comic"><img src="/image/{number}.png"></div>'.encode()
            client.pages[BASE + f'/image/{number}.png'] = Response(PNG, BASE + f'/image/{number}.png', {'content-type': 'image/png'})
        client.pages[BASE + '/image/1.png'] = SourceUnavailable(404, BASE)
        with patch('media_intake.webcomics.watcher.HttpClient', return_value=client), \
             patch('media_intake.webcomics.downloads.HttpClient', return_value=client):
            process_due_watches(self.db, str(Path(self.temp.name) / 'library'), force=True)
        view = next(row for row in library(self.db) if row['watch_id'] == watch_id)
        self.assertEqual(view['archived'], 2)
        self.assertIn('404', view['last_error'])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_conditional_get_survives_new_client(self):
        client = HttpClient(self.db, interval=0, respect_robots=False)
        with patch.object(client, '_request', return_value=Response(b'body', BASE, {'etag': 'v1'})):
            client.get(BASE)
        other = HttpClient(self.db, interval=0, respect_robots=False)
        headers = Message()
        error = urllib.error.HTTPError(BASE, 304, 'Not Modified', headers, None)
        with patch.object(other, '_request', side_effect=error) as request:
            response = other.get(BASE)
        self.assertTrue(response.unchanged)
        self.assertEqual(response.body, b'body')
        self.assertEqual(request.call_args.args[1]['If-None-Match'], 'v1')

    def test_http_failures_retry_after_and_no_unbounded_wait(self):
        for code in (404, 429, 500):
            with self.subTest(code=code):
                headers = Message(); headers['Retry-After'] = '120'
                error = urllib.error.HTTPError(BASE, code, 'Failed', headers, None)
                client = HttpClient(interval=0, retries=0, respect_robots=False)
                with patch.object(client, '_request', side_effect=error), self.assertRaises(SourceUnavailable) as raised:
                    client.get(BASE)
                self.assertEqual(raised.exception.status, code)
                self.assertEqual(raised.exception.retry_after, 120)
        self.assertEqual(retry_seconds('7'), 7)
        self.assertIsNone(retry_seconds('invalid'))

    def test_robots_disallow_is_respected(self):
        client = HttpClient(interval=0)
        with patch.object(client, '_request', return_value=Response(b'User-agent: *\nDisallow: /private\n', BASE + '/robots.txt', {})) as request:
            with self.assertRaises(SourceUnavailable):
                client.get(BASE + '/private/comic')
            self.assertEqual(request.call_count, 1)

    def test_redirect_target_is_checked_before_request(self):
        client = HttpClient(interval=0, retries=0, respect_robots=False)
        headers = Message(); headers['Location'] = 'https://restricted.example/comic'
        error = urllib.error.HTTPError(BASE, 302, 'Redirect', headers, None)
        with patch.object(client, '_request', side_effect=error) as request, \
             patch.object(client, '_allowed', side_effect=lambda url: (_ for _ in ()).throw(SourceUnavailable(403, url))
                          if 'restricted.example' in url else None):
            with self.assertRaises(SourceUnavailable):
                client.get(BASE)
        self.assertEqual(request.call_count, 1)

    def test_transient_500_retries_then_recovers(self):
        client = HttpClient(interval=0, retries=1, respect_robots=False)
        headers = Message()
        error = urllib.error.HTTPError(BASE, 500, 'Failure', headers, None)
        with patch.object(client, '_request', side_effect=[error, Response(b'recovered', BASE, {})]), \
             patch('media_intake.webcomics.http.time.sleep') as sleep:
            self.assertEqual(client.get(BASE).body, b'recovered')
            sleep.assert_called_once_with(1)

    def test_last_modified_and_redirect_provenance(self):
        client = HttpClient(self.db, interval=0, respect_robots=False)
        headers = Message(); headers['Location'] = BASE + '/new'
        redirect = urllib.error.HTTPError(BASE, 301, 'Moved', headers, None)
        with patch.object(client, '_request', side_effect=[redirect,
                Response(b'body', BASE + '/new', {'last-modified': 'Mon, 01 Jan 2024 00:00:00 GMT'})]):
            self.assertEqual(client.get(BASE).url, BASE + '/new')
        cached = self.db.execute('SELECT * FROM webcomic_http_cache').fetchone()
        self.assertEqual(cached['final_url'], BASE + '/new')
        with patch.object(client, '_request', return_value=Response(b'newbody', BASE + '/', {})) as request:
            client.get(BASE)
        self.assertIn('If-Modified-Since', request.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
