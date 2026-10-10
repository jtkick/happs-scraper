"""
Each listing event's own page: the link in its entry on the listing.

attach(html, page_url, events, detail_links=()) → None
    For events whose parser gave no `url` (the model reads the page's text, so
    never sees links; a recipe may have no link field), find the entry holding
    the event's title and take its link. The entry is the title's nearest
    ancestors, up to one that also holds another event's title; the closest
    link wins that is the title itself, says "more info"/"details", is one of
    the listing's repeated detail links, or (when it's the entry's only link)
    looks like an event page. Not one shared by differently named events, or a
    page like /about. A found link sets `url` and GUESSED, so the detail page
    must show it's the same event (same_event) before its data is used.

same_event(listing, detail) → bool
    Does a detail page's event share most of the listing entry's title words?
"""
from __future__ import annotations
import re
from itertools import chain
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

from lxml import etree, html as lxml_html

from scraper.discovery.events_page import same_site
from scraper.discovery.links import looks_like_event
from scraper.follow import page_key
from scraper.util import fold

GUESSED = '_link_guessed'

# Shorter titles ("Jam") turn up in too much other text.
MIN_TITLE = 4
# How far up from the title an entry may reach.
MAX_CLIMB = 6
# A same-event detail page repeats at least this share of the listing title's words.
SAME_EVENT_SHARE = 0.5

_DROP = '//script|//style|//noscript|//template|//svg|//head'
_MORE = re.compile(r'\b(more|info|details?|read|learn|view|see|find out|full)\b', re.IGNORECASE)
_NOT_A_PAGE = re.compile(r'\.(pdf|jpe?g|png|gif|webp|svg|ics|zip|docx?)$|[?&](ical|outlook-ical|share)=',
                         re.IGNORECASE)
# A site's own pages that are never one event's.
_SITE_PAGE = re.compile(
    r'^/(about(-us)?|contact(-us)?|menus?|food|drinks|privacy(-policy)?|terms|careers|jobs|faqs?|team|'
    r'gift-?cards?|reservations?|book|login|account|cart|shop|private-(events|parties)|weddings)/?$',
    re.IGNORECASE)
_QUOTES = str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-'})
_WORD = re.compile(r'[^\W_]{3,}')


def attach(html: str, page_url: str, events: list[dict], detail_links: Iterable[str] = ()) -> None:
    titles = list(dict.fromkeys(t for t in (_norm(e.get('title')) for e in events) if len(t) >= MIN_TITLE))
    wanted = {_norm(e.get('title')) for e in events if not e.get('url')} & set(titles)
    if not wanted:
        return
    try:
        doc = lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        return
    for node in doc.xpath(_DROP):
        node.drop_tree()
    body = doc.find('body')
    page = _Page(body if body is not None else doc, page_url, detail_links)

    matches = {title: page.deepest(title) for title in titles}
    for node in set(chain.from_iterable(matches.values())):
        for holder in chain([node], node.iterancestors()):
            page.titles_in[holder] = page.titles_in.get(holder, 0) + 1

    guessed: dict[str, set] = {}
    for title in wanted:
        found = [page.link(node, title) for node in matches[title]]
        group = [e for e in events if _norm(e.get('title')) == title]
        distinct = list(dict.fromkeys(filter(None, found)))
        if len(distinct) == 1:
            found = [distinct[0]] * len(group)
        elif len(distinct) == len(group):
            found = distinct            # each entry shown more than once (a calendar cell and its popup)
        elif len(found) != len(group):
            continue
        for event, url in zip(group, found):
            if url and not event.get('url'):
                event['url'] = url
                event[GUESSED] = True
                guessed.setdefault(url, set()).add(title)
    # One link for differently named events is a section of the site, not either's own page.
    for event in events:
        if event.get(GUESSED) and len(guessed[event['url']]) > 1:
            del event['url'], event[GUESSED]


def same_event(listing: dict, detail: dict) -> bool:
    words = set(_WORD.findall(_norm(listing.get('title'))))
    if not words:
        return True
    shared = words & set(_WORD.findall(_norm(detail.get('title'))))
    return len(shared) >= SAME_EVENT_SHARE * len(words)


class _Page:

    def __init__(self, root, page_url: str, detail_links: Iterable[str]):
        self.root, self.page_url = root, page_url
        self.listing_path = urlparse(page_url).path.rstrip('/')
        self.detail = {page_key(u) for u in detail_links}
        self.titles_in: dict = {}
        self._text: dict = {}

    def text(self, node) -> str:
        if node not in self._text:
            self._text[node] = _norm(node.text_content())
        return self._text[node]

    def deepest(self, title: str) -> list:
        """The innermost elements holding `title`, in page order."""
        if title not in self.text(self.root):
            return []
        found, stack = [], [self.root]
        while stack:
            node = stack.pop()
            inner = [c for c in node if isinstance(c.tag, str) and title in self.text(c)]
            if not inner:
                found.append(node)
            stack.extend(reversed(inner))
        return found

    def link(self, match, title: str) -> Optional[str]:
        for level, node in enumerate(chain([match], match.iterancestors())):
            if level > MAX_CLIMB or node is self.root or node.tag in ('body', 'html'):
                return None
            if node is not match and self.titles_in.get(node, 0) > 1:
                return None
            best = self._best(node.iter('a'), title)
            if best:
                return best
        return None

    def _best(self, anchors, title: str) -> Optional[str]:
        scores: dict[str, int] = {}
        for a in anchors:
            url = self._url(a)
            if not url or a.xpath('ancestor::nav|ancestor::header|ancestor::footer'):
                continue
            label = _norm(a.text_content()) or _norm(a.get('aria-label') or a.get('title'))
            score = 0
            if label and (title in label or (len(label) >= 8 and label in title)):
                score += 3
            if page_key(url) in self.detail:
                score += 3
            if label and _MORE.search(label):
                score += 2
            if looks_like_event(url, self.listing_path):
                score += 1
            scores[url] = max(score, scores.get(url, 0))
        if not scores:
            return None
        url = max(scores, key=scores.get)
        return url if scores[url] >= 2 or (len(scores) == 1 and scores[url]) else None

    def _url(self, anchor) -> Optional[str]:
        href = (anchor.get('href') or '').strip()
        if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            return None
        url = urljoin(self.page_url, href).split('#')[0]
        if (not url.startswith('http') or not same_site(url, self.page_url)
                or page_key(url) == page_key(self.page_url)):
            return None
        return None if _NOT_A_PAGE.search(url) or _SITE_PAGE.match(urlparse(url).path) else url


def _norm(text) -> str:
    return fold(str(text or '').translate(_QUOTES))
