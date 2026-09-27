"""
Custom Scrapy downloader middlewares.

RotatingUserAgentMiddleware
  Rotates through a pool of realistic browser UA strings so no single UA
  dominates the request log of a target site.

ConditionalFetchMiddleware
  Attaches If-None-Match / If-Modified-Since headers from the dedup DB on
  repeat visits, and short-circuits on 304 Not Modified responses.
"""

from __future__ import annotations
import itertools
import logging
import random

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
    Use HTTP cache-validation headers to avoid re-downloading unchanged pages.

    On outgoing requests: attach If-None-Match / If-Modified-Since if we have
    cached values in the dedup DB.

    On incoming 304 responses: raise IgnoreRequest so the item is skipped
    cleanly (Scrapy will still call process_response, but we drop it here).
    """

    def process_request(self, request, spider):
        # Look up the dedup pipeline's DB through the crawler's extension
        # registry.  It may not be open yet on the very first request.
        dedup = self._get_dedup(spider)
        if dedup is None:
            return

        url = request.url
        row = dedup.conn.execute(
            'SELECT etag, last_modified FROM scraped_urls WHERE url = ?', (url,)
        ).fetchone()

        if row:
            etag, last_modified = row
            if etag:
                request.headers['If-None-Match'] = etag
            if last_modified:
                request.headers['If-Modified-Since'] = last_modified

    def process_response(self, request, response: Response, spider):
        if response.status == 304:
            logger.debug("304 Not Modified — skipping %s", request.url)
            raise IgnoreRequest(f"304 Not Modified: {request.url}")

        # Store ETag / Last-Modified for next run.
        dedup = self._get_dedup(spider)
        if dedup is not None:
            etag = response.headers.get('ETag', b'').decode('utf-8', errors='ignore') or None
            lm   = response.headers.get('Last-Modified', b'').decode('utf-8', errors='ignore') or None
            if etag or lm:
                dedup.mark_url(request.url, etag=etag, last_modified=lm)

        return response

    def process_exception(self, request, exception, spider):
        return None

    @staticmethod
    def _get_dedup(spider):
        """Retrieve the FingerprintDedupPipeline instance if available."""
        try:
            from scraper.pipelines import FingerprintDedupPipeline
            crawler = spider.crawler
            for pipeline in crawler.engine.scraper.itemproc.middlewares:
                if isinstance(pipeline, FingerprintDedupPipeline):
                    return pipeline
        except Exception:
            pass
        return None
