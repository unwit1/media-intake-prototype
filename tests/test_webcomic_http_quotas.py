"""Durable robots policy, shared host back-off, LRU cache quota and per-host pacing."""
import tempfile
import time
import unittest
import urllib.error
from email.message import Message

from media_intake.core import connect
from media_intake.webcomics import http
from media_intake.webcomics.http import HttpClient, SourceUnavailable


class Reply:
    def __init__(self, url, body, status=200, headers=None):
        self.url, self.body, self.status = url, body, status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, limit):
        return self.body[:limit]

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeWeb:
    def __init__(self):
        self.calls = []
        self.retry_after = None

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(url)
        if url.endswith('/robots.txt'):
            return Reply(url, b'User-agent: *\nDisallow: /private\n')
        if self.retry_after is not None:
            headers = Message()
            headers['Retry-After'] = str(self.retry_after)
            raise urllib.error.HTTPError(url, 429, 'Too Many Requests', headers, None)
        return Reply(url, b'x' * 100_000)


def fresh_process():
    """Forget everything kept in memory, as a second app process would."""
    with http._GUARD:
        http._ROBOTS.clear()
        http._DOMAINS.clear()


class QuotaTests(unittest.TestCase):
    def setUp(self):
        fresh_process()
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(self.temp.name)
        self.web = FakeWeb()

    def tearDown(self):
        fresh_process()
        self.db.close()
        self.temp.cleanup()

    def client(self, **kwargs):
        return HttpClient(self.db, interval=0, retries=0, opener=self.web, **kwargs)

    def test_thousand_series_on_one_host_share_one_robots_fetch(self):
        for number in range(1000):
            if number % 250 == 0:
                fresh_process()  # new process/client, same database
            self.client().get(f'https://comic.example/series/{number}', conditional=False, max_bytes=200_000)
        self.assertEqual(sum(url.endswith('/robots.txt') for url in self.web.calls), 1)
        with self.assertRaises(SourceUnavailable):
            self.client().get('https://comic.example/private/page', conditional=False)
        # After the robots lifetime, the policy is fetched again.
        self.db.execute('UPDATE webcomic_robots SET fetched=fetched-?', (http.ROBOTS_TTL + 1,))
        self.db.commit()
        fresh_process()
        self.client().get('https://comic.example/series/x', conditional=False, max_bytes=200_000)
        self.assertEqual(sum(url.endswith('/robots.txt') for url in self.web.calls), 2)

    def test_cache_stays_within_quota_lru(self):
        self.db.execute("INSERT INTO gui_settings(key,value) VALUES('webcomics_http_cache_mb','0.5')")
        self.db.commit()
        client = self.client()
        for number in range(8):
            client.get(f'https://comic.example/page/{number}', max_bytes=200_000)
            time.sleep(0.01)
        size = self.db.execute('SELECT SUM(LENGTH(body)) FROM webcomic_http_cache').fetchone()[0]
        self.assertLessEqual(size, 512 * 1024)
        kept = [row[0] for row in self.db.execute('SELECT url FROM webcomic_http_cache ORDER BY url')]
        self.assertEqual(kept, [f'https://comic.example/page/{n}' for n in range(3, 8)])  # oldest evicted
        self.assertEqual(client.enforce_cache_quota(max_bytes=150_000), 4)

    def test_retry_after_postpones_every_worker_and_process(self):
        self.web.retry_after = 600
        with self.assertRaises(SourceUnavailable) as raised:
            self.client().get('https://busy.example/a', conditional=False)
        self.assertEqual(raised.exception.status, 429)
        calls = len(self.web.calls)
        fresh_process()  # another process: the in-memory block is gone, the stored one is not
        self.web.retry_after = None
        with self.assertRaises(SourceUnavailable) as again:
            self.client().get('https://busy.example/b', conditional=False)
        self.assertEqual(again.exception.status, 429)
        self.assertEqual(len(self.web.calls), calls)  # nothing was sent to the busy host
        self.client().get('https://other.example/c', conditional=False)  # other hosts are unaffected

    def test_per_host_interval(self):
        self.db.execute("""INSERT INTO gui_settings(key,value) VALUES('webcomics_host_intervals','{"slow.example": 0.3}')""")
        self.db.commit()
        client = self.client()
        start = time.monotonic()
        for number in range(3):
            client.get(f'https://slow.example/{number}', conditional=False)
        self.assertGreaterEqual(time.monotonic() - start, 0.55)


if __name__ == '__main__':
    unittest.main()
