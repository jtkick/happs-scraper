"""
A page's text: its main content, and all of it.

main_text(html) → str | None
    The main content via trafilatura (what dates and the model read); all of
    the page's text when that comes back thin (a JS shell's "browser not supported").

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
from typing import Any, Iterator, Optional

from lxml import etree, html as lxml_html

from scraper.extractors.inline_json import _iter_scripts, _json_from_script
from scraper.util import strip_tags

MAX_CHARS = 50_000
# Below this, trafilatura found no real main content.
THIN_TEXT = 200

_DROP = '//script|//style|//noscript|//template|//svg|//head'
_BLOCK = (
    'address', 'article', 'aside', 'blockquote', 'br', 'dd', 'div', 'dl', 'dt', 'figcaption',
    'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav',
    'ol', 'p', 'pre', 'section', 'table', 'td', 'th', 'tr', 'ul',
)
_URLISH = re.compile(r'^(?:https?:|/|www\.|#)|^\S+\.(?:jpe?g|png|gif|webp|svg|css|js)$', re.IGNORECASE)


def main_text(html: str, *, tables: bool = False, full: Optional[str] = None) -> Optional[str]:
    """`full` is this page's full_text when already computed."""
    try:
        import trafilatura
    except ImportError:
        text = None
    else:
        text = trafilatura.extract(html, include_comments=False, include_tables=tables)
    if text and len(text) >= THIN_TEXT:
        return text
    return (full if full is not None else full_text(html)) or text


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
                text = (strip_tags(value) or '') if '<' in value else html_lib.unescape(_collapse(value).replace('\n', ' '))
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
