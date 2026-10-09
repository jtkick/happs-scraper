"""
What earlier crawls knew about a source's pages, and what this crawl learns.

Loaded per source from the backend (api/scraper/sources/<id>/pages/) when
the source's crawl starts, and sent back in its run report (`pages`), so any
worker can pick up where the last one left off.

  listing pages  HTTP validators (etag, last_modified) and the fingerprints listed
  detail pages   the listing entry they were reached from (listing_data, listing_hash),
                 a hash of what they parsed to (content_hash), the fingerprints they
                 produced, when they were fetched, and with which EXTRACTION_VERSION

refetch_reason() decides whether a detail page has to be fetched again;
lastmod_missed_change() whether its sitemap's lastmod can be trusted.
"""
from __future__ import annotations
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from scraper.util import parse_utc

# Fields that say how an event was found rather than what it is.
_HOW_FOUND = frozenset({
    'extraction_method', 'tag_names', 'confidence', 'review_required', 'evidence', 'fingerprint',
    'source_id', 'source_url', 'ingest_status', 'drop_reason',
})
_EMPTY = (None, '', [], {})


def event_view(data: dict) -> dict:
    """An event's own fields: no private keys, bookkeeping or empty values."""
    return {k: v for k, v in data.items()
            if not k.startswith('_') and k not in _HOW_FOUND and v not in _EMPTY}


def stable_hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def listing_hash(data: dict) -> str:
    return stable_hash(event_view(data))


def content_hash(events: Iterable[dict]) -> str:
    return stable_hash(sorted(stable_hash(event_view(e)) for e in events))


def refetch_reason(record: Optional[dict], *, version: int, max_age: timedelta,
                   listing: Optional[str] = None, lastmod: Optional[datetime] = None,
                   now: Optional[datetime] = None) -> Optional[str]:
    """
    Why a detail page must be fetched again, or None when nothing suggests it
    changed. `listing` is this run's listing_hash for it (None when it wasn't
    reached from a listing entry); `lastmod` is the sitemap's date for it, only
    passed when the source's sitemap is trusted.
    """
    if not record or not record.get('fetched_at'):
        return 'new'
    if record.get('extraction_version') != version:
        return 'version'
    fetched = parse_utc(record['fetched_at'])
    now = now or datetime.now(timezone.utc)
    if fetched is None or now - fetched > max_age:
        return 'max_age'
    if listing is not None and record.get('listing_hash') != listing:
        return 'listing'
    if lastmod is not None and lastmod > fetched:
        return 'lastmod'
    if listing is None and lastmod is None:
        return 'no_signal'
    return None


def lastmod_missed_change(record: Optional[dict], *, content_hash: str, lastmod: Optional[datetime],
                          version: int) -> bool:
    """
    The page parses differently now (same EXTRACTION_VERSION, so the page
    itself changed), yet its sitemap lastmod is no later than our last fetch.
    """
    if not record or not lastmod or not record.get('content_hash'):
        return False
    fetched = parse_utc(record['fetched_at']) if record.get('fetched_at') else None
    return bool(fetched and lastmod <= fetched and record.get('extraction_version') == version
                and record['content_hash'] != content_hash)



class PageState:

    def __init__(self, pages: Iterable[dict] = ()):
        self.pages: dict[str, dict] = {p['url']: dict(p) for p in pages if p.get('url')}
        self._changed: dict[str, dict] = {}

    def get(self, url: str) -> Optional[dict]:
        return self.pages.get(url)

    def update(self, url: str, kind: str, **fields) -> None:
        """Change what's known about a page; only what changed goes into the report."""
        fields = {'kind': kind, **fields}
        self.pages.setdefault(url, {'url': url}).update(fields)
        self._changed.setdefault(url, {'url': url}).update(fields)

    def details_of(self, listing_url: str) -> list[dict]:
        return [p for p in self.pages.values()
                if p.get('kind') == 'detail' and p.get('listing_url') == listing_url]

    def to_report(self) -> list[dict]:
        return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in page.items()}
                for page in self._changed.values()]
