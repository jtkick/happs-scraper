"""
Per-source bookkeeping for one crawl, reported to the backend when the
spider closes (POST api/scraper/runs/) along with any recipe it learned.

Reports are sent only at spider close: items finish the pipelines
asynchronously, so an earlier flush could miss fingerprints and make
still-listed events look like they disappeared.
"""
from __future__ import annotations
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Drop codes that aren't losses (the event was captured another way).
_NOT_A_LOSS = {'duplicate_in_run'}
MAX_DROPPED = 50
# A relearned recipe must find this share of the baseline to replace the old one.
RELEARN_ACCEPT = 0.8


@dataclass
class SourceRun:
    source: dict
    key: str = ''
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    relearning: bool = False
    listing_pages: int = 0
    detail_pages: int = 0
    scheduled_listings: int = 0
    scheduled_details: int = 0
    not_modified: int = 0
    seen: set = field(default_factory=set)
    status_counts: Counter = field(default_factory=Counter)
    strategies: Counter = field(default_factory=Counter)
    dropped: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    complete: bool = True
    fetch_failed: bool = False
    robots_blocked: bool = False
    strategy_fallback: bool = False
    learned: dict = field(default_factory=dict)
    learned_origin: str = ''
    # listing page URL → fingerprints of the events it listed this run
    page_fps: dict = field(default_factory=dict)

    @property
    def source_id(self) -> Optional[str]:
        return self.source.get('id')

    @property
    def recipe(self) -> dict:
        return self.source.get('effective_recipe') or {}

    def report(self) -> dict:
        strategy = self.strategies.most_common(1)[0][0] if self.strategies else ''
        return {
            'source_id':          self.source_id,
            'started_at':         self.started_at,
            'recipe_version':     self.source.get('recipe_version', 0),
            'strategy_used':      strategy,
            'complete':           self.complete,
            'pages_fetched':      self.listing_pages + self.detail_pages,
            'not_modified':       self.not_modified,
            'events_found':       len(self.seen),
            'created':            self.status_counts['created'],
            'updated':            self.status_counts['updated'],
            'unchanged':          self.status_counts['unchanged'],
            'dropped':            self.dropped,
            'errors':             self.errors[:20],
            'seen_fingerprints':  sorted(self.seen),
            'fetch_failed':       self.fetch_failed,
            'robots_blocked':     self.robots_blocked,
            'strategy_fallback':  self.strategy_fallback,
        }


class RunTracker:

    def __init__(self, client=None):
        self.client = client
        self.runs: dict[str, SourceRun] = {}
        self.page_store = None      # ConditionalFetchMiddleware, once it sees a listing page

    def start(self, source: dict, relearning: bool = False) -> SourceRun:
        key = source.get('id') or source.get('domain') or source['homepage_url']
        run = SourceRun(source=source, key=key, relearning=relearning)
        self.runs[key] = run
        return run

    def for_request(self, request) -> Optional[SourceRun]:
        return self.runs.get(request.meta.get('run_key'))

    # ── Signals ───────────────────────────────────────────────────────────────

    def not_modified(self, request, carried=()):
        """A listing page was unchanged: its previously seen events are still listed."""
        run = self.for_request(request)
        if run:
            run.not_modified += 1
            run.seen.update(carried)
            run.page_fps[request.url] = set(carried)

    def page_fetched(self, request, url: str):
        run = self.for_request(request)
        if run:
            run.page_fps.setdefault(url, set())

    def item_scraped(self, item, response=None, spider=None):
        run = self._run_for_item(item, response)
        if run is None:
            return
        run.seen.add(item['fingerprint'])
        run.status_counts[item.get('ingest_status') or 'unknown'] += 1
        meta = getattr(response, 'meta', None) or {}
        page = meta.get('listing_url') or (response.url if meta.get('stage') == 'listing' else None)
        if page:
            run.page_fps.setdefault(page, set()).add(item['fingerprint'])

    def item_dropped(self, item, response=None, exception=None, spider=None):
        run = self._run_for_item(item, response)
        if run is None:
            return
        reason = str(exception or '').split(':')[0].strip() or 'unknown'
        if reason in _NOT_A_LOSS:
            return
        if len(run.dropped) < MAX_DROPPED:
            run.dropped.append({'title': (item.get('title') or '')[:120], 'reason': reason})

    def error(self, request, failure, stage: str):
        run = self.for_request(request)
        if run is None:
            return
        message = f'{stage}: {request.url}: {failure.value}'[:300]
        run.errors.append(message)
        if stage in ('home', 'listing') and not run.listing_pages:
            if 'robots.txt' in str(failure.value):
                run.robots_blocked = True
            else:
                run.fetch_failed = True
        else:
            # A lost detail page may hide a still-listed event.
            run.complete = False

    # ── Learning ──────────────────────────────────────────────────────────────

    def learn(self, run: SourceRun, origin: str, **recipe_updates):
        run.learned.update({k: v for k, v in recipe_updates.items() if v is not None})
        run.learned_origin = run.learned_origin or origin

    # ── Flush ─────────────────────────────────────────────────────────────────

    def flush(self):
        if self.page_store is not None:
            for run in self.runs.values():
                for page, fps in run.page_fps.items():
                    self.page_store.remember_events(page, fps)
        for run in self.runs.values():
            if self.client is None or not run.source_id:
                logger.info("Ad-hoc run %s: %d events, learned %s", run.key, len(run.seen), run.learned)
                continue
            self._save_recipe(run)
            result = self.client.report_run(run.report())
            if result:
                logger.info("Run %s: %s (%d events, source %s)", run.source.get('domain'),
                            result.get('outcome'), len(run.seen), result.get('source_status'))

    def _save_recipe(self, run: SourceRun):
        if run.source.get('recipe_locked'):
            return
        update: dict = {}
        baseline = run.source.get('baseline_events') or 0
        if run.relearning and baseline and len(run.seen) < RELEARN_ACCEPT * baseline:
            # The new recipe does worse than the old one did; keep the old
            # one and let the run's degraded/failed outcome flag the source.
            logger.warning("Relearned recipe for %s found %d events (baseline %d) — not saved",
                           run.source.get('domain'), len(run.seen), baseline)
            run.learned = {}
        if run.learned:
            base = {} if run.relearning else dict(run.source.get('recipe') or {})
            recipe = {**base, **run.learned}
            if recipe != run.source.get('recipe'):
                update['recipe'] = recipe
                update['recipe_origin'] = run.learned_origin or 'heuristic'
        if run.relearning:
            update['relearn_requested'] = False
        if update:
            self.client.update_source(run.source_id, update)

    def _run_for_item(self, item, response) -> Optional[SourceRun]:
        if response is not None and response.meta.get('run_key') in self.runs:
            return self.runs[response.meta['run_key']]
        sid = item.get('source_id')
        return self.runs.get(sid) if sid else None
