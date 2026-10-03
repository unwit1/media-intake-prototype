"""Suggest a recipe for a comic site that no adapter understands, with evidence and a dry run.

1. Archive page: the page itself, plus any same-site link labelled "archive".
2. Entry links: the largest group of same-site links sharing a path pattern
   (digits and slugs normalised); the selector is the nearest ancestor with an
   ID or class that contains most of the group, accepted only when what it
   selects is mostly that group.
3. Images: three sampled entries are fetched; an image selector is accepted only
   if it finds an image on every sample *and* the image differs between pages
   (site logos and banners repeat). Open Graph images are never evidence.

The result is a candidate, never activated by itself: the caller shows the
sample previews and saves the recipe (per host, so later comics on the same
site reuse it) only after the user accepts it.
"""
import json
import re
import urllib.parse
from collections import Counter

from .extraction import Document, Recipe
from .models import canonicalize_url

IMAGE_CANDIDATES = ('#comic img', '.comic img', 'img.comic', '#cc-comic', '#comic-image img', '.webcomic-image img',
                    '.comic-page img', '#comicimage', '.entry-content img', 'article img', 'main img')
MIN_LINKS = 5


def _host(url):
    return (urllib.parse.urlsplit(url).hostname or '').casefold()


def _pattern(url):
    path = urllib.parse.urlsplit(url).path.rstrip('/') or '/'
    parts = path.split('/')
    parts = [re.sub(r'\d+', '#', part) for part in parts]
    if len(parts) > 1 and not re.fullmatch(r'#?', parts[-1]):
        parts[-1] = '*'  # the slug varies per entry
    query = urllib.parse.urlsplit(url).query
    return '/'.join(parts) + ('?' + re.sub(r'=\d+', '=#', query) if query else '')


def _selector_for(node):
    if node.attrs.get('id'):
        return f"#{node.attrs['id']}"
    classes = [name for name in (node.attrs.get('class') or '').split() if re.fullmatch(r'[\w-]+', name)]
    return f'{node.tag}.{classes[0]}' if classes else None


def _link_group(document, base):
    host = _host(base)
    links = []
    for node in document.select('a'):
        href = node.attrs.get('href')
        if not href:
            continue
        try:
            target = canonicalize_url(urllib.parse.urljoin(base, href))
        except ValueError:
            continue
        if _host(target) == host and target.rstrip('/') != base.rstrip('/'):
            links.append((node, target))
    groups = Counter(_pattern(target) for _node, target in links)
    if not groups:
        return None, []
    pattern, size = groups.most_common(1)[0]
    if size < MIN_LINKS:
        return None, []
    return pattern, [(node, target) for node, target in links if _pattern(target) == pattern]


def _archive_selector(document, group):
    """The tightest ancestor selector that keeps most of the group and little else."""
    members = {id(node) for node, _target in group}
    candidates = Counter()
    for node, _target in group:
        parent = node.parent
        while parent is not None and parent.tag != 'document':
            selector = _selector_for(parent)
            if selector:
                candidates[selector] += 1
            parent = parent.parent
    best = None
    for selector, covered in candidates.most_common():
        if covered < len(group) * 0.8:
            break
        picked = document.select(f'{selector} a')
        precision = sum(id(node) in members for node in picked) / max(1, len(picked))
        if precision >= 0.8 and (best is None or len(picked) < best[2]):
            best = (f'{selector} a', precision, len(picked))
    return best


def _image_selector(samples):
    """A selector that finds an image on every sample, with a different image on each page."""
    for selector in IMAGE_CANDIDATES:
        found = []
        for url, document in samples:
            nodes = document.select(selector)
            images = [node if node.tag == 'img' else next(iter(node.select('img')), None) for node in nodes]
            srcs = [image.attrs.get('src') or image.attrs.get('data-src') for image in images if image is not None]
            srcs = [urllib.parse.urljoin(url, src) for src in srcs if src]
            if not srcs:
                break
            found.append(srcs)
        else:
            if len({tuple(srcs) for srcs in found}) == len(found):
                return selector, found
    return None, []


def infer_recipe(client, url, sample_count=3):
    """Return {'recipe', 'confidence', 'evidence', 'samples'} or None when nothing convincing is found."""
    start = client.get(url)
    pages = [(start.url, Document(start.body))]
    for node in pages[0][1].select('a'):
        href, label = node.attrs.get('href'), node.text().casefold()
        if href and 'archive' in (label + ' ' + href.casefold()):
            target = canonicalize_url(urllib.parse.urljoin(start.url, href))
            if _host(target) == _host(start.url) and target != start.url:
                response = client.get(target)
                pages.append((response.url, Document(response.body)))
                break
    best = None
    for page_url, document in pages:
        pattern, group = _link_group(document, page_url)
        if not group:
            continue
        chosen = _archive_selector(document, group)
        if chosen and (best is None or len(group) > len(best['group'])):
            best = {'archive_url': page_url, 'selector': chosen[0], 'precision': chosen[1], 'group': group,
                    'pattern': pattern}
    if best is None:
        return None
    sample_links = [target for _node, target in best['group'][:sample_count]]
    samples = [(link, Document(client.get(link).body)) for link in sample_links]
    image_selector, images = _image_selector(samples)
    if not image_selector:
        return None
    host = _host(start.url)
    recipe = Recipe(id=f'inferred-{host}', domains=[host], archive_url=best['archive_url'],
                    archive_selector=best['selector'], image_selector=image_selector).__dict__
    confidence = round(best['precision'] * min(1.0, len(best['group']) / 20) ** 0.25, 2)
    return {'recipe': recipe, 'confidence': confidence,
            'evidence': [f"{len(best['group'])} archive links share the pattern {best['pattern']}",
                         f"'{best['selector']}' selects them with {best['precision']:.0%} precision",
                         f"'{image_selector}' finds a different image on each of {len(samples)} sample pages"],
            'samples': [{'url': link, 'images': srcs} for (link, _document), srcs in zip(samples, images)]}


def save_recipe(db, recipe):
    recipes = saved_recipes(db)
    recipes = [item for item in recipes if item.get('id') != recipe['id']] + [recipe]
    db.execute('INSERT INTO gui_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               ('webcomic_recipes', json.dumps(recipes)))
    db.commit()


def saved_recipes(db):
    row = db.execute("SELECT value FROM gui_settings WHERE key='webcomic_recipes'").fetchone()
    try:
        return json.loads(row[0]) if row else []
    except (TypeError, ValueError):
        return []


def recipe_for(db, url):
    """A saved recipe whose domains cover this URL, for reuse on later comics from the same site."""
    host = _host(url)
    for recipe in saved_recipes(db):
        if any(host == domain or host.endswith('.' + domain) for domain in recipe.get('domains') or []):
            return recipe
    return None
