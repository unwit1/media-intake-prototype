"""Live metadata search check: run the app's category search for well-known series and flag problems.

Usage:  python tools/metadata_check.py [output.json] [--only CATEGORY] [--more]

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
import unicodedata

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
COMICS_PAUSE = 30
# A second, wider set: older and newer works, non-English originals, punctuation,
# numbers, subtitles, one-word and very common titles.
MORE_QUERIES = [
    ('Books', 'The Lord of the Rings', 'lord of the rings'), ('Books', 'The Chronicles of Narnia', 'narnia'),
    ('Books', 'His Dark Materials', 'his dark materials'), ('Books', "The Hitchhiker's Guide to the Galaxy", 'hitchhiker'),
    ('Books', 'Foundation', 'foundation'), ('Books', 'The Witcher', 'witcher'), ('Books', 'Earthsea', 'earthsea'),
    ('Books', 'The Kingkiller Chronicle', 'kingkiller'), ('Books', 'The Dark Tower', 'dark tower'),
    ('Books', 'Outlander', 'outlander'), ('Books', 'Jack Reacher', 'reacher'), ('Books', 'Twilight', 'twilight'),
    ('Books', 'The Maze Runner', 'maze runner'), ('Books', 'Divergent', 'divergent'), ('Books', 'Shadow and Bone', 'shadow and bone'),
    ('Books', 'The First Law', 'first law'), ('Books', 'The Murderbot Diaries', 'murderbot'),
    ('Books', 'The Three-Body Problem', 'three body'), ('Books', 'Hyperion Cantos', 'hyperion'),
    ('Books', 'The Belgariad', 'belgariad'), ('Books', 'Sherlock Holmes', 'sherlock holmes'),
    ('Books', 'Hercule Poirot', 'poirot'), ('Books', 'Cormoran Strike', 'strike'),
    ('Books', 'The Inheritance Cycle', 'inheritance'), ('Books', 'Wings of Fire', 'wings of fire'),
    ('Videos', 'The Sopranos', 'sopranos'), ('Videos', 'The Wire', 'wire'), ('Videos', 'Friends', 'friends'),
    ('Videos', 'Seinfeld', 'seinfeld'), ('Videos', 'The Simpsons', 'simpsons'), ('Videos', 'Better Call Saul', 'better call saul'),
    ('Videos', 'The Mandalorian', 'mandalorian'), ('Videos', 'Ted Lasso', 'ted lasso'), ('Videos', 'Fargo', 'fargo'),
    ('Videos', 'Twin Peaks', 'twin peaks'), ('Videos', 'Star Trek: The Next Generation', 'next generation'),
    ('Videos', 'Black Mirror', 'black mirror'), ('Videos', 'The Last of Us', 'last of us'),
    ('Videos', 'Harry Potter', 'harry potter'), ('Videos', 'Jurassic Park', 'jurassic park'),
    ('Videos', 'Indiana Jones', 'indiana jones'), ('Videos', 'Back to the Future', 'back to the future'),
    ('Videos', 'The Godfather', 'godfather'), ('Videos', 'Pirates of the Caribbean', 'pirates of the caribbean'),
    ('Videos', 'Shrek', 'shrek'), ('Videos', 'Mad Max', 'mad max'), ('Videos', 'Terminator', 'terminator'),
    ('Videos', 'Rocky', 'rocky'), ('Videos', 'Spider-Man', 'spider man'),
    ('Comics', 'Bone', 'bone'), ('Comics', 'Y: The Last Man', 'last man'), ('Comics', 'Preacher', 'preacher'),
    ('Comics', 'Paper Girls', 'paper girls'), ('Comics', 'Monstress', 'monstress'), ('Comics', 'Fables', 'fables'),
    ('Comics', 'Transmetropolitan', 'transmetropolitan'), ('Comics', 'Usagi Yojimbo', 'usagi yojimbo'),
    ('Comics', 'Sin City', 'sin city'), ('Comics', 'Locke & Key', 'locke and key'), ('Comics', 'Maus', 'maus'),
    ('Comics', 'East of West', 'east of west'), ('Comics', 'Ms. Marvel', 'ms marvel'),
    ('Manga', 'Naruto', 'naruto'), ('Manga', 'Vagabond', 'vagabond'), ('Manga', 'Jujutsu Kaisen', 'jujutsu kaisen'),
    ('Manga', 'Dragon Ball', 'dragon ball'), ('Manga', 'Death Note', 'death note'), ('Manga', 'Vinland Saga', 'vinland saga'),
    ('Manga', 'Monster', 'monster'), ('Manga', 'Akira', 'akira'), ('Manga', 'Yotsuba&!', 'yotsuba'),
    ('Manga', 'Oyasumi Punpun', 'punpun'), ('Manga', 'Blue Lock', 'blue lock'), ('Manga', 'Dandadan', 'dandadan'),
    ('Anime', 'Neon Genesis Evangelion', 'evangelion'), ('Anime', 'Mob Psycho 100', 'mob psycho'),
    ('Anime', 'Demon Slayer', 'demon slayer'), ('Anime', 'Steins;Gate', 'steins gate'), ('Anime', 'Haikyu!!', 'haikyu'),
    ('Anime', 'My Hero Academia', 'my hero academia'), ('Anime', 'Ghost in the Shell', 'ghost in the shell'),
    ('Anime', 'Gurren Lagann', 'gurren lagann'), ('Anime', 'Bocchi the Rock!', 'bocchi'), ('Anime', 'Hunter x Hunter', 'hunter x hunter'),
    ('Games', 'Super Mario', 'mario'), ('Games', 'Halo', 'halo'), ('Games', 'Metroid', 'metroid'),
    ('Games', 'Dark Souls', 'dark souls'), ('Games', 'The Elder Scrolls', 'elder scrolls'), ('Games', 'Fallout', 'fallout'),
    ('Games', 'Resident Evil', 'resident evil'), ('Games', 'Kingdom Hearts', 'kingdom hearts'),
    ('Games', 'Pok\u00e9mon', 'pokemon'), ('Games', 'Half-Life', 'half life'),
    ('Light novels', 'Sword Art Online', 'sword art online'), ('Light novels', 'Mushoku Tensei', 'mushoku tensei'),
    ('Light novels', 'The Apothecary Diaries', 'apothecary diaries'), ('Light novels', 'Ascendance of a Bookworm', 'bookworm'),
    ('Light novels', 'Konosuba', 'konosuba'), ('Light novels', 'That Time I Got Reincarnated as a Slime', 'slime'),
]
EDITION = re.compile(r'\b(paperback|hardcover|hardback|kindle edition|mass market|box(ed)? set|boxset|collector\'?s|'
                     r'omnibus|deluxe edition|anniversary edition|special edition|illustrated edition|audiobook|'
                     r'unabridged|large print|\d+(st|nd|rd|th) edition|edition)\b', re.I)


def norm(value):
    value = ''.join(c for c in unicodedata.normalize('NFKD', str(value or '')) if not unicodedata.combining(c))
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
    bare = lambda text: re.sub(r'^(?:the|a|an) ', '', norm(text))
    exact_rank = next((i + 1 for i, r in enumerate(results[:20]) if bare(r.get('title')) == bare(query)), None)
    keys = {}
    for row in results[:40]:
        if row.get('kind') == 'book' and row.get('series'):
            continue  # series member rows are expected under a series bundle
        subtype = row.get('video_subtype') or row.get('game_subtype') or row.get('comic_entity') or row.get('entity_type')
        year = (row.get('release_date') or '')[:4] or str(row.get('release_year') or row.get('series_start_year') or '')
        key = (norm(row.get('title')), norm(row.get('author') if str(row.get('author')).casefold() != 'unknown' else ''),
               row.get('kind'), subtype, year)
        keys.setdefault(key, []).append(row.get('display_source') or row.get('source'))
    duplicates = [{'title': key[0], 'author': key[1], 'kind': key[2], 'type': key[3], 'year': key[4], 'sources': sources}
                  for key, sources in keys.items() if len(sources) > 1]
    editions = [r.get('title') for r in results[:40] if EDITION.search(str(r.get('title') or ''))]
    return {'category': category, 'query': query, 'seconds': round(time.time() - started, 1), 'count': len(results),
            'expected_rank': expected_rank, 'exact_rank': exact_rank, 'duplicates': duplicates, 'edition_rows': editions,
            'series_name': response.get('series_name'), 'series_members': response.get('series_member_count'),
            'errors': (response.get('errors') or [])[:6], 'top': top}


def main():
    output = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else Path('build/metadata-check.json')
    only = sys.argv[sys.argv.index('--only') + 1] if '--only' in sys.argv else None
    with tempfile.TemporaryDirectory() as root:
        options = {'rights_evidence_path': str(ensure_rights_evidence(Path(root) / 'rights-evidence.json'))}
        report = []
        for category, query, expect in (MORE_QUERIES if '--more' in sys.argv else QUERIES):
            if only and category != only:
                continue
            if category == 'Comics' and report and report[-1]['category'] == 'Comics':
                time.sleep(COMICS_PAUSE)  # the Grand Comics Database limits bursts of searches
            result = check(category, query, expect, options)
            report.append(result)
            flag = ('ERROR ' + result['error']) if 'error' in result else (
                f"rank={result['expected_rank']} exact={result['exact_rank']} n={result['count']} dup={len(result['duplicates'])} "
                f"editions={len(result['edition_rows'])} errs={len(result['errors'])} {result['seconds']}s")
            print(f'{category:12} {query:32} {flag}', flush=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
