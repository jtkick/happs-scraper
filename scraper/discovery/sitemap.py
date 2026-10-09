"""
Reading a site's sitemaps: which pages it has, and when each last changed.

roots(robots_body, base_url) → the sitemaps robots.txt names, else /sitemap.xml
parse(body)                  → ('urlset' | 'sitemapindex' | '', [(loc, lastmod or None)])
pick_children(locs)          → which child sitemaps of an index to fetch (event-looking first)
key(url)                     → the form URLs are compared in (sitemaps and listings spell them differently)
SitemapRead                  → one source's sitemaps being read (the spider fetches, this keeps count)

Read to discover a new site's events pages (events_page.events_urls_from_sitemap)
and, on later crawls, to decide whether a detail page changed (scraper/page_state.py).
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

from scrapy.utils.gz import gunzip
from scrapy.utils.sitemap import Sitemap, sitemap_urls_from_robots
from w3lib.url import canonicalize_url

from scraper.page_state import parse_time

logger = logging.getLogger(__name__)

MAX_ROOTS = 5
MAX_CHILDREN = 20
DISCOVERY_CHILDREN = 5
MAX_URLS = 50_000
MAX_BYTES = 50 * 1024 * 1024        # the sitemap protocol's own limit, uncompressed

_EVENTISH = re.compile(r'event|calendar|show|concert|performance|happening', re.IGNORECASE)


def roots(robots_body: bytes, base_url: str) -> list[str]:
    found = list(dict.fromkeys(sitemap_urls_from_robots(robots_body, base_url=base_url)))
    return found[:MAX_ROOTS] or [urljoin(base_url, '/sitemap.xml')]


def parse(body: bytes) -> tuple[str, list[tuple[str, Optional[datetime]]]]:
    if body[:2] == b'\x1f\x8b':
        try:
            body = gunzip(body, max_size=MAX_BYTES)
        except Exception as exc:  # truncated or oversized archive
            logger.debug("Unreadable gzipped sitemap: %s", exc)
            return '', []
    try:
        sitemap = Sitemap(body)
    except Exception:  # not XML at all (an HTML 404 page, say)
        return '', []
    if sitemap.type not in ('urlset', 'sitemapindex'):
        return '', []
    entries = []
    for item in sitemap:
        loc = (item.get('loc') or '').strip()
        if loc:
            entries.append((loc, parse_time(item['lastmod']) if item.get('lastmod') else None))
        if len(entries) >= MAX_URLS:
            break
    return sitemap.type, entries


def pick_children(locs: list[str], limit: int = MAX_CHILDREN) -> list[str]:
    return sorted(locs, key=lambda u: not _EVENTISH.search(u))[:limit]


def key(url: str) -> str:
    return canonicalize_url(url).rstrip('/')


@dataclass
class SitemapRead:
    """
    One source's sitemaps being read. `purpose` 'discover' keeps every page URL
    listed (locs) to find events pages in; 'lastmod' holds back `waiting`
    listing requests until the dates are in. Both learn which roots exist.
    """
    purpose: str
    max_children: int = MAX_CHILDREN
    waiting: list = field(default_factory=list)
    home_url: str = ''
    home_links: list = field(default_factory=list)
    pending: int = 0
    children: int = 0
    roots: list = field(default_factory=list)
    lastmod: dict = field(default_factory=dict)
    locs: list = field(default_factory=list)

    def add(self, url: str, kind: str, entries: list, *, root: bool = False) -> list[str]:
        """Take in one fetched sitemap; returns the child sitemaps to fetch next."""
        if kind and root:
            self.roots.append(url)
        if kind == 'sitemapindex':
            children = pick_children([loc for loc, _ in entries], limit=max(0, self.max_children - self.children))
            self.children += len(children)
            return children
        for loc, lastmod in entries if kind == 'urlset' else ():
            if self.purpose == 'discover' and len(self.locs) < MAX_URLS:
                self.locs.append(loc)
            if lastmod and len(self.lastmod) < MAX_URLS:
                self.lastmod[key(loc)] = lastmod
        return []
