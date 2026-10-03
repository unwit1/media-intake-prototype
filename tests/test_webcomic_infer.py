"""Recipe inference: archive link groups, varying-image evidence, no OG-only activation, reuse per host."""
import tempfile
import unittest

from media_intake.core import connect
from media_intake.webcomics.infer import infer_recipe, recipe_for, save_recipe
from media_intake.webcomics.http import Response
from media_intake.webcomics.watcher import add_series

BASE = 'https://strips.example'


def page(body, title='Fixture Strips'):
    return (f'<html><head><title>{title}</title><meta property="og:image" content="{BASE}/og.png"></head>'
            f'<body><img class="logo" src="/logo.png">{body}</body></html>').encode()


def site(comic_images=True, count=30):
    links = ''.join(f'<li><a href="/strip/{n}/the-{n}-th-day">Day {n}</a></li>' for n in range(1, count + 1))
    pages = {
        BASE + '/': page('<nav><a href="/about">About</a> <a href="/archive">Archive</a> <a href="/shop">Shop</a></nav>'
                         '<div id="comic"><img src="/art/latest.png"></div>'),
        BASE + '/archive': page(f'<nav><a href="/">Home</a><a href="/about">About</a></nav>'
                                f'<div class="strip-list"><ul>{links}</ul></div>'
                                '<aside class="blogroll"><a href="https://friend.example/">Friend</a></aside>'),
    }
    for n in range(1, count + 1):
        art = f'<div id="comic"><img src="/art/{n}.png"></div>' if comic_images else '<p>Text only</p>'
        pages[f'{BASE}/strip/{n}/the-{n}-th-day'] = page(art, f'Day {n}')
    return pages


class Client:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **kwargs):
        if url not in self.pages:
            from media_intake.webcomics.http import SourceUnavailable
            raise SourceUnavailable(404, url)
        return Response(self.pages[url], url, {})


class InferTests(unittest.TestCase):
    def test_infers_archive_and_image_selectors_with_samples(self):
        result = infer_recipe(Client(site()), BASE + '/')
        recipe = result['recipe']
        self.assertEqual((recipe['archive_url'], recipe['archive_selector'], recipe['image_selector']),
                         (BASE + '/archive', 'div.strip-list a', '#comic img'))
        self.assertEqual(recipe['domains'], ['strips.example'])
        self.assertEqual(len(result['samples']), 3)
        self.assertEqual(result['samples'][0]['images'], [BASE + '/art/1.png'])
        self.assertGreater(result['confidence'], 0.9)
        self.assertTrue(any('30 archive links' in line for line in result['evidence']))

    def test_og_image_or_repeating_logo_is_never_enough(self):
        self.assertIsNone(infer_recipe(Client(site(comic_images=False)), BASE + '/'))

    def test_too_few_links_gives_no_candidate(self):
        self.assertIsNone(infer_recipe(Client(site(count=3)), BASE + '/'))

    def test_accepted_recipe_is_reused_for_the_same_site(self):
        result = infer_recipe(Client(site()), BASE + '/')
        with tempfile.TemporaryDirectory() as root:
            db = connect(root)
            try:
                self.assertIsNone(recipe_for(db, BASE + '/'))
                save_recipe(db, result['recipe'])
                reused = recipe_for(db, 'https://www.strips.example/other-comic/')
                self.assertEqual(reused['archive_selector'], 'div.strip-list a')
                add_series(db, BASE + '/', client=Client(site()), config={'recipe': reused})
                count = db.execute('SELECT COUNT(*) FROM webcomic_entries').fetchone()[0]
                self.assertEqual(count, 30)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
