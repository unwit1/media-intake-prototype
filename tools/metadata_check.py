"""Live metadata search check: run the app's category search for well-known series and flag problems.

Usage:  python tools/metadata_check.py [output.json] [--only CATEGORY]

For each query it records the top results and flags: the expected title missing
from the top 5, duplicate rows for one work (same normalized title and creator
within a category), and edition/format/box-set rows that should have been folded
into their work. It uses a fresh rights-evidence file, so only the catalogs and
connectors the app searches by default are queried; nothing is downloaded.
Catalogs that need API keys are skipped unless their keys are set.
"""
import json
from pathlib import Path
import re
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_intake.media_resolver import resolve  # noqa: E402
from media_intake.rights_evidence import ensure_rights_evidence  # noqa: E402

QUERIES = [
    ('Books', 'The Stormlight Archive', 'stormlight archive'), ('Books', 'Percy Jackson and the Olympians', 'percy jackson'),
    ('Books', 'Harry Potter', 'harry potter'), ('Books', 'The Wheel of Time', 'wheel of time'),
    ('Books', 'Discworld', 'discworld'), ('Books', 'Mistborn', 'mistborn'), ('Books', 'The Expanse', 'expanse'),
    ('Books', 'Dune', 'dune'), ('Books', 'The Hunger Games', 'hunger games'),
    ('Books', 'A Song of Ice and Fire', 'song of ice and fire'), ('Books', 'Red Rising', 'red rising'),
    ('Books', 'The Dresden Files', 'dresden files'),
    ('Videos', 'Breaking Bad', 'breaking bad'), ('Videos', 'The Office', 'office'), ('Videos', 'Game of Thrones', 'game of thrones'),
    ('Videos', 'Stranger Things', 'stranger things'), ('Videos', 'Doctor Who', 'doctor who'), ('Videos', 'Severance', 'severance'),
    ('Videos', 'The Bear', 'bear'), ('Videos', 'Succession', 'succession'), ('Videos', 'Arcane', 'arcane'),
    ('Videos', 'Star Wars', 'star wars'), ('Videos', 'The Lord of the Rings', 'lord of the rings'),
    ('Videos', 'Toy Story', 'toy story'), ('Videos', 'Mission: Impossible', 'mission impossible'),
    ('Videos', 'John Wick', 'john wick'), ('Videos', 'The Matrix', 'matrix'), ('Videos', 'Alien', 'alien'),
    ('Comics', 'Saga', 'saga'), ('Comics', 'The Walking Dead', 'walking dead'), ('Comics', 'The Sandman', 'sandman'),
    ('Comics', 'Watchmen', 'watchmen'), ('Comics', 'Invincible', 'invincible'), ('Comics', 'Hellboy', 'hellboy'),
    ('Manga', 'One Piece', 'one piece'), ('Manga', 'Berserk', 'berserk'), ('Manga', 'Chainsaw Man', 'chainsaw man'),
    ('Manga', 'Spy x Family', 'spy x family'), ('Manga', 'Frieren', 'frieren'),
    ('Anime', 'Attack on Titan', 'attack on titan'), ('Anime', 'Fullmetal Alchemist', 'fullmetal alchemist'),
    ('Anime', 'Cowboy Bebop', 'cowboy bebop'),
    ('Games', 'The Legend of Zelda', 'zelda'), ('Games', 'Final Fantasy', 'final fantasy'), ('Games', 'Mass Effect', 'mass effect'),
    ('Games', 'The Witcher', 'witcher'), ('Games', 'Persona', 'persona'),
    ('Light novels', 'Re:Zero', 're zero'), ('Light novels', 'Overlord', 'overlord'), ('Light novels', 'Spice and Wolf', 'spice and wolf'),
]
EDITION = re.compile(r'\b(paperback|hardcover|hardback|kindle edition|mass market|box(ed)? set|boxset|collector\'?s|'
                     r'omnibus|deluxe edition|anniversary edition|special edition|illustrated edition|audiobook|'
                     r'unabridged|large print|\d+(st|nd|rd|th) edition|edition)\b', re.I)


def norm(value):
    return re.sub(r'[\W_]+', ' ', str(value or '').casefold().replace('&', ' and ')).strip()


def check(category, query, expect, options):
    started = time.time()
    try:
        response = resolve(query, category, options, {}, None)
    except Exception as exc:
        return {'category': category, 'query': query, 'error': f'{type(exc).__name__}: {exc}'}
    results = response.get('results') or []
    top = [{'title': r.get('title'), 'author': r.get('author'), 'kind': r.get('kind'), 'year': (r.get('release_date') or '')[:4],
            'source': r.get('display_source') or r.get('source'), 'format': r.get('format')} for r in results[:10]]
    expected_rank = next((i + 1 for i, r in enumerate(results[:20]) if norm(expect) in norm(r.get('title'))), None)
    keys = {}
    for row in results[:40]:
        if row.get('kind') == 'book' and row.get('series'):
            continue  # series member rows are expected under a series bundle
        key = (norm(row.get('title')), norm(row.get('author') if str(row.get('author')).casefold() != 'unknown' else ''),
               row.get('kind'), (row.get('release_date') or '')[:4] if row.get('kind') in ('movie', 'video') else '')
        keys.setdefault(key, []).append(row.get('display_source') or row.get('source'))
    duplicates = [{'title': key[0], 'author': key[1], 'kind': key[2], 'sources': sources}
                  for key, sources in keys.items() if len(sources) > 1]
    editions = [r.get('title') for r in results[:40] if EDITION.search(str(r.get('title') or ''))]
    return {'category': category, 'query': query, 'seconds': round(time.time() - started, 1), 'count': len(results),
            'expected_rank': expected_rank, 'duplicates': duplicates, 'edition_rows': editions,
            'series_name': response.get('series_name'), 'series_members': response.get('series_member_count'),
            'errors': (response.get('errors') or [])[:6], 'top': top}


def main():
    output = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else Path('build/metadata-check.json')
    only = sys.argv[sys.argv.index('--only') + 1] if '--only' in sys.argv else None
    with tempfile.TemporaryDirectory() as root:
        options = {'rights_evidence_path': str(ensure_rights_evidence(Path(root) / 'rights-evidence.json'))}
        report = []
        for category, query, expect in QUERIES:
            if only and category != only:
                continue
            result = check(category, query, expect, options)
            report.append(result)
            flag = ('ERROR ' + result['error']) if 'error' in result else (
                f"rank={result['expected_rank']} n={result['count']} dup={len(result['duplicates'])} "
                f"editions={len(result['edition_rows'])} errs={len(result['errors'])} {result['seconds']}s")
            print(f'{category:12} {query:32} {flag}', flush=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
