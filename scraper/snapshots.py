"""
Save interesting pages from a crawl to the backend for review, with what the
crawl made of them. tools/review.py sync turns them into labelled test cases.

A page is saved when the crawl had to guess or lose something on it:
  ai        the model extracted its events
  review    an event scored below REVIEW_THRESHOLD (held for review)
  dropped   validation dropped an event (for any reason but having passed)
  empty     a known events page (from the recipe) yielded nothing
  sample    none of the above, picked at random (SNAPSHOT_SAMPLE_RATE)

Uploads wait until the spider closes so they never hold up the crawl. The
backend skips pages it already has and caps how many one source can add.
"""
from __future__ import annotations
import base64
import gzip
import logging
import random
from typing import Optional

from scraper.pipelines import dry_run
from scraper.util import now_iso

logger = logging.getLogger(__name__)

MAX_PAGE_BYTES = 4 * 1024 * 1024


class SnapshotSampler:

    def __init__(self, client, *, max_per_run: int = 50, sample_rate: float = 0.01,
                 rng: Optional[random.Random] = None):
        self.client = client
        self.max_per_run = max_per_run
        self.sample_rate = sample_rate
        self.rng = rng or random.Random()
        self.pending: list[dict] = []

    @classmethod
    def from_spider(cls, spider) -> Optional['SnapshotSampler']:
        settings = spider.settings
        if not settings.getbool('SNAPSHOTS_ENABLED') or spider.client is None or spider.adhoc_url:
            return None
        return cls(spider.client, max_per_run=settings.getint('SNAPSHOT_MAX_PER_RUN', 50),
                   sample_rate=settings.getfloat('SNAPSHOT_SAMPLE_RATE', 0.01))

    def consider(self, spider, response, result, events: list[dict], *, kind: str,
                 platform: Optional[str] = None) -> Optional[list[str]]:
        """Queue the page if it's worth reviewing. Returns the reasons, or None."""
        if len(self.pending) >= self.max_per_run or len(response.body) > MAX_PAGE_BYTES:
            return None
        kept, dropped = dry_run([spider.build_item(e, response) for e in events], spider)
        meta = response.meta
        recipe = meta.get('recipe') or {}
        reasons = []
        if result.ai_used:
            reasons.append('ai')
        if any(e.get('review_required') for e in kept):
            reasons.append('review')
        if any(e['drop_reason'] != 'past_event' for e in dropped):
            reasons.append('dropped')
        if kind == 'listing' and not events and _known_events_page(response.url, recipe):
            reasons.append('empty')
        if not reasons and self.rng.random() < self.sample_rate:
            reasons.append('sample')
        if not reasons:
            return None

        self.pending.append({
            'source_id': meta.get('source_id'),
            'url': response.url,
            'captured_at': now_iso(),
            'reason': ','.join(reasons),
            'html_gz': base64.b64encode(gzip.compress(response.body)).decode(),
            'parsed': {'strategy': result.strategy, 'ai_used': result.ai_used,
                       'events': kept, 'dropped': dropped},
            'ai_response': meta.get('ai_response'),
            'context': {
                'kind': kind,
                'seed_context': meta.get('context') or {},
                'recipe': recipe if recipe.get('item_css') else {},
                'partial': meta.get('partial'),
                'platform': platform,
            },
        })
        return reasons

    def flush(self) -> int:
        """Upload what was queued. Returns how many the backend kept."""
        kept = 0
        for snapshot in self.pending:
            response = self.client.upload_snapshot(snapshot)
            if response and response.get('id'):
                kept += 1
        if self.pending:
            logger.info("Snapshots: %d queued, %d new in the backend", len(self.pending), kept)
        self.pending = []
        return kept


def _known_events_page(url: str, recipe: dict) -> bool:
    known = {u.rstrip('/') for u in recipe.get('events_urls') or []}
    return url.rstrip('/') in known
