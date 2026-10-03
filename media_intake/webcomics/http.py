"""Bounded polite HTTP, conditional cache and robots policy for every adapter."""
from dataclasses import dataclass
import datetime as dt
import email.utils
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from .models import canonicalize_url

_GUARD = threading.Lock()
_DOMAINS = {}
_ROBOTS = {}
ROBOTS_TTL = 3600
DEFAULT_CACHE_MB = 200
_DISALLOW_ALL = 'User-agent: *\nDisallow: /'


def ensure_http_tables(db):
    """Durable robots policies, host back-offs and cache recency, shared by every process."""
    db.execute('CREATE TABLE IF NOT EXISTS webcomic_robots(origin TEXT PRIMARY KEY, body TEXT NOT NULL, fetched REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS webcomic_host_blocks(host TEXT PRIMARY KEY, until REAL NOT NULL)')
    columns = {row[1] for row in db.execute('PRAGMA table_info(webcomic_http_cache)')}
    if columns and 'last_used' not in columns:
        db.execute('ALTER TABLE webcomic_http_cache ADD COLUMN last_used REAL')


def _setting(db, key, default):
    try:
        row = db.execute('SELECT value FROM gui_settings WHERE key=?', (key,)).fetchone()
    except Exception:
        return default
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SourceUnavailable(OSError):
    def __init__(self, status, url, retry_after=None):
        super().__init__(f'Source HTTP {status}: {url}')
        self.status, self.url, self.retry_after = status, url, retry_after


class ExtractionError(ValueError):
    pass


@dataclass
class Response:
    body: bytes
    url: str
    headers: dict
    status: int = 200
    unchanged: bool = False


def retry_seconds(value):
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            date = email.utils.parsedate_to_datetime(value)
            return max(0, (date - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class HttpClient:
    def __init__(self, db=None, interval=1.0, timeout=30, retries=2,
                 user_agent='MediaArchiveImporter/0.3 (+public webcomic archive)',
                 respect_robots=True, opener=None):
        self.db, self.interval, self.timeout, self.retries = db, max(0, interval), timeout, retries
        self.user_agent, self.respect_robots = user_agent, respect_robots
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.robots = {}
        self.host_intervals = {}
        if db is not None:
            try:
                ensure_http_tables(db)
                db.commit()
                self.host_intervals = {str(k).casefold(): float(v) for k, v in
                                       (_setting(db, 'webcomics_host_intervals', {}) or {}).items()}
            except Exception:
                self.host_intervals = {}

    def _db_call(self, sql, params=(), fetch=False):
        """Durable state is best-effort: a client used off its connection's thread just skips it."""
        if self.db is None:
            return None
        try:
            cursor = self.db.execute(sql, params)
            if fetch:
                return cursor.fetchone()
            self.db.commit()
        except Exception:
            return None
        return None

    def _block_host(self, url, seconds):
        until = time.time() + seconds
        host = urllib.parse.urlsplit(url).netloc.lower()
        self._domain(url)['blocked_until'] = time.monotonic() + seconds
        self._db_call("INSERT INTO webcomic_host_blocks VALUES(?,?) ON CONFLICT(host) "
                      "DO UPDATE SET until=MAX(until, excluded.until)", (host, until))

    def _domain(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        with _GUARD:
            return _DOMAINS.setdefault(host, {'lock': threading.Lock(), 'last': 0.0,
                                               'blocked_until': 0.0})

    def _request(self, url, headers, max_bytes, method='GET'):
        domain = self._domain(url)
        host = urllib.parse.urlsplit(url).netloc.lower()
        interval = max(self.interval, self.host_intervals.get(host, 0),
                       self.host_intervals.get(host.removeprefix('www.'), 0))
        # A back-off another process recorded applies here too.
        row = self._db_call('SELECT until FROM webcomic_host_blocks WHERE host=?', (host,), fetch=True)
        if row and row[0] > time.time():
            domain['blocked_until'] = max(domain['blocked_until'], time.monotonic() + (row[0] - time.time()))
        # One in-flight request per host across clients and GUI workers.
        with domain['lock']:
            wait = max(domain['last'] + interval, domain['blocked_until']) - time.monotonic()
            if wait > 60:
                raise SourceUnavailable(429, url, wait)
            if wait > 0:
                time.sleep(wait)
            try:
                request = urllib.request.Request(url, headers={'User-Agent': self.user_agent, **headers}, method=method)
                with self.opener(request, timeout=self.timeout) as reply:
                    body = reply.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise ExtractionError('Source exceeds response byte budget')
                    return Response(body, canonicalize_url(reply.geturl()),
                                    {k.lower(): v for k, v in reply.headers.items()},
                                    getattr(reply, 'status', 200))
            finally:
                domain['last'] = time.monotonic()

    def head(self, url, stop_at=None):
        """Polite bounded redirect probe without reading a media response body."""
        target, visited = canonicalize_url(url), set()
        for _ in range(8):
            if target in visited:
                raise ExtractionError('Source redirect loop')
            visited.add(target)
            if stop_at and stop_at(target):
                return Response(b'', target, {}, status=302)
            self._allowed(target)
            try:
                return self._request(target, {}, 0, method='HEAD')
            except urllib.error.HTTPError as exc:
                location = exc.headers.get('Location')
                code = exc.code
                delay = retry_seconds(exc.headers.get('Retry-After'))
                exc.close()
                if code in (301, 302, 303, 307, 308) and location:
                    target = canonicalize_url(urllib.parse.urljoin(target, location))
                    continue
                if code == 429:
                    self._domain(target)['blocked_until'] = time.monotonic() + (delay or 60)
                raise SourceUnavailable(code, target, delay)
        raise ExtractionError('Source exceeded redirect budget')

    def _allowed(self, url):
        if not self.respect_robots:
            return
        parts = urllib.parse.urlsplit(url)
        origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, '', '', ''))
        if origin not in self.robots:
            with _GUARD:
                cached = _ROBOTS.get(origin)
            if cached and cached[0] > time.monotonic():
                self.robots[origin] = cached[1]
        if origin not in self.robots:
            # Shared across processes and restarts: one robots.txt fetch per origin per hour.
            row = self._db_call('SELECT body, fetched FROM webcomic_robots WHERE origin=?', (origin,), fetch=True)
            if row and time.time() - row[1] < ROBOTS_TTL:
                parser = urllib.robotparser.RobotFileParser(origin + '/robots.txt')
                parser.parse(row[0].splitlines())
                self.robots[origin] = parser
                with _GUARD:
                    _ROBOTS[origin] = (time.monotonic() + ROBOTS_TTL - (time.time() - row[1]), parser)
        if origin not in self.robots:
            robot_url = origin + '/robots.txt'
            parser = urllib.robotparser.RobotFileParser(robot_url)
            try:
                response = self._request(robot_url, {}, 512_000)
                text = response.body.decode('utf-8', 'replace')
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    text = ''
                elif exc.code in (401, 403):
                    text = _DISALLOW_ALL
                else:
                    raise SourceUnavailable(exc.code, robot_url, retry_seconds(exc.headers.get('Retry-After')))
            parser.parse(text.splitlines())
            self.robots[origin] = parser
            with _GUARD:
                _ROBOTS[origin] = (time.monotonic() + ROBOTS_TTL, parser)
            self._db_call('INSERT INTO webcomic_robots VALUES(?,?,?) ON CONFLICT(origin) DO UPDATE SET '
                          'body=excluded.body, fetched=excluded.fetched', (origin, text, time.time()))
        parser = self.robots[origin]
        if not parser.can_fetch(self.user_agent, url):
            raise SourceUnavailable(403, url)
        delay = parser.crawl_delay(self.user_agent)
        rate = parser.request_rate(self.user_agent)
        if delay:
            self.interval = max(self.interval, delay)
        if rate and rate.requests:
            self.interval = max(self.interval, rate.seconds / rate.requests)

    def get(self, url, max_bytes=3_000_000, conditional=True, headers=None):
        url = canonicalize_url(url)
        self._allowed(url)
        cached = self.db.execute('SELECT * FROM webcomic_http_cache WHERE url=?', (url,)).fetchone() if self.db and conditional else None
        outgoing = dict(headers or {})
        if cached:
            old = json.loads(cached['headers'])
            if old.get('etag'):
                outgoing['If-None-Match'] = old['etag']
            if old.get('last-modified'):
                outgoing['If-Modified-Since'] = old['last-modified']
        for attempt in range(self.retries + 1):
            try:
                target, visited = url, set()
                for _ in range(8):
                    if target in visited:
                        raise ExtractionError('Source redirect loop')
                    visited.add(target)
                    self._allowed(target)
                    try:
                        response = self._request(target, outgoing, max_bytes)
                        break
                    except urllib.error.HTTPError as redirect:
                        if redirect.code not in (301, 302, 303, 307, 308):
                            raise
                        location = redirect.headers.get('Location')
                        redirect.close()
                        if not location:
                            raise ExtractionError('Redirect has no destination')
                        target = canonicalize_url(urllib.parse.urljoin(target, location))
                else:
                    raise ExtractionError('Source exceeded redirect budget')
                if self.db and conditional:
                    self.db.execute('''INSERT INTO webcomic_http_cache(url,final_url,body,headers,fetched_at,last_used)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET final_url=excluded.final_url,body=excluded.body,
                        headers=excluded.headers,fetched_at=excluded.fetched_at,last_used=excluded.last_used''',
                        (url, response.url, response.body, json.dumps(response.headers),
                         dt.datetime.now(dt.timezone.utc).isoformat(), time.time()))
                    self.db.commit()
                    self.enforce_cache_quota()
                return response
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code == 304 and cached:
                    self._db_call('UPDATE webcomic_http_cache SET last_used=? WHERE url=?', (time.time(), url))
                    return Response(bytes(cached['body']), cached['final_url'],
                                    json.loads(cached['headers']), 304, True)
                delay = retry_seconds(exc.headers.get('Retry-After'))
                if exc.code == 429 or (exc.code == 503 and delay is not None):
                    # Every worker and process waits for this host.
                    self._block_host(url, delay if delay is not None else 60)
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise SourceUnavailable(exc.code, url, delay) from exc
                delay = delay if delay is not None else 2 ** attempt
                if delay > 60:
                    raise SourceUnavailable(exc.code, url, delay) from exc
                time.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == self.retries:
                    raise SourceUnavailable(0, url) from exc
                time.sleep(2 ** attempt)

    def enforce_cache_quota(self, max_bytes=None):
        """Keep the response cache within its size limit, evicting least recently used entries."""
        if self.db is None:
            return 0
        if max_bytes is None:
            max_bytes = int(float(_setting(self.db, 'webcomics_http_cache_mb', DEFAULT_CACHE_MB)) * 1024 * 1024)
        try:
            total = self.db.execute('SELECT COALESCE(SUM(LENGTH(body)),0) FROM webcomic_http_cache').fetchone()[0]
            removed = 0
            if total > max_bytes:
                for url, size in self.db.execute("SELECT url, LENGTH(body) FROM webcomic_http_cache "
                                                 "ORDER BY COALESCE(last_used, 0), fetched_at").fetchall():
                    if total <= max_bytes:
                        break
                    self.db.execute('DELETE FROM webcomic_http_cache WHERE url=?', (url,))
                    total -= size
                    removed += 1
                self.db.commit()
            return removed
        except Exception:
            return 0

    def prune(self, max_age_days=30):
        if self.db:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)).isoformat()
            self.db.execute('DELETE FROM webcomic_http_cache WHERE fetched_at<?', (cutoff,))
            self.db.commit()
