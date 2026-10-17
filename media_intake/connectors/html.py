"""A small forgiving HTML tree for scraping listing pages without third-party parsers."""
from html.parser import HTMLParser

_VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
# Tags whose open element is closed implicitly by a sibling of the same family.
_IMPLIED_CLOSE = {'tr': {'tr'}, 'td': {'td', 'th'}, 'th': {'td', 'th'}, 'li': {'li'},
                  'p': {'p', 'div', 'table', 'ul', 'ol'}, 'option': {'option'}}


class Node:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.children, self.parent = tag, dict(attrs or {}), [], parent

    def get(self, name, default=''):
        value = self.attrs.get(name)
        return default if value is None else value

    @property
    def classes(self):
        return set(self.get('class').split())

    def text(self, separator=' '):
        parts = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text(separator))
        return ' '.join(separator.join(parts).split()) if separator == ' ' else separator.join(parts)

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def find_all(self, tag=None, class_=None, **attrs):
        wanted = set(class_.split()) if class_ else set()
        for node in self.iter():
            if tag and node.tag != tag:
                continue
            if wanted and not wanted <= node.classes:
                continue
            if any((node.attrs.get(name) != value) if value is not True else name not in node.attrs
                   for name, value in attrs.items()):
                continue
            yield node

    def find(self, tag=None, class_=None, **attrs):
        return next(self.find_all(tag, class_, **attrs), None)


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('#root')
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        closes = _IMPLIED_CLOSE.get(tag)
        if closes:
            node = self.current
            while node is not self.root and node.tag not in ('table', 'tbody', 'thead', 'ul', 'ol', 'select'):
                if node.tag in closes:
                    self.current = node.parent
                    break
                node = node.parent
        node = Node(tag, [(name, value if value is not None else '') for name, value in attrs], self.current)
        self.current.children.append(node)
        if tag not in _VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, [(name, value if value is not None else '') for name, value in attrs], self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.current = node.parent

    def handle_data(self, data):
        if data:
            self.current.children.append(data)


def parse_html(text):
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    builder = _Builder()
    builder.feed(text)
    builder.close()
    return builder.root
