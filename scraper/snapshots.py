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

Curators can also ask for a page (the backend's curation console does when
none is saved since the scraper last read an event's page). The crawler pool
fills those requests with fulfil_requests(), parsing the page as a crawl would.
"""
from __future__ import annotations
import base64
import gzip
import logging
import random
from typing import Optional

from scraper.pipelines import dry_run
from scraper.metakeys import AI_RESPONSE, CONTEXT, PARTIAL, RECIPE, SOURCE_ID
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
        kept, dropped = dry_run([spider.build_item(e, response) for e in events], spider.settings)
        meta = response.meta
        recipe = meta.get(RECIPE) or {}
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
            'source_id': meta.get(SOURCE_ID),
            'url': response.url,
            'captured_at': now_iso(),
            'reason': ','.join(reasons),
            'html_gz': base64.b64encode(gzip.compress(response.body)).decode(),
            'parsed': {'strategy': result.strategy, 'ai_used': result.ai_used,
                       'events': kept, 'dropped': dropped},
            'ai_response': meta.get(AI_RESPONSE),
            'context': {
                'kind': kind,
                'seed_context': meta.get(CONTEXT) or {},
                'recipe': recipe if recipe.get('item_css') else {},
                'partial': meta.get(PARTIAL),
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


def fulfil_requests(client, *, api_key: str = '', limit: int = 5, fetch=None) -> int:
    """
    Capture pages curators asked for: render each, parse it as a crawl would
    (asking the model live when there's a key, and recording its answer), and
    upload it against the request. A page that can't be fetched is given up
    on with the reason. Returns how many were uploaded.
    """
    from scraper.eval.capture import FetchError, fetch as fetch_page, new_case
    fetch = fetch or fetch_page
    done = 0
    for request in client.snapshots(status='requested')[:limit]:
        ctx = request.get('context') or {}
        give_up = lambda why: client.update_snapshot(  # noqa: E731
            request['id'], {'status': 'discarded', 'reason': why[:255]})
        try:
            html = fetch(request['url'])
        except FetchError as exc:
            logger.warning("Requested page %s: %s", request['url'], exc)
            give_up(f'could not fetch it: {exc}')
            continue
        if len(html.encode('utf-8')) > MAX_PAGE_BYTES:
            give_up('the page is too large')
            continue
        try:
            case = new_case(request['url'], html, kind=ctx.get('kind') or 'listing',
                            seed_context=ctx.get('seed_context') or {}, partial=ctx.get('partial'),
                            recipe=ctx.get('recipe') or {}, platform=ctx.get('platform'),
                            ai='live' if api_key else None, api_key=api_key, case_id=request['id'])
        except Exception as exc:
            logger.exception("Parsing requested page %s failed", request['url'])
            give_up(f'the scraper failed on it: {type(exc).__name__}')
            continue
        parsed = {k: case.parsed.get(k) for k in ('strategy', 'ai_used', 'events', 'dropped')}
        response = client.upload_snapshot({
            'request_id': request['id'], 'source_id': request.get('source'), 'url': request['url'],
            'captured_at': case.captured_at, 'reason': request.get('reason') or '',
            'html_gz': base64.b64encode(gzip.compress(html.encode('utf-8'))).decode(),
            'parsed': parsed, 'ai_response': case.ai_response, 'context': ctx,
        })
        if response and response.get('id'):
            done += 1
    if done:
        logger.info("Captured %d requested pages", done)
    return done


def _known_events_page(url: str, recipe: dict) -> bool:
    known = {u.rstrip('/') for u in recipe.get('events_urls') or []}
    return url.rstrip('/') in known
