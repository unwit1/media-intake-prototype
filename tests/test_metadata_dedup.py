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

    def test_wikidata_videos_skip_other_media_and_carry_their_year(self):
        from media_intake.discovery import _query_wikidata
        from media_intake.gui import MediaIntakeApp
        found = [{'qid': 'Q1', 'title': 'John Wick', 'description': '2014 film directed by Chad Stahelski'},
                 {'qid': 'Q2', 'title': 'John Wick', 'description': 'comic book limited series'},
                 {'qid': 'Q3', 'title': 'The Matrix: Music from the Motion Picture', 'description': 'soundtrack album'},
                 {'qid': 'Q4', 'title': 'Show, season 2', 'description': 'season of television series'},
                 {'qid': 'Q5', 'title': 'John Wick', 'description': 'American media franchise'}]
        with patch('media_intake.discovery.search_wikidata', return_value=found):
            rows = _query_wikidata('John Wick', 'Videos')
        self.assertEqual([(r['qid'], MediaIntakeApp._result_creator_label(r)) for r in rows],
                         [('Q1', 'Film · 2014'), ('Q5', 'Franchise')])
        self.assertEqual(MediaIntakeApp._result_creator_label({'author': 'Ann Author'}), 'Ann Author')

    def test_rows_sharing_an_imdb_id_merge_whatever_their_titles(self):
        rows = [{'title': 'Arcane', 'video_subtype': 'tv_series', 'provider': 'Wikidata', 'source_url': 'w',
                 'external_ids': [{'provider': 'imdb', 'external_id': 'tt11126994'}]},
                {'title': 'Arcane: League of Legends', 'video_subtype': 'web_series', 'release_date': '2021-11-06',
                 'provider': 'TVMaze', 'source_url': 't',
                 'external_ids': [{'provider': 'tvmaze', 'external_id': '41981'},
                                  {'provider': 'imdb', 'external_id': 'TT11126994'}]},
                {'title': 'Arcane', 'video_subtype': 'movie', 'provider': 'Wikidata', 'source_url': 'w2',
                 'external_ids': [{'provider': 'imdb', 'external_id': 'tt0000001'}]}]
        merged = _merge_same_title(rows, 'Videos', [])
        self.assertEqual(sorted(len(r['metadata_variants']) for r in merged), [1, 2])

    def test_wikidata_video_rows_get_cross_catalog_ids(self):
        from media_intake.discovery import _query_wikidata
        with patch('media_intake.discovery.search_wikidata', return_value=[
                {'qid': 'Q1', 'title': 'Arcane', 'description': 'animated television series'}]),                 patch('media_intake.discovery.wikidata_external_ids',
                      return_value={'Q1': [{'provider': 'imdb', 'external_id': 'tt11126994'}]}):
            row = _query_wikidata('Arcane', 'Videos')[0]
        self.assertIn({'provider': 'imdb', 'external_id': 'tt11126994'}, row['external_ids'])
        with patch('media_intake.discovery.search_wikidata', return_value=[
                {'qid': 'Q1', 'title': 'Arcane', 'description': 'animated television series'}]),                 patch('media_intake.discovery.wikidata_external_ids', side_effect=ValueError('busy')):
            self.assertEqual(len(_query_wikidata('Arcane', 'Videos')), 1)  # the lookup is best effort


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

    def test_exact_run_is_found_pages_into_a_long_name_ordered_list(self):
        # GCD lists runs by sort name (article dropped): the two "Saga" runs sit pages in.
        names = ([f'All In Saga {i:03}' for i in range(98)] + ['Saga', 'The Saga'] +
                 [f'The Saga of Rex {i:03}' for i in range(160)])
        self.assertEqual(names, sorted(names, key=cm._gcd_sort_key))
        rows = [{'api_url': f'https://www.comics.org/api/series/{i}/', 'name': name, 'year_began': 2012,
                 'language': 'en', 'active_issues': [], 'publishing_format': ''} for i, name in enumerate(names, 1)]
        requested = []

        def fake_json(provider, url, headers=None, interval=1):
            requested.append(url)
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('page', ['1'])[0])
            chunk = rows[(page - 1) * 50:page * 50]
            return {'count': len(rows), 'next': 'more' if page * 50 < len(rows) else None, 'results': chunk}
        with patch.object(cm, '_json', side_effect=fake_json):
            found = cm._gcd_exact_series('https://www.comics.org/api/series/name/Saga/', 'saga')
        self.assertEqual([row['name'] for row in found], ['Saga', 'The Saga'])
        self.assertLessEqual(len(requested), 5)


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

    def test_rate_limited_catalogs_are_paced_and_briefly_cached(self):
        from media_intake import core

        class Ok:
            def __init__(self, url): self.url = url
            def __enter__(self): return self
            def __exit__(self, *args): return False
            headers = {}
            def read(self, n): return b'{}'
            def geturl(self): return self.url
        opened = []
        def fake_open(request, timeout=30):
            opened.append(request.full_url)
            return Ok(request.full_url)
        url = 'https://www.wikidata.org/w/api.php?action=wbsearchentities&search=pacing-test'
        with patch.object(core, 'open_url', side_effect=fake_open), patch.dict(core._PACED_CACHE, clear=True):
            core.http_get(url)
            core.http_get(url)
            core.http_get('https://example.org/feed')
            core.http_get('https://example.org/feed')
        self.assertEqual(opened, [url, 'https://example.org/feed', 'https://example.org/feed'])

    def test_google_books_uses_a_configured_key(self):
        from media_intake import sources
        seen = []
        def fake_get(url, max_bytes=None):
            seen.append(url)
            return b'{"items": []}', 'https://www.googleapis.com/books/v1/volumes'
        with patch.object(sources, 'http_get', side_effect=fake_get):
            with patch.dict('os.environ', {'GOOGLE_BOOKS_API_KEY': 'abc123'}):
                sources.search_google_books_metadata('Dune')
            with patch.dict('os.environ', {}, clear=True):
                sources.search_google_books_metadata('Dune')
        self.assertIn('key=abc123', seen[0])
        self.assertNotIn('key=', seen[1])


class HundredSeriesTests(unittest.TestCase):
    """General problems found by the 100-series check."""

    def test_wikidata_search_falls_back_to_wikipedia_when_throttled(self):
        from media_intake import wikidata
        busy = urllib.error.HTTPError('https://www.wikidata.org/w/api.php', 429, 'Too Many Requests', {}, None)
        pages = {'query': {'pages': [
            {'index': 2, 'title': 'Shrek (franchise)', 'description': 'DreamWorks Animation media franchise',
             'pageprops': {'wikibase_item': 'Q1203222'}},
            {'index': 1, 'title': 'Shrek', 'description': '2001 DreamWorks Animation film',
             'pageprops': {'wikibase_item': 'Q483815'}},
            {'index': 3, 'title': 'Shrek (disambiguation)', 'pageprops': {'wikibase_item': 'Q1', 'disambiguation': ''}}]}}
        def fake_get(url, max_bytes=None):
            if 'wikidata.org' in url:
                raise busy
            return json.dumps(pages).encode(), 'https://en.wikipedia.org/w/api.php'
        with patch.object(wikidata, 'http_get', side_effect=fake_get):
            rows = wikidata.search_wikidata('Shrek')
        self.assertEqual([(r['qid'], r['title']) for r in rows], [('Q483815', 'Shrek'), ('Q1203222', 'Shrek')])

    def test_organisations_are_not_offered_as_book_authors(self):
        from media_intake.sources import _CORPORATE_AUTHOR
        for name in ('Nuffield Foundation.', 'National Science Foundation (U.S.)',
                     'International Conference on the Three Body Problem (1st 1969 Birmingham)'):
            self.assertTrue(_CORPORATE_AUTHOR.search(name), name)
        for name in ('Isaac Asimov', 'Ursula K. Le Guin', 'Clubb Smith', 'Steven Pressfield'):
            self.assertFalse(_CORPORATE_AUTHOR.search(name), name)

    def test_common_word_series_is_found_by_asking_for_the_series(self):
        from media_intake import watched
        calls = []
        def fake_search(query, limit=20):
            calls.append(query)
            if query == 'Foundation series':
                return [{'qid': 'Q2', 'title': 'Foundation series', 'description': 'science fiction book series by Isaac Asimov'}]
            return [{'qid': 'Q1', 'title': 'Foundation', 'description': 'type of nonprofit organization'}]
        with patch.object(watched, 'search_wikidata', side_effect=fake_search), \
                patch.object(watched, '_wikidata_series_members', return_value=[]):
            found = watched._search_wikidata_series('Foundation')
        self.assertEqual([row['qid'] for row in found], ['Q2'])
        self.assertEqual(calls, ['Foundation', 'Foundation series'])

    def test_exact_title_film_precedes_a_promoted_prefix_series(self):
        from media_intake.discovery import _promote_matching_series_results
        rows = [{'title': 'Rocky Road', 'entity_type': 'series'}, {'title': 'Rocky', 'entity_type': 'work'}]
        ordered = _sort_by_title_relevance(_promote_matching_series_results(rows, 'Rocky'), 'Rocky')
        self.assertEqual([r['title'] for r in ordered], ['Rocky', 'Rocky Road'])

    def test_plain_comic_search_leads_with_the_longest_named_series(self):
        rows = [cm.normalize(dict(row, provider='GCD', source_url=row['title'] + row.get('issue_number', '')))
                for row in ({'title': 'Bone #1', 'comic_entity': 'issue', 'issue_number': '1', 'series_title': 'Bone'},
                            {'title': 'Bone', 'comic_entity': 'series', 'series_title': 'Bone', 'issue_count': 3,
                             'series_start_year': '2004'},
                            {'title': 'Bone', 'comic_entity': 'series', 'series_title': 'Bone', 'issue_count': 55,
                             'series_start_year': '1991'},
                            {'title': 'Bone Parish', 'comic_entity': 'series', 'series_title': 'Bone Parish'})]
        ordered = cm.sort_comics(rows, 'Bone')
        self.assertEqual([(r['title'], r.get('issue_count')) for r in ordered[:3]],
                         [('Bone', 55), ('Bone', 3), ('Bone #1', None)])
        self.assertEqual(cm.sort_comics(rows, 'Bone #1')[0]['comic_entity'], 'issue')


if __name__ == '__main__':
    unittest.main()
