"""Public Tapas series (recorded markup): paged episode lists, image lists, locked and age-gated episodes."""
import json
import tempfile
import unittest
import urllib.parse

from media_intake.core import connect
from media_intake.webcomics.http import ExtractionError, Response
from media_intake.webcomics.tapas import TapasAdapter
from media_intake.webcomics.watcher import add_series, queue_pending

SERIES = 'https://tapas.io/series/fixture-tale/info'


def list_body(numbers, locked=()):
    return ''.join(
        f'<li class="episode-item{" locked" if n in locked else ""}" data-permalink="/episode/{1000 + n}">'
        f'<a href="/episode/{1000 + n}"><p class="title">Episode {n}</p></a></li>' for n in numbers)


class TapasSite:
    def __init__(self, total=45, locked=(45,)):
        self.total, self.locked, self.calls = total, locked, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url == SERIES:
            return Response(b'<html><p class="title">Fixture Tale</p><a class="creator">Cy Creator</a>'
                            b'<button class="subscribe-btn" data-id="777">Subscribe</button></html>', url, {})
        if '/series/777/episodes' in url:
            page = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))['page'])
            numbers = range((page - 1) * 20 + 1, min(self.total, page * 20) + 1)
            data = {'data': {'body': list_body(numbers, self.locked), 'pagination': {'has_next': page * 20 < self.total}}}
            return Response(json.dumps(data).encode(), url, {})
        if url == 'https://tapas.io/episode/1001':
            return Response(b'<img class="content__img" data-src="https://us-a.tapas.io/pc/a1.png">'
                            b'<img class="content__img" data-src="https://us-a.tapas.io/pc/a2.png">', url, {})
        if url == 'https://tapas.io/episode/1002':
            return Response(b'<div class="age-gate">Confirm your age</div>', url, {})
        raise AssertionError(url)


class TapasTests(unittest.TestCase):
    def test_series_paging_images_and_locked_episodes(self):
        site = TapasSite()
        snapshot = TapasAdapter().resolve_series('https://tapas.io/series/fixture-tale', site)
        self.assertEqual([e.title for e in snapshot.entries][:2], ['Episode 1', 'Episode 2'])
        self.assertEqual(len(snapshot.entries), 45)
        self.assertEqual(sum('/series/777/episodes' in url for url in site.calls), 3)
        self.assertEqual((snapshot.series.title, snapshot.series.creators[0]['name']), ('Fixture Tale', 'Cy Creator'))
        locked = snapshot.entries[-1]
        self.assertEqual((locked.source_id, locked.content_type), ('1045', 'locked'))
        with self.assertRaisesRegex(ExtractionError, 'locked'):
            TapasAdapter().resolve_assets(locked, site)
        self.assertNotIn('https://tapas.io/episode/1045', site.calls)  # never requested
        first = TapasAdapter().resolve_assets(snapshot.entries[0], site)
        self.assertEqual([a.source_url for a in first.assets], ['https://us-a.tapas.io/pc/a1.png', 'https://us-a.tapas.io/pc/a2.png'])
        with self.assertRaisesRegex(ExtractionError, 'not publicly readable'):
            TapasAdapter().resolve_assets(snapshot.entries[1], site)

    def test_adding_by_url_uses_tapas_and_never_queues_locked(self):
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                add_series(db, SERIES, client=TapasSite(total=5, locked=(3,)), archive_existing=True,
                           config={'auto_archive': False})
                self.assertEqual(db.execute('SELECT adapter FROM webcomic_sources').fetchone()[0], 'tapas')
                source = dict(db.execute('SELECT * FROM webcomic_sources').fetchone())
                self.assertEqual(len(queue_pending(db, source, 10)), 4)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
