"""Polite live discovery smoke: one bounded public query per configured connector.

Does not approve sources, fetch release files, join IRC, create accounts, or alter
the user's database. Load/stress belongs to deterministic local fixture tests.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_intake import download_connectors as connectors
from media_intake.comic_sources import search_getcomics, _ReleasePage, http_get
from media_intake.media_catalogs import search_lnori, search_novelarchive
from media_intake.sources import search_site
import urllib.parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('build/connector-live-smoke.json'))
    parser.add_argument('--source', action='append', help='Check only these source keys')
    args = parser.parse_args()
    tasks = [
        ('getcomics.org', lambda: search_getcomics('public domain', limit=3)),
        ('internet_archive', lambda: connectors.search_internet_archive('Alice in Wonderland', limit=1)),
        ('github_foss', lambda: connectors.search_github_foss('SumatraPDF', limit=1, repo_limit=1)),
        ('sourceforge.net', lambda: connectors.search_sourceforge_foss('7zip', limit=1)),
        ('fosshub.com', lambda: connectors.search_fosshub_foss('SumatraPDF', limit=1)),
        ('softarchive.download', lambda: connectors.search_softarchive('public domain', limit=1)),
        ('meawfy.com', lambda: connectors.search_meawfy('public domain', limit=1)),
        ('libgen', lambda: search_site('libgen', 'Alice in Wonderland')),
        ('annas_archive', lambda: search_site('annas', 'Alice in Wonderland')),
        ('lnori.com', lambda: search_lnori('public domain', limit=1)),
        ('novelarchive.cc', lambda: search_novelarchive('public domain', limit=1)),
    ]
    for key, _provider, searcher, _resolver in connectors.ANIME_DOWNLOAD_ADAPTERS:
        tasks.append((key, lambda searcher=searcher: searcher('public domain', limit=1)))
    for key, _provider, categories in connectors.GENERAL_DOWNLOAD_ADAPTERS:
        category = 'Books' if 'Books' in categories else 'Games'
        tasks.append((key, lambda key=key, category=category:
            connectors.search_registered_download_site('public domain', key, limit=1, category=category)))
    if args.source:
        known = {key for key, _search in tasks}
        if set(args.source) - known:
            parser.error('Unknown source: ' + ', '.join(sorted(set(args.source) - known)))
        tasks = [(key, search) for key, search in tasks if key in args.source]
    def run(key, search):
        started = time.monotonic()
        try:
            rows = search()
            browser = rows and all(str(row.get('title') or '').startswith('Open ') for row in rows)
            status = 'browser_only' if browser else 'metadata_results' if rows and all(row.get('discovery_only') for row in rows) else 'results' if rows else 'no_results'
            result = {'source': key, 'status': status, 'count': len(rows),
                      'sample': [{field: row.get(field) for field in ('title', 'format', 'provider', 'source', 'discovery_only', 'access_note')}
                                 for row in rows[:3]]}
            if key == 'getcomics.org':
                inventories = []
                for row in rows[:2]:
                    raw, final = http_get(row['url'], max_bytes=5_000_000)
                    page = _ReleasePage(final); page.feed(raw.decode('utf-8', 'replace'))
                    hosts = {}
                    for link in page.links:
                        host = urllib.parse.urlsplit(link['href']).hostname or ''
                        if any(name in host for name in ('pixeldrain', 'mediafire', 'mega.', 'wetransfer', 'we.tl', 'gofile', 'terabox')):
                            hosts[host] = hosts.get(host, 0) + 1
                    inventories.append({'source': row['url'], 'file_host_link_counts': hosts})
                result['post_link_inventory'] = inventories
        except Exception as exc:
            result = {'source': key, 'status': 'failed', 'error': str(exc),
                      'http_status': getattr(exc, 'status', getattr(exc, 'code', None))}
        result['seconds'] = round(time.monotonic() - started, 2)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return result
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run, key, search) for key, search in tasks]
        for future in as_completed(futures):
            results.append(future.result())
    report = {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'mode': 'discovery only; zero release-file downloads',
              'results': sorted(results, key=lambda row: row['source'])}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'Report: {args.output.resolve()}')


if __name__ == '__main__':
    main()
