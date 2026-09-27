"""
Custom Scrapy downloader middlewares.

RotatingUserAgentMiddleware
  Rotates through a pool of realistic browser UA strings so no single UA
  dominates the request log of a target site.

ConditionalFetchMiddleware
  Skips unchanged listing pages (304) and tells the run tracker which
  events those pages still list.
"""

from __future__ import annotations
import itertools
import json
import logging
import random
import sqlite3

from scrapy import signals
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response

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

    Requests marked meta['cacheable'] (listing pages) have their validators
    (ETag / Last-Modified) stored; the run tracker stores which event
    fingerprints each page produced. Requests also marked meta['conditional']
    send the validators — but only when both are on file — and a 304 hands
    the page's remembered fingerprints to the tracker as "still listed".
    Without that, an unchanged page would look like its events vanished.

    Never used for detail pages. Everything lives in a small local SQLite
    file (ETAG_DB_PATH); losing it only costs full downloads.
    """

    def __init__(self, path: str, crawler=None):
        self.crawler = crawler
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            'CREATE TABLE IF NOT EXISTS validators '
            '(url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT)')
        self.conn.execute(
            'CREATE TABLE IF NOT EXISTS page_events (url TEXT PRIMARY KEY, fingerprints TEXT)')
        self.conn.commit()

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls(crawler.settings.get('ETAG_DB_PATH', 'etags.db'), crawler)
        # engine_stopped fires after spider_closed, when the tracker has
        # written each page's fingerprints through remember_events().
        crawler.signals.connect(mw.close, signal=signals.engine_stopped)
        return mw

    def close(self, spider=None):
        self.conn.close()

    def process_request(self, request, spider=None):
        if not request.meta.get('conditional') or self.page_events(request.url) is None:
            return None
        row = self.conn.execute(
            'SELECT etag, last_modified FROM validators WHERE url = ?', (request.url,)).fetchone()
        if row:
            etag, last_modified = row
            if etag:
                request.headers['If-None-Match'] = etag
            if last_modified:
                request.headers['If-Modified-Since'] = last_modified
        return None

    def process_response(self, request, response: Response, spider=None):
        if not request.meta.get('cacheable'):
            return response
        # Scrapy ≥ 2.13 no longer passes `spider` to middleware hooks.
        spider = spider or getattr(self.crawler, 'spider', None)
        tracker = getattr(spider, 'tracker', None)
        if tracker is not None:
            tracker.page_store = self
        if response.status == 304:
            if tracker is not None:
                tracker.not_modified(request, self.page_events(request.url) or [])
            raise IgnoreRequest(f"304 Not Modified: {request.url}")
        etag = response.headers.get('ETag', b'').decode('utf-8', errors='ignore') or None
        lm = response.headers.get('Last-Modified', b'').decode('utf-8', errors='ignore') or None
        if etag or lm:
            self.remember(request.url, etag, lm)
        return response

    def remember(self, url: str, etag: str = None, last_modified: str = None):
        self.conn.execute(
            'INSERT OR REPLACE INTO validators (url, etag, last_modified) VALUES (?, ?, ?)',
            (url, etag, last_modified))
        self.conn.commit()

    def remember_events(self, url: str, fingerprints) -> None:
        self.conn.execute('INSERT OR REPLACE INTO page_events (url, fingerprints) VALUES (?, ?)',
                          (url, json.dumps(sorted(fingerprints))))
        self.conn.commit()

    def page_events(self, url: str):
        row = self.conn.execute('SELECT fingerprints FROM page_events WHERE url = ?', (url,)).fetchone()
        return json.loads(row[0]) if row else None
