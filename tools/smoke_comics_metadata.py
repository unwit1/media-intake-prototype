"""Bounded, metadata-only live checks; no watches, downloads or source approvals.

Optional Comic Vine/Metron credentials come from the launch environment. Run
fixtures for load testing; this tool makes only the normal bounded catalog query.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from media_intake.comic_metadata import metadata_tasks, sort_comics
from media_intake.discovery import _catalog_rows, _merge_same_title, _query_wikidata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--query', default='Monstress (2015) #1')
    parser.add_argument('--provider', action='append', choices=('Comic Vine', 'Grand Comics Database', 'Metron', 'Wikidata'))
    parser.add_argument('--output', type=Path, default=Path('build/comics-metadata-live-smoke.json'))
    args = parser.parse_args()
    tasks = metadata_tasks(args.query) + [('Wikidata', lambda: _query_wikidata(args.query, 'Comics'))]
    selected = set(args.provider or [name for name, _ in tasks])
    tasks = [(name, action) for name, action in tasks if name in selected]
    report = {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'query': args.query,
              'providers': [], 'skipped_unconfigured': sorted(selected - {name for name, _ in tasks})}
    rows = []
    def check(name, action):
        started = time.monotonic()
        try:
            value = action()
            found, errors = value if isinstance(value, tuple) else (value, [])
        except Exception as exc:
            found, errors = [], [str(exc)]
        return {'provider': name, 'seconds': round(time.monotonic() - started, 3),
                'count': len(found), 'errors': errors}, found
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(check, name, action) for name, action in tasks]
        for future in futures:
            status, found = future.result()
            report['providers'].append(status)
            rows.extend(_catalog_rows(found, 'Comics', status['provider']))
    report['results'] = sort_comics(_merge_same_title(rows, 'Comics', ()), args.query)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(args.output), 'providers': report['providers'],
                      'skipped_unconfigured': report['skipped_unconfigured'],
                      'result_count': len(report['results'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
