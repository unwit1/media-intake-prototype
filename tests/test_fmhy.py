import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from media_intake.fmhy import candidates, fallback_search, parse_reading_page, refresh_directory
from media_intake.sources import resolve_detail


class FMHYTest(unittest.TestCase):
    def test_extract_sections_and_mirrors(self):
        page = '''<h3 id="ebooks">Ebooks</h3><ul><li><a href="https://example.org">Example Books</a>, <a href="https://mirror.example.org">2</a> - Books</li></ul><h3 id="public-domain">Public Domain</h3><ul><li><a href="https://standardebooks.org">Standard Ebooks</a></li></ul><h3 id="readers">Ebook Readers</h3><ul><li><a href="https://reader.example">Reader</a></li></ul>'''
        sites = parse_reading_page(page)
        self.assertEqual(len(sites), 2)
        self.assertEqual(sites[0]['mirrors'], ['https://mirror.example.org'])
        self.assertEqual(sites[1]['section'], 'Public Domain')
        with tempfile.TemporaryDirectory() as temp:
            html = Path(temp) / 'page.html'; html.write_text(page)
            out = Path(temp) / 'directory.json'
            self.assertEqual(refresh_directory(out, html)['count'], 2)
            self.assertEqual(len(candidates('a title', out)), 2)

    def test_fallback_when_preferred_misses(self):
        with patch('media_intake.sources.search_openlibrary', return_value=[]):
            result = fallback_search('missing title', limit=3)
        self.assertEqual(result['matches'], [])
        self.assertEqual(len(result['fallback_sites']), 3)
        self.assertIn('site:', parse_qs(urlsplit(result['fallback_sites'][0]['search_url']).query)['q'][0])
        with patch('media_intake.sources.search_openlibrary', return_value=[{'title':'Missing Title','author':'Writer','url':'https://example.org/book.epub'}]):
            result = fallback_search('missing title', limit=3)
        self.assertEqual(len(result['matches']), 1)
        self.assertEqual(result['fallback_sites'], [])

    def test_generic_detail_file_link(self):
        with tempfile.TemporaryDirectory() as temp:
            html = Path(temp) / 'detail.html'
            html.write_text('<a href="/books/item.epub">EPUB download</a>')
            results = resolve_detail('generic', 'https://books.example.org/item', 'Item', html_path=html)
            self.assertEqual(results[0]['url'], 'https://books.example.org/books/item.epub')


if __name__ == '__main__': unittest.main()
