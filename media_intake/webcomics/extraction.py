"""Small deterministic DOM parser and declarative recipe validation (no eval)."""
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re
from ..serial import VOID_TAGS
from .http import ExtractionError


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: object = None
    chunks: list = field(default_factory=list)

    def text(self):
        values, stack = [], list(reversed(self.chunks))
        while stack:
            item = stack.pop()
            if isinstance(item, Node):
                stack.extend(reversed(item.chunks))
            else:
                values.append(item)
        return ' '.join(' '.join(values).split())

    def matches(self, selector):
        return any(self._chain(part.strip().split()) for part in selector.split(','))

    def _simple(self, selector):
        match = re.fullmatch(r'([\w-]+|\*)?((?:[.#][\w-]+)*)', selector)
        if not match:
            raise ValueError('Selectors support tags, IDs, classes, descendants and commas')
        tag, qualifiers = match.groups()
        if tag and tag != '*' and self.tag != tag:
            return False
        for symbol, name in re.findall(r'([.#])([\w-]+)', qualifiers):
            if symbol == '#' and self.attrs.get('id') != name:
                return False
            if symbol == '.' and name not in self.attrs.get('class', '').split():
                return False
        return True

    def _chain(self, parts):
        if not parts or not self._simple(parts[-1]):
            return False
        parent = self.parent
        for selector in reversed(parts[:-1]):
            while parent and not parent._simple(selector):
                parent = parent.parent
            if not parent:
                return False
            parent = parent.parent
        return True

    def select(self, selector):
        result, stack = [], list(reversed(self.children))
        while stack:
            node = stack.pop()
            if node.matches(selector):
                result.append(node)
            stack.extend(reversed(node.children))
        return result


class Document(HTMLParser):
    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]
        self.count = 0
        self.feed(body.decode('utf-8-sig', 'replace') if isinstance(body, bytes) else body)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 100000 or len(self.stack) > 256:
            raise ExtractionError('HTML exceeds node/depth budget')
        node = Node(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack[-1].chunks.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].chunks.append(data)

    def select(self, selector):
        return self.root.select(selector)


@dataclass
class Recipe:
    id: str = 'static'
    domains: list = field(default_factory=list)
    version: str = '1'
    title_selector: str = 'h1,title'
    archive_selector: str = '#archive a,.comic-archive a'
    image_selector: str = '#comic img,.comic img,img.comic'
    chapter_selector: str = 'h2,h3'
    date_selector: str = 'time'
    next_selector: str = ''
    archive_url: str | None = None
    order: str = 'source'
    max_pages: int = 20
    complete: bool = False

    @classmethod
    def from_dict(cls, data):
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError('Unknown recipe fields: ' + ', '.join(sorted(unknown)))
        recipe = cls(**data)
        if not recipe.id or not recipe.version or not isinstance(recipe.domains, list):
            raise ValueError('Recipe requires an ID, version and domain list')
        if not 1 <= recipe.max_pages <= 100 or recipe.order not in {'source', 'reverse', 'natural'}:
            raise ValueError('Invalid pagination bound or ordering')
        for key in ('title_selector', 'archive_selector', 'image_selector', 'chapter_selector', 'date_selector', 'next_selector'):
            value = getattr(recipe, key)
            if not isinstance(value, str) or len(value) > 300:
                raise ValueError('Invalid selector length')
            if value:
                for part in value.split(','):
                    for selector in part.strip().split():
                        Node('test')._simple(selector)
        return recipe


def load_recipes(path):
    with open(path, encoding='utf-8') as stream:
        payload = json.load(stream)
    if not isinstance(payload, list) or len(payload) > 500:
        raise ValueError('Recipe file must contain at most 500 recipes')
    return [Recipe.from_dict(value) for value in payload]
