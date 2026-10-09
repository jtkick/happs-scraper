"""
Custom Scrapy downloader middlewares.

RotatingUserAgentMiddleware
  Rotates through a pool of realistic browser UA strings so no single UA
  dominates the request log of a target site.

ConditionalFetchMiddleware
  Skips unchanged listing pages (304) and tells the run tracker which
  events those pages still list. Validators live in the run's page state.
"""

from __future__ import annotations
import itertools
import logging
import random

from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response

from scraper.metakeys import CACHEABLE, CONDITIONAL, RENDER_IF_CHANGED
logger = logging.getLogger(__name__)

# A realistic cross-browser pool.  Extend as needed.
_USER_AGENTS = [
    # Chrome / Windows
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
     'AppleWebKit/537.36 (KHTML, like Gecko) '
     'Chrome/124.0.0.0 Safari/537.36'),
    # Chrome / macOS
    ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
     'AppleWebKit/537.36 (KHTML, like Gecko) '
     'Chrome/124.0.0.0 Safari/537.36'),
    # Firefox / Windows
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) '
     'Gecko/20100101 Firefox/125.0'),
    # Safari / macOS
    ('Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) '
     'AppleWebKit/605.1.15 (KHTML, like Gecko) '
     'Version/17.4.1 Safari/605.1.15'),
    # Edge / Windows
    ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
     'AppleWebKit/537.36 (KHTML, like Gecko) '
     'Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0'),
]


class NotModified(IgnoreRequest):
    """A listing page answered 304; its events were reported as still listed."""


class RotatingUserAgentMiddleware:
    """Assign a random user-agent from the pool to each outgoing request."""

    def __init__(self):
        # Shuffle once so the same index isn't always chosen first.
        pool = list(_USER_AGENTS)
        random.shuffle(pool)
        self._cycle = itertools.cycle(pool)

    def process_request(self, request, spider):
        request.headers['User-Agent'] = next(self._cycle)


class ConditionalFetchMiddleware:
    """
    Skip unchanged listing pages without losing track of their events.

    Requests marked meta[CACHEABLE] (listing pages) have their validators
    (ETag / Last-Modified) stored in the run's page state (scraper/page_state.py,
    kept by the backend between runs); the run tracker stores which event
    fingerprints each page produced. Requests also marked meta[CONDITIONAL]
    send the validators — but only when both are on file — and a 304 hands
    the page's remembered fingerprints to the tracker as "still listed".
    Without that, an unchanged page would look like its events vanished.

    Never used for detail pages (scraper/page_state.py decides those).

    Chromium aborts a navigation answered 304, so a rendered request with
    validators on file is sent raw instead, marked meta[RENDER_IF_CHANGED];
    the spider renders the page only when it comes back changed.
    """

    def __init__(self, crawler=None):
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_request(self, request, spider=None):
        if not request.meta.get(CONDITIONAL):
            return None
        page = self._page(request, spider)
        if not page or 'fingerprints' not in page or not (page.get('etag') or page.get('last_modified')):
            return None
        if request.meta.get('playwright'):
            meta = {k: v for k, v in request.meta.items() if not k.startswith('playwright')}
            return request.replace(meta={**meta, RENDER_IF_CHANGED: True}, dont_filter=True)
        if page.get('etag'):
            request.headers['If-None-Match'] = page['etag']
        if page.get('last_modified'):
            request.headers['If-Modified-Since'] = page['last_modified']
        return None

    def process_response(self, request, response: Response, spider=None):
        if not request.meta.get(CACHEABLE):
            return response
        tracker = self._tracker(spider)
        run = tracker.for_request(request) if tracker is not None else None
        if response.status == 304:
            if tracker is not None:
                page = run.pages.get(request.url) if run else None
                tracker.not_modified(request, (page or {}).get('fingerprints') or [])
            raise NotModified(f"304 Not Modified: {request.url}")
        etag = response.headers.get('ETag', b'').decode('utf-8', errors='ignore')
        lm = response.headers.get('Last-Modified', b'').decode('utf-8', errors='ignore')
        if run is not None and (etag or lm or run.pages.get(request.url)):
            run.pages.update(request.url, 'listing', etag=etag, last_modified=lm)
        return response

    def _tracker(self, spider):
        # Scrapy ≥ 2.13 no longer passes `spider` to middleware hooks.
        spider = spider or getattr(self.crawler, 'spider', None)
        return getattr(spider, 'tracker', None)

    def _page(self, request, spider):
        tracker = self._tracker(spider)
        run = tracker.for_request(request) if tracker is not None else None
        return run.pages.get(request.url) if run else None
