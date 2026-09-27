"""
Find the page(s) on a site that list its events, starting from the homepage.

Heuristic link scoring first (free); scraper/extractors/ai.pick_events_links
is the fallback the spider uses when nothing scores high enough.
"""
from __future__ import annotations
import re
from typing import Optional
from urllib.parse import urlparse

import tldextract

_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())

# Path / anchor vocabulary for event listings, with weights.
_STRONG = ('events', 'event', 'calendar', 'whats-on', 'whatson', 'shows', 'gigs',
           'concerts', 'live-music', 'livemusic', 'lineup', 'line-up', 'happenings',
           'upcoming', 'schedule', 'performances', 'programme', 'program', 'agenda')
_ANCHOR_RE = re.compile(
    r"\b(events?|calendar|what'?s on|shows?|gigs?|concerts?|live music|line-?up|"
    r"happenings|upcoming|schedule|performances|programme|program|agenda|tickets)\b",
    re.IGNORECASE)
_NEGATIVE = ('past', 'archive', 'private', 'book', 'booking', 'reservation', 'reserve',
             'menu', 'careers', 'jobs', 'hire', 'wedding', 'gift', 'login', 'cart', 'shop')
_SKIP_EXT = re.compile(r'\.(pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp[34])$', re.IGNORECASE)

THRESHOLD = 3.0


def site_of(url: str) -> str:
    """Registrable domain, e.g. 'venue.co.uk' for https://www.venue.co.uk/x."""
    domain = _EXTRACT(url).top_domain_under_public_suffix
    if domain:
        return domain
    host = (urlparse(url).hostname or '').lower()
    if not host or host.replace('.', '').isdigit() or '.' not in host:
        return host                     # IP address or bare hostname
    return '.'.join(host.split('.')[-2:])


def same_site(url: str, other: str) -> bool:
    return site_of(url) == site_of(other)


def page_links(response) -> list[tuple[str, str, bool]]:
    """(anchor text, absolute URL, in nav/header) for same-site links, first occurrence wins."""
    seen: set[str] = set()
    links = []
    for a in response.xpath('//a[@href]'):
        href = a.attrib.get('href', '').strip()
        if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            continue
        url = response.urljoin(href).split('#')[0]
        if url in seen or not url.startswith('http') or not same_site(url, response.url):
            continue
        seen.add(url)
        text = ' '.join(t.strip() for t in a.xpath('.//text()').getall() if t.strip())
        text = text or a.attrib.get('title', '') or a.attrib.get('aria-label', '')
        in_nav = bool(a.xpath('ancestor::nav or ancestor::header or '
                              'ancestor::*[contains(@class,"nav") or contains(@class,"menu") '
                              'or contains(@id,"nav") or contains(@id,"menu")]'))
        links.append((text, url, in_nav))
    return links


def score_link(text: str, url: str, in_nav: bool = False) -> float:
    path = urlparse(url).path.lower().rstrip('/')
    if _SKIP_EXT.search(path):
        return -10.0
    segments = [s for s in path.split('/') if s]
    score = 0.0
    if any(seg in _STRONG for seg in segments):
        score += 3.0
    elif any(word in path for word in ('event', 'calendar', 'gig', 'show', 'concert')):
        score += 1.5
    if _ANCHOR_RE.search(text or ''):
        score += 3.0
    if in_nav:
        score += 1.0
    words = segments + re.findall(r'[a-z]+', (text or '').lower())
    if any(neg in word for word in words for neg in _NEGATIVE):
        score -= 5.0
    if _ANCHOR_RE.search(text or '') is None and len(segments) > 2:
        score -= 1.0          # deep link without event wording: likely one item, not the listing
    if urlparse(url).query:
        score -= 0.5
    return score


def find_events_pages(response, limit: int = 3) -> list[str]:
    """Best-scoring same-site links that look like event listings."""
    scored = []
    for text, url, in_nav in page_links(response):
        if url.rstrip('/') == response.url.rstrip('/'):
            continue
        s = score_link(text, url, in_nav)
        if s >= THRESHOLD:
            scored.append((s, -len(url), url))
    scored.sort(reverse=True)
    return _drop_nested([url for _, _, url in scored])[:limit]


def events_urls_from_sitemap(locs: list[str], limit: int = 3) -> list[str]:
    """Pick listing-looking URLs from sitemap <loc> entries (no anchor text available)."""
    scored = sorted(((score_link('', u), -len(u), u) for u in locs), reverse=True)
    return _drop_nested([u for s, _, u in scored if s >= THRESHOLD - 1.5])[:limit]


def _drop_nested(urls: list[str]) -> list[str]:
    """/events and /events/some-show → keep /events only (the listing, not an item)."""
    kept: list[str] = []
    for url in urls:
        base = url.rstrip('/')
        if any(base.startswith(k.rstrip('/') + '/') for k in kept):
            continue
        kept = [k for k in kept if not k.rstrip('/').startswith(base + '/')]
        kept.append(url)
    return kept


def home_url(url: str) -> Optional[str]:
    p = urlparse(url)
    return f'{p.scheme}://{p.netloc}/' if p.scheme and p.netloc else None
