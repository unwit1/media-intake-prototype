import unittest

from media_intake.link_capture import classify_link, classify_links, queue_item

MAGNET = 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Some+Pack'


class LinkCaptureTests(unittest.TestCase):
    def test_classifies_download_links_and_ignores_pages(self):
        self.assertEqual(classify_link(MAGNET), ('magnet', '.torrent'))
        self.assertIsNone(classify_link('magnet:?dn=no-hash'))
        self.assertEqual(classify_link('https://tracker.example/get/abc.torrent'), ('torrent', '.torrent'))
        self.assertEqual(classify_link('https://pixeldrain.com/u/abc'), ('file_host', None))
        self.assertEqual(classify_link('https://www.mediafire.com/file/x/Book.epub/file'), ('file_host', None))
        self.assertEqual(classify_link('https://ouo.io/abc'), ('shortlink', None))
        self.assertEqual(classify_link('https://files.example/a/Book%20One.epub'), ('direct', '.epub'))
        self.assertEqual(classify_link('https://files.example/a/tool.tar.gz'), ('direct', '.tar.gz'))
        for page in ('https://example.com/article', 'https://github.com/a/b', 'ftp://x.example/a.epub',
                     'https://user:pw@files.example/a.epub', 'not a link'):
            self.assertIsNone(classify_link(page), page)

    def test_extracts_links_from_copied_text(self):
        text = (f'Get it here: https://ouo.io/abc, or {MAGNET} (mirror: https://files.example/x.cbz).\n'
                'Reviews at https://example.com/review and again https://ouo.io/abc')
        self.assertEqual([(link['kind'], link['url']) for link in classify_links(text)],
                         [('shortlink', 'https://ouo.io/abc'), ('magnet', MAGNET),
                          ('direct', 'https://files.example/x.cbz')])
        self.assertEqual(classify_links(''), [])

    def test_queue_items(self):
        torrent = queue_item({'url': MAGNET, 'kind': 'magnet', 'format': '.torrent'})
        self.assertEqual((torrent['title'], torrent['format'], torrent['format_guessed']), ('Some Pack', '.torrent', False))
        short = queue_item({'url': 'https://ouo.io/abc', 'kind': 'shortlink', 'format': None}, '.epub')
        self.assertEqual((short['title'], short['format'], short['format_guessed']), ('abc', '.epub', True))
        direct = queue_item({'url': 'https://files.example/a/Book%20One.epub', 'kind': 'direct', 'format': '.epub'})
        self.assertEqual(direct['title'], 'Book One')


if __name__ == '__main__':
    unittest.main()
