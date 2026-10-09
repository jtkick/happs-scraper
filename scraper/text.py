"""
All of a page's text, for passes that look for one phrase anywhere on it
(recurrence) and as a fallback when trafilatura finds no main content.

full_text(html) → str
    The visible DOM text, one block element per line, followed by the text
    in the page's embedded JSON (inline_json's scripts) that isn't already
    visible. On a rendered page the DOM already holds what JavaScript drew;
    on a raw one the embedded data is often all there is.
"""
from __future__ import annotations
import html as html_lib
import json
import re
from typing import Any, Iterator

from lxml import etree, html as lxml_html

from scraper.extractors.inline_json import _iter_scripts, _json_from_script, _strip_html

MAX_CHARS = 50_000

_DROP = '//script|//style|//noscript|//template|//svg|//head'
_BLOCK = (
    'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt', 'figcaption',
    'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav',
    'ol', 'p', 'pre', 'section', 'table', 'td', 'th', 'tr', 'ul',
)
_URLISH = re.compile(r'^(?:https?:|/|www\.|#)|^\S+\.(?:jpe?g|png|gif|webp|svg|css|js)$', re.IGNORECASE)


def full_text(html: str) -> str:
    visible = visible_text(html)
    data = [s for s in _data_strings(html) if s not in visible]
    return '\n'.join(filter(None, [visible, *data]))[:MAX_CHARS]


def visible_text(html: str) -> str:
    try:
        doc = lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        return _collapse(re.sub(r'<[^>]+>', ' ', html))
    for node in doc.xpath(_DROP):
        node.drop_tree()
    for node in doc.iter(*_BLOCK):
        node.tail = '\n' + (node.tail or '')
        node.text = '\n' + (node.text or '')
    return _collapse(doc.text_content())


def _collapse(text: str) -> str:
    lines = (re.sub(r'\s+', ' ', line).strip() for line in text.splitlines())
    return '\n'.join(line for line in lines if line)


def _data_strings(html: str) -> Iterator[str]:
    seen: set[str] = set()
    for attrs, content in _iter_scripts(html):
        if 'ld+json' in attrs.lower():
            continue
        for parsed in _json_from_script(attrs, content):
            for value in _strings(parsed):
                text = html_lib.unescape(_strip_html(value) if '<' in value else _collapse(value).replace('\n', ' '))
                if text not in seen and ' ' in text and re.search(r'[A-Za-z]{2}', text) \
                        and not _URLISH.search(text):
                    seen.add(text)
                    yield text


def _strings(data: Any, depth: int = 0) -> Iterator[str]:
    if depth > 12:
        return
    if isinstance(data, str):
        if data.strip().startswith(('{', '[')):
            try:
                yield from _strings(json.loads(data), depth + 1)
                return
            except json.JSONDecodeError:
                pass
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from _strings(value, depth + 1)
    elif isinstance(data, list):
        for item in data[:500]:
            yield from _strings(item, depth + 1)
