"""Search clean-up found by the live 50-series check: labels, editions, add-ons, relevance, translations."""
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from media_intake import comic_metadata as cm
from media_intake.discovery import _merge_same_title, _sort_by_title_relevance
from media_intake.media_providers import search_steam_games, steam_base_title
from media_intake.watched import _BOOK_SERIES_DESCRIPTION, _wikidata_series_members


def binding(qid, title, author=None, number=None, description='', english=True):
    row = {'work': {'value': f'http://www.wikidata.org/entity/{qid}'}, 'workLabel': {'value': title},
           'workTypeLabel': {'value': 'novel'}, 'workDescription': {'value': description}}
    if english:
        row['englishTitle'] = {'value': title}
    if author:
        row['authorLabel'] = {'value': author}
    if number:
        row['seriesNumber'] = {'value': number}
    return row


class WikidataMemberTests(unittest.TestCase):
    def members(self, rows):
        payload = json.dumps({'results': {'bindings': rows}}).encode()
        with patch('media_intake.watched.http_get', return_value=(payload, 'https://query.wikidata.org/sparql')):
            return _wikidata_series_members('Q1')

    def test_bare_ids_translations_and_editions_are_not_books(self):
        members = self.members([
            binding('Q10', 'Book One', 'Q34660', '1'),                     # author has no readable label
            binding('Q11', 'Q5813009', 'Ann Author', '2', english=False),  # title has no readable label
            binding('Q12', 'Kampen om jerntronen', 'Ann Author', '2',
                    'Norwegian translation of the second half of Book One'),
            binding('Q13', 'Book Two', 'Ann Author', '2'),
        ])
        self.assertEqual([(m['title'], m['author']) for m in members],
                         [('Book One', 'Unknown'), ('Book Two', 'Ann Author')])

    def test_numbered_series_names_are_recognised(self):
        self.assertTrue(_BOOK_SERIES_DESCRIPTION.search('pentalogy of books by Suzanne Collins (2008-2025)'))
        self.assertTrue(_BOOK_SERIES_DESCRIPTION.search('quartet of novels'))


class SteamTests(unittest.TestCase):
    def test_edition_suffixes_fold_and_add_ons_are_dropped(self):
        self.assertEqual(steam_base_title('Mass Effect™ 2 (2010) Edition'), ('Mass Effect 2', '(2010) Edition'))
        self.assertEqual(steam_base_title("The Witcher: Enhanced Edition Director's Cut")[0], 'The Witcher')
        self.assertEqual(steam_base_title('Mass Effect (2007)'), ('Mass Effect', None))
        self.assertEqual(steam_base_title('Persona 5 Royal'), ('Persona 5 Royal', None))
        items = [{'id': 1, 'name': 'Mass Effect™ Legendary Edition'},
                 {'id': 2, 'name': 'Mass Effect™: Andromeda Krogan Vanguard Multiplayer Recruit Pack'},
                 {'id': 3, 'name': 'The Witcher 2: Assassins of Kings Enhanced Edition Soundtrack'}]
        with patch('media_intake.media_providers._json_response', return_value={'items': items}):
            rows = search_steam_games('mass effect')
        self.assertEqual([(r['title'], r['catalog_metadata']['edition']) for r in rows],
                         [('Mass Effect', 'Legendary Edition')])

    def test_store_editions_merge_with_the_catalog_game(self):
        rows = [{'title': 'Mass Effect', 'game_subtype': 'base_game', 'provider': 'Wikidata', 'source_url': 'w'},
                {'title': 'Mass Effect Legendary Edition', 'game_subtype': 'base_game', 'provider': 'Wikidata',
                 'source_url': 'w2'},
                {'title': 'Mass Effect 2', 'game_subtype': 'base_game', 'provider': 'Steam', 'source_url': 's'}]
        merged = _merge_same_title(rows, 'Games', [])
        self.assertEqual([r['title'] for r in merged], ['Mass Effect', 'Mass Effect 2'])


class VideoTests(unittest.TestCase):
    def test_web_and_tv_series_of_one_show_merge(self):
        rows = [{'title': 'Stranger Things', 'video_subtype': 'web_series', 'release_date': '2016-07-15',
                 'provider': 'TVMaze', 'source_url': 't'},
                {'title': 'Stranger Things', 'video_subtype': 'tv_series', 'provider': 'Wikidata', 'source_url': 'w'}]
        merged = _merge_same_title(rows, 'Videos', [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['metadata_sources'], ['TVMaze', 'Wikidata'])

    def test_fuzzy_catalog_matches_follow_real_hits(self):
        rows = [{'title': 'Cane'}, {'title': 'Crane'}, {'title': 'Arcane: League of Legends'}, {'title': 'Arcane'}]
        self.assertEqual([r['title'] for r in _sort_by_title_relevance(rows, 'Arcane')],
                         ['Arcane', 'Arcane: League of Legends', 'Cane', 'Crane'])


class GcdTests(unittest.TestCase):
    def test_article_dropped_longest_english_run_first_and_translations_hidden(self):
        def series(sid, name, year, language, issues):
            return {'api_url': f'https://www.comics.org/api/series/{sid}/', 'name': name, 'year_began': year,
                    'language': language, 'active_issues': list(range(issues)), 'publisher': None}
        requested = []

        def pages(provider, url, **kwargs):
            requested.append(url)
            if '/name/' in url:
                return [series(1, 'The Sandman', 1974, 'en', 7), series(2, 'Sandman', 1989, 'pt', 75),
                        series(3, 'Sandman', 1989, 'en', 86), series(4, 'Sandman Mystery Theatre', 1993, 'en', 70)]
            return []
        with patch.object(cm, '_pages', side_effect=pages):
            rows, _errors = cm.search_gcd("The Sandman")
        self.assertEqual(urllib.parse.unquote(requested[0]), 'https://www.comics.org/api/series/name/Sandman/')
        series_rows = [r for r in rows if r.get('comic_entity') == 'series']
        self.assertEqual([(r['title'], r['series_start_year']) for r in series_rows][:2],
                         [('Sandman', '1989'), ('The Sandman', '1974')])
        self.assertNotIn('pt', {r.get('language') for r in series_rows})

    def test_collected_edition_runs_list_volumes_not_duplicate_issues(self):
        def pages(provider, url, **kwargs):
            if '/name/' in url:
                return [{'api_url': 'https://www.comics.org/api/series/1/', 'name': 'Invincible', 'year_began': 2003,
                         'language': 'en', 'active_issues': [1] * 170, 'publishing_format': 'was ongoing series'},
                        {'api_url': 'https://www.comics.org/api/series/2/', 'name': 'Invincible', 'year_began': 2003,
                         'language': 'en', 'active_issues': [1] * 34,
                         'publishing_format': 'Collected Editions; Was Ongoing Series'}]
            series_id = url.rstrip('/').split('/')[-2]
            return [{'issue_id': f'{series_id}0{n}', 'number': str(n),
                     'series': f'https://www.comics.org/api/series/{series_id}/'} for n in (1, 2)]
        with patch.object(cm, '_pages', side_effect=pages):
            rows, _errors = cm.search_gcd('Invincible')
        from media_intake.discovery import _merge_same_title
        merged = _merge_same_title([dict(r, source_role='metadata') for r in rows], 'Comics', [])
        titles = sorted(r['title'] for r in merged if r.get('comic_entity') != 'series')
        self.assertEqual(titles, ['Invincible', 'Invincible (2003) #1', 'Invincible (2003) #2',
                                  'Invincible Vol. 1', 'Invincible Vol. 2'])  # 'Invincible': the trade run itself


class RetryTests(unittest.TestCase):
    def test_rate_limited_catalog_request_is_retried(self):
        from media_intake import core

        class Ok:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            headers = {}
            def read(self, n): return b'{}'
            def geturl(self): return 'https://example.org/'
        busy = urllib.error.HTTPError('https://example.org/', 429, 'Too Many Requests', {'Retry-After': '0'}, None)
        with patch.object(core, 'open_url', side_effect=[busy, Ok()]), patch.object(core.time, 'sleep') as sleep:
            self.assertEqual(core.http_get('https://example.org/'), (b'{}', 'https://example.org/'))
        sleep.assert_called_once()


if __name__ == '__main__':
    unittest.main()
