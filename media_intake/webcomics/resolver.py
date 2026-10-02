"""Layered discovery with deterministic adapters and explicit manual recipes."""
import urllib.parse
from .adapters import HtmlAdapter, default_registry
from .extraction import Document, Recipe
from .http import ExtractionError
from .models import canonicalize_url


def registry_for(config=None):
    registry = default_registry()
    recipe = (config or {}).get('recipe')
    if recipe:
        adapter = HtmlAdapter(Recipe.from_dict(recipe))
        registry.adapters[adapter.id] = adapter
    return registry


def resolve(url, client, config=None, registry=None):
    url = canonicalize_url(url)
    config = dict(config or {})
    registry = registry or registry_for(config)
    if config.get('adapter') or config.get('recipe'):
        adapter = registry.select(url, adapter_id=config.get('adapter') or config['recipe'].get('id', 'static'))
    else:
        # Public APIs that can resolve a URL without a page fetch.
        api = registry.adapters.get('xkcd')
        if api and api.probe(url):
            adapter = api
        else:
            response = client.get(url)
            feed = registry.adapters.get('feed')
            if feed and feed.probe(url, response):
                adapter = feed
                url = response.url
            else:
                document = Document(response.body)
                for node in document.select('link'):
                    if ('alternate' in (node.attrs.get('rel') or '').split() and
                            node.attrs.get('type') in {'application/rss+xml', 'application/atom+xml', 'application/feed+json'} and
                            node.attrs.get('href')):
                        url = canonicalize_url(urllib.parse.urljoin(response.url, node.attrs['href']))
                        adapter = registry.select(url, adapter_id='feed')
                        config['discovered_from'] = response.url
                        break
                else:
                    # Structured metadata enriches identity; never use a cover as
                    # a content page. Archive extraction needs positive evidence.
                    adapter = registry.select(response.url, response)
                    if adapter.id == 'comicfury' and not document.select(adapter.recipe.archive_selector):
                        archive = next((node.attrs.get('href') for node in document.select('a')
                                        if node.text().casefold() == 'archive'), None)
                        if archive:
                            config['recipe'] = {**adapter.recipe.__dict__,
                                'archive_url': canonicalize_url(urllib.parse.urljoin(response.url, archive))}
                    url = response.url
    url = adapter.canonicalize_url(url)
    snapshot = adapter.resolve_series(url, client, {}, config)
    if not snapshot.entries:
        raise ExtractionError('No comic entries discovered; a manual recipe is required')
    config['adapter'] = adapter.id
    return adapter, snapshot, config
