"""
Classify the links on a listing page: which lead to event detail pages, and
which one is the next page of the listing.

Detail links are found structurally: listings render each event with the
same markup, so links that share a DOM "signature" and repeat 3+ times form
a candidate group. A group is accepted when most of its URLs look like event
pages — an event-ish path or date, or a path under the listing's own path
(/events → /events/jazz-night).
"""
from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

from scraper.discovery.events_page import same_site

_EVENT_PATH = re.compile(
    r'/(events?|shows?|gigs?|concerts?|calendar|performances?|tickets?|whats-on|happenings)/[^/]+|'
    r'/20\d\d[/-]\d\d([/-]\d\d)?\b|[?&](event|eid|event_id)=',
    re.IGNORECASE)
_NEXT_TEXT = re.compile(r'^\s*(next|more|older|load more|next page|›|»|→|>)\s*(events?|page|»|›)?\s*$',
                        re.IGNORECASE)
_PAGE_PARAM = ('page', 'paged', 'p', 'pg', 'offset')

MIN_GROUP = 3
MIN_EVENTISH = 0.5


@dataclass
class ListingLinks:
    details: list[str] = field(default_factory=list)
    next_page: Optional[str] = None


def classify(response, *, detail_css: Optional[str] = None,
             pagination_css: Optional[str] = None) -> ListingLinks:
    """Recipe selectors (when given) win over the structural heuristics."""
    result = ListingLinks()

    if detail_css:
        result.details = _unique(response.urljoin(h) for h in response.css(detail_css).getall())
    else:
        result.details = _detail_links(response)

    if pagination_css:
        href = response.css(pagination_css).get()
        result.next_page = response.urljoin(href) if href else None
    else:
        result.next_page = _next_page(response)
    return result


def _detail_links(response) -> list[str]:
    listing_path = urlparse(response.url).path.rstrip('/')
    groups: dict[str, list[str]] = defaultdict(list)
    for a in response.xpath('//body//a[@href][not(ancestor::nav) and not(ancestor::header) '
                            'and not(ancestor::footer)]'):
        url = response.urljoin(a.attrib['href']).split('#')[0]
        if not url.startswith('http') or not same_site(url, response.url) or url == response.url:
            continue
        groups[_signature(a)].append(url)

    best: list[str] = []
    best_score = 0.0
    for urls in groups.values():
        urls = _unique(urls)
        if len(urls) < MIN_GROUP:
            continue
        eventish = sum(1 for u in urls if _looks_like_event(u, listing_path)) / len(urls)
        if eventish < MIN_EVENTISH:
            continue
        score = eventish * len(urls)
        if score > best_score:
            best, best_score = urls, score
    return best


def _looks_like_event(url: str, listing_path: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip('/')
    if _EVENT_PATH.search(path + ('?' + parsed.query if parsed.query else '')):
        return True
    return bool(listing_path) and path.startswith(listing_path + '/')


def _signature(anchor) -> str:
    """Tag + first class of the anchor and its 3 nearest ancestors."""
    parts = []
    node = anchor
    for _ in range(4):
        tag = node.root.tag if isinstance(getattr(node.root, 'tag', None), str) else ''
        cls = (node.attrib.get('class') or '').split()
        parts.append(f"{tag}.{cls[0]}" if cls else tag)
        parent = node.xpath('..')
        if not parent:
            break
        node = parent[0]
    return '>'.join(reversed(parts))


def _next_page(response) -> Optional[str]:
    href = response.css('a[rel~="next"]::attr(href), link[rel~="next"]::attr(href)').get()
    if href:
        return response.urljoin(href)
    for a in response.xpath('//a[@href]'):
        text = ' '.join(a.xpath('.//text()').getall()).strip() or a.attrib.get('aria-label', '')
        if _NEXT_TEXT.match(text) or 'next' in (a.attrib.get('class') or '').lower().split():
            url = response.urljoin(a.attrib['href'])
            if url != response.url and same_site(url, response.url):
                return url
    return _incremented_page(response)


def _incremented_page(response) -> Optional[str]:
    """?page=2 → a link to ?page=3; /page/2/ → /page/3/ (only if the page links to it)."""
    current = urlparse(response.url)
    query = parse_qs(current.query)
    wanted_params = {name: str(int(query[name][0]) + 1) if name in query and query[name][0].isdigit()
                     else '2' for name in _PAGE_PARAM if name in query or not query}
    m = re.search(r'/page/(\d+)', current.path)
    wanted_path = f'/page/{int(m.group(1)) + 1}' if m else '/page/2'
    for href in response.xpath('//a/@href').getall():
        url = response.urljoin(href)
        if not same_site(url, response.url):
            continue
        parsed = urlparse(url)
        if parsed.path.rstrip('/').endswith(wanted_path):
            return url
        target = parse_qs(parsed.query)
        if parsed.path == current.path and any(
                target.get(name, [None])[0] == value for name, value in wanted_params.items()):
            return url
    return None


def _unique(urls) -> list[str]:
    return list(dict.fromkeys(urls))
