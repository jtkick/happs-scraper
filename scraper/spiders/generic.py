"""
GenericEventSpider — one spider for every venue website. A site that needs
hand-tuning gets a locked recipe override on its backend Source (events_urls,
item_css, detail_link_css, pagination_css), not a spider of its own.

    scrapy crawl generic -a limit=200          # crawl sources the backend says are due
    scrapy crawl generic -a source=venue.com   # one registered source (debugging)
    scrapy crawl generic -a url=https://…      # ad hoc, nothing reported to the backend
    … -a relearn=1                             # ignore the saved recipe; rediscover
    … -a keep_claiming=1 -a batch=10 -a budget_minutes=30
                                               # keep claiming due sources until none are
                                               # left or the budget is spent (tools/worker.py)

Per source:
  1. Saved recipe? → go straight to its platform feed or events pages.
     Otherwise discover: platform detection → scored homepage links →
     the sitemaps (robots.txt, indexes) → Claude picks from the homepage links → the homepage itself.
  2. Listing pages: extract every event (scraper/extraction.py). Follow an
     event's detail page when the listing lacks its date or description
     ("required"), or when the page probably says more ("soft": a cut-off
     description, no address, no end, a multi-day span with no schedule),
     carrying the listing data along as meta[PARTIAL]. Soft follows leave
     GENERIC_DETAIL_RESERVE of the budget for required ones, and the recipe
     learns whether they help (`detail_useful`). Paginate.
     With PLAYWRIGHT_ENABLED every HTML page is rendered (scraper/rendering.py).
     A detail page nothing suggests has changed is skipped and its events
     reported as still listed (scraper/page_state.py): same listing entry,
     sitemap lastmod no newer (fetched first, raw), parsed within
     PAGE_MAX_AGE_DAYS by this EXTRACTION_VERSION. A 304 listing still
     refreshes its stale detail pages.
  3. Anything learned (events URLs, platform, selectors) and a run report go
     back to the backend when the spider closes, or with keep_claiming each
     time a batch is done (scraper/sources/tracker.py).
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from functools import cached_property
from typing import Optional
from urllib.parse import urljoin, urlparse

import scrapy
from scrapy import signals
from scrapy.exceptions import DontCloseSpider, IgnoreRequest
from scrapy.spidermiddlewares.httperror import HttpError

from scraper import extraction, follow, page_state, platforms, settings as defaults
from scraper.discovery import links as link_classifier, sitemap
from scraper.discovery.events_page import (
    events_urls_from_sitemap, find_events_pages, page_links, same_site, site_of,
)
from scraper.extractors import recipe as recipe_extractor
from scraper.items import EventItem, event_item
from scraper.metakeys import (
    AI_RESPONSE, CACHEABLE, CARRIED, CONDITIONAL, CONTEXT, DETAIL_REASON, LISTING_URL, PAGE_URL, PARTIAL,
    PLATFORM, RECIPE, REFETCH, RENDER_FAILED, ROBOTS, ROOT, RUN_KEY, SOURCE_ID, STAGE,
)
from scraper.middlewares import NotModified
from scraper.platforms.base import Platform
from scraper.rendering import raw_retry, render_meta, rendered_after_check
from scraper.snapshots import SnapshotSampler
from scraper.sources.client import BackendClient
from scraper.sources.tracker import RunTracker, SourceRun
from scraper.util import now_iso

logger = logging.getLogger(__name__)

# Stages whose pages are HTML, and so are rendered when Playwright is on.
_RENDERED_STAGES = ('home', 'listing', 'detail')


@dataclass(frozen=True)
class CrawlConfig:
    """The settings the spider reads, read once; defaults are scraper/settings.py's."""
    max_listing_pages: int = defaults.GENERIC_MAX_LISTING_PAGES
    max_detail_pages: int = defaults.GENERIC_MAX_DETAIL_PAGES
    detail_reserve: int = defaults.GENERIC_DETAIL_RESERVE
    page_max_age: timedelta = timedelta(days=defaults.PAGE_MAX_AGE_DAYS)
    api_key: str = ''
    render: bool = False

    @classmethod
    def from_settings(cls, settings) -> 'CrawlConfig':
        return cls(
            max_listing_pages=settings.getint('GENERIC_MAX_LISTING_PAGES', cls.max_listing_pages),
            max_detail_pages=settings.getint('GENERIC_MAX_DETAIL_PAGES', cls.max_detail_pages),
            detail_reserve=settings.getint('GENERIC_DETAIL_RESERVE', cls.detail_reserve),
            page_max_age=timedelta(days=settings.getfloat('PAGE_MAX_AGE_DAYS',
                                                          cls.page_max_age / timedelta(days=1))),
            api_key=settings.get('ANTHROPIC_API_KEY') or '',
            render=settings.getbool('PLAYWRIGHT_ENABLED'),
        )


class GenericEventSpider(scrapy.Spider):
    name = 'generic'

    def __init__(self, limit=50, source=None, url=None, relearn=False, keep_claiming=False,
                 batch=10, budget_minutes=30, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limit = int(limit)
        self.source_domain = source
        self.adhoc_url = url
        self.force_relearn = str(relearn).lower() in ('1', 'true', 'yes')
        self.keep_claiming = str(keep_claiming).lower() in ('1', 'true', 'yes')
        self.batch = int(batch)
        self.budget_seconds = float(budget_minutes) * 60
        self.started = time.monotonic()
        self.tracker = RunTracker()
        self.client: Optional[BackendClient] = None
        self.snapshots: Optional[SnapshotSampler] = None

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.client = BackendClient.from_settings(crawler.settings)
        if not spider.adhoc_url:
            spider.tracker.client = spider.client
        spider.snapshots = SnapshotSampler.from_spider(spider)
        crawler.signals.connect(spider.tracker.item_scraped, signal=signals.item_scraped)
        crawler.signals.connect(spider.tracker.item_dropped, signal=signals.item_dropped)
        crawler.signals.connect(spider._on_closed, signal=signals.spider_closed)
        crawler.signals.connect(spider._on_idle, signal=signals.spider_idle)
        return spider

    @cached_property
    def config(self) -> CrawlConfig:
        return CrawlConfig.from_settings(self.settings)

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def start(self):
        for source in self._sources():
            for request in self.entry_requests(source):
                yield request

    def _sources(self) -> list[dict]:
        if self.adhoc_url:
            return [{'id': None, 'domain': site_of(self.adhoc_url), 'homepage_url': self.adhoc_url,
                     'status': 'new', 'effective_recipe': {}, 'recipe': {}, 'context': {}}]
        if self.source_domain:
            found = self.client.source_by_domain(self.source_domain)
            if not found:
                logger.error("No source registered for %s", self.source_domain)
            return [found] if found else []
        sources = self.client.due_sources(self.batch if self.keep_claiming else self.limit)
        logger.info("Crawling %d due sources", len(sources))
        return sources

    def _on_idle(self, spider=None):
        """With keep_claiming, claim the next batch whenever the current one is done."""
        if not self.keep_claiming or self.client is None or self.adhoc_url or self.source_domain:
            return
        self._flush()
        if time.monotonic() - self.started >= self.budget_seconds:
            logger.info("Time budget spent; finishing")
            return
        sources = self.client.due_sources(self.batch)
        if not sources:
            logger.info("No more due sources; finishing")
            return
        logger.info("Claimed %d more due sources", len(sources))
        for source in sources:
            for request in self.entry_requests(source):
                self.crawler.engine.crawl(request)
        raise DontCloseSpider

    def entry_requests(self, source: dict):
        relearn = bool(self.force_relearn or source.get('relearn_requested')) \
            and not source.get('recipe_locked')
        recipe = dict(source.get('override') or {}) if relearn else dict(source.get('effective_recipe') or {})
        # A relearn crawls everything afresh; otherwise unchanged pages may be skipped.
        known = self.client.pages(source['id']) if self.client and source.get('id') and not relearn else []
        run = self.tracker.start(source, relearning=relearn, pages=known)
        meta = {
            RUN_KEY: run.key,
            SOURCE_ID: source.get('id'),
            CONTEXT: source.get('context') or {},
            RECIPE: recipe,
        }
        # Only a healthy, known site may skip unchanged pages.
        conditional = not relearn and source.get('status') == 'active'

        adapter = platforms.get(recipe.get('platform'))
        if adapter and recipe.get('platform_urls'):
            for url in recipe['platform_urls']:
                yield self._platform_request(url, adapter, meta, run)
        elif recipe.get('events_urls'):
            listings = [self._listing_request(url, meta, run, conditional=conditional)
                        for url in recipe['events_urls']]
            yield from self._after_lastmod(listings, meta, run, source)
        elif self.adhoc_url and urlparse(self.adhoc_url).path.strip('/'):
            yield self._listing_request(self.adhoc_url, meta, run)
        else:
            yield self._request(source['homepage_url'], self.parse_home, meta, 'home', run)

    # ── Discovery ─────────────────────────────────────────────────────────────

    async def parse_home(self, response):
        run = self._run(response)
        run.listing_pages += 1
        meta = self._carry(response)

        taken = self._maybe_platform(response, meta, run)
        if taken is not None:
            for request in taken:
                yield request
            return

        urls = find_events_pages(response)
        if urls:
            for request in self._follow_listings(urls, meta, run, origin='heuristic'):
                yield request
            return

        run.sitemaps = sitemap.SitemapRead(
            purpose='discover', max_children=sitemap.DISCOVERY_CHILDREN, home_url=response.url,
            home_links=[(text, url) for text, url, _ in page_links(response)])
        requests = self._sitemap_requests(run, meta, response.url)
        for request in requests:
            yield request
        if not requests:                # the recipe knows the site has none
            async for request in self._sitemaps_read(run, meta):
                yield request

    async def _discovered(self, read: sitemap.SitemapRead, meta, run: SourceRun):
        """The sitemaps are in: events pages among their URLs, else Claude's pick, else the homepage."""
        urls = [u for u in events_urls_from_sitemap(read.locs) if same_site(u, read.home_url)]
        origin = 'heuristic'
        if not urls and self.config.api_key and read.home_links:
            from scraper.extractors import ai
            urls = await asyncio.to_thread(ai.pick_events_links, read.home_links, read.home_url,
                                           self.config.api_key)
            origin = 'ai'
        if urls:
            requests = self._follow_listings(urls, meta, run, origin=origin)
        else:
            # Many small sites list events on the homepage itself.
            requests = self._follow_listings([read.home_url], meta, run, origin='heuristic', dont_filter=True)
        for request in requests:
            yield request

    def _follow_listings(self, urls, meta, run, origin, dont_filter=False):
        self.tracker.learn(run, origin, events_urls=list(urls))
        for url in urls:
            yield self._listing_request(url, meta, run, dont_filter=dont_filter)

    # ── Sitemaps ──────────────────────────────────────────────────────────────

    def _after_lastmod(self, requests, meta, run, source):
        """
        Read the source's sitemaps for when each page last changed, then send
        `requests`, so skip decisions can use them. Straight away when there
        are no stored detail pages to skip, no sitemap, or it lied before.
        """
        recipe = meta[RECIPE]
        wanted = (any(p.get('kind') == 'detail' for p in run.pages.pages.values())
                  and recipe.get('lastmod_trusted') is not False and recipe.get('sitemap_urls') != [])
        if not wanted:
            yield from requests
            return
        run.sitemaps = sitemap.SitemapRead(purpose='lastmod', waiting=list(requests))
        yield from self._sitemap_requests(run, meta, source['homepage_url'])

    def _sitemap_requests(self, run, meta, base_url) -> list:
        """
        Requests for run.sitemaps, fetched raw: the recipe's sitemap_urls, else
        those robots.txt names, else /sitemap.xml. _sitemaps_read carries on
        once the last is in. Empty when the recipe knows there are none.
        """
        roots = meta[RECIPE].get('sitemap_urls')
        if roots is None:
            return [self._sitemap_request(urljoin(base_url, '/robots.txt'), meta, run,
                                          self.parse_robots_sitemaps, {ROBOTS: True})]
        return [self._sitemap_request(url, meta, run, self.parse_sitemap, {ROOT: True})
                for url in roots[:sitemap.MAX_ROOTS]]

    async def parse_robots_sitemaps(self, response):
        run, meta = self._run(response), self._carry(response)
        for url in sitemap.roots(response.body, response.url):
            yield self._sitemap_request(url, meta, run, self.parse_sitemap, {ROOT: True})
        async for request in self._sitemap_done(run, meta):
            yield request

    async def parse_sitemap(self, response):
        run, meta = self._run(response), self._carry(response)
        kind, entries = sitemap.parse(response.body)
        for url in run.sitemaps.add(response.request.url, kind, entries, root=response.meta.get(ROOT)):
            yield self._sitemap_request(url, meta, run, self.parse_sitemap)
        async for request in self._sitemap_done(run, meta):
            yield request

    async def _sitemap_failed(self, failure):
        request = failure.request
        run, meta = self.tracker.for_request(request), self._carry(request)
        if request.meta.get(ROBOTS):
            yield self._sitemap_request(urljoin(request.url, '/sitemap.xml'), meta, run,
                                        self.parse_sitemap, {ROOT: True})
        async for later in self._sitemap_done(run, meta):
            yield later

    def _sitemap_request(self, url, meta, run, callback, extra=None):
        run.sitemaps.pending += 1
        return scrapy.Request(url, callback=callback, errback=self._sitemap_failed, dont_filter=True,
                              meta={**meta, STAGE: 'sitemap', **(extra or {})})

    async def _sitemap_done(self, run, meta):
        run.sitemaps.pending -= 1
        if not run.sitemaps.pending:
            async for request in self._sitemaps_read(run, meta):
                yield request

    async def _sitemaps_read(self, run, meta):
        read, run.sitemaps = run.sitemaps, None
        if meta[RECIPE].get('sitemap_urls') is None:
            self.tracker.learn(run, 'heuristic', sitemap_urls=list(dict.fromkeys(read.roots)))
        run.lastmod = read.lastmod
        if read.purpose == 'discover':
            async for request in self._discovered(read, meta, run):
                yield request
        else:
            for request in read.waiting:
                yield request

    # ── Listings ──────────────────────────────────────────────────────────────

    async def parse_listing(self, response):
        rendered = rendered_after_check(response.request, self.settings)
        if rendered is not None:
            yield rendered          # changed since last run: now fetch it as the crawl sees it
            return
        run = self._run(response)
        run.listing_pages += 1
        self.tracker.page_fetched(response.request, response.url)
        meta = self._carry(response)
        recipe = meta[RECIPE]

        if not recipe.get('platform'):
            taken = self._maybe_platform(response, meta, run)
            if taken is not None:
                for request in taken:
                    yield request
                return

        result, events = await extraction.parse_page_async(
            response, kind='listing', recipe=recipe, context=meta[CONTEXT], ai=self._ai_extractor(response))
        run.strategies[result.strategy] += len(result.events)
        if recipe.get('item_css') and result.strategy == 'ai':
            run.strategy_fallback = True
        if result.ai_truncated:
            run.complete = False
        if result.strategy == 'ai' and len(result.events) >= 2 and not recipe.get('item_css'):
            learned = await asyncio.to_thread(recipe_extractor.learn, response,
                                              [d for d, _ in result.events], self.config.api_key)
            if learned:
                self.tracker.learn(run, 'ai', **learned)

        found = link_classifier.classify(response, detail_css=recipe.get('detail_link_css'),
                                         pagination_css=recipe.get('pagination_css'))
        self.page_parsed(response, result, events, kind='listing')

        for output in self._emit_listing_events(events, response, meta, run):
            yield output

        if not events:
            for url in found.details:
                refetch = self._refetch_reason(url, run, response.url)
                if refetch is None:
                    continue
                if self._detail_budget(run) <= 0:
                    run.complete = False
                    break
                yield self._detail_request(url, meta, run, response.url, refetch=refetch)

        if found.next_page:
            if run.scheduled_listings < self.config.max_listing_pages:
                yield self._listing_request(found.next_page, meta, run)
            else:
                run.complete = False

    def _emit_listing_events(self, events, response, meta, run):
        for data in events:
            detail_url = follow.detail_url(data, response.url)
            refetch = None
            if detail_url:
                refetch = self._refetch_reason(detail_url, run, response.url,
                                               listing=page_state.listing_hash(data))
                if refetch is None:
                    continue
            reason = follow.detail_reason(data) if detail_url else None
            if reason and follow.may_follow(
                    reason, budget=self._detail_budget(run), detail_useful=meta[RECIPE].get('detail_useful'),
                    reserve=self.config.detail_reserve, soft_scheduled=run.soft_scheduled):
                run.soft_scheduled += reason == 'soft'
                yield self._detail_request(detail_url, meta, run, response.url, partial=data,
                                           detail_reason=reason, refetch=refetch)
                continue
            yield self.build_item(data, response)

    def _refetch_reason(self, url, run, listing_url, listing=None) -> Optional[str]:
        """
        Why the detail page at `url` must be fetched (scraper/page_state.py), or
        None when it's unchanged — its events are then carried as still listed.
        """
        record = run.pages.get(url)
        reason = page_state.refetch_reason(
            record, version=extraction.EXTRACTION_VERSION, listing=listing,
            lastmod=run.lastmod.get(sitemap.key(url)),
            max_age=self.config.page_max_age)
        if record:
            run.pages.update(url, 'detail', last_listed_at=now_iso())
        if reason is None:
            self.tracker.carry(run, record.get('fingerprints') or [], listing_url)
        return reason

    def _refresh_stale_details(self, request):
        """A listing came back 304: its events were carried, but its detail pages may still be stale."""
        run = self.tracker.for_request(request)
        if run is None:
            return
        meta = self._carry(request)
        for record in run.pages.details_of(request.url):
            refetch = self._refetch_reason(record['url'], run, request.url,
                                           listing=record.get('listing_hash') or '')
            if refetch is None:
                continue
            if self._detail_budget(run) <= 0:
                run.complete = False
                return
            yield self._detail_request(record['url'], meta, run, request.url,
                                       partial=record.get('listing_data'), refetch=refetch)

    def _detail_budget(self, run: SourceRun) -> int:
        return max(0, self.config.max_detail_pages - run.scheduled_details)

    # ── Platforms ─────────────────────────────────────────────────────────────

    def _maybe_platform(self, response, meta, run) -> Optional[list]:
        """The requests to make when a platform takes this page over, else None."""
        adapter = platforms.detect(response)
        if adapter is None:
            return None
        if adapter.rerender_only:
            if response.meta.get('playwright') or response.meta.get(RENDER_FAILED):
                return None             # already rendered (or tried) — carry on normally
            if not self.config.render:
                message = f'needs_js ({adapter.name}): {response.url}'
                if message not in run.errors:
                    run.errors.append(message)
                return None
            logger.info("%s: %s page, re-requesting rendered", response.url, adapter.name)
            self.tracker.learn(run, 'heuristic', render_js=True)
            meta[RECIPE] = {**meta[RECIPE], 'render_js': True}
            return [response.request.replace(
                meta={**meta, STAGE: response.meta.get(STAGE), 'playwright': True}, dont_filter=True)]
        urls = adapter.feed_urls(response)
        if not urls:
            return None
        logger.info("%s: detected platform %s", response.url, adapter.name)
        self.tracker.learn(run, 'platform', platform=adapter.name, platform_urls=urls)
        meta = {**meta, RECIPE: {**meta[RECIPE], 'platform': adapter.name}}
        return [self._platform_request(url, adapter, meta, run) for url in urls]

    def _platform_request(self, url, adapter, meta, run):
        # Feeds (iCal, JSON) are fetched raw; an adapter that only locates an HTML page is rendered.
        html_page = adapter.render_js or type(adapter).parse is Platform.parse
        return self._request(url, self.parse_platform, {**meta, PLATFORM: adapter.name},
                             'listing', run, render=html_page)

    async def parse_platform(self, response):
        run = self._run(response)
        adapter = platforms.get(response.meta[PLATFORM])
        meta = self._carry(response)
        finals = extraction.parse_feed(adapter, response, context=meta[CONTEXT])
        if finals is None:           # adapter only locates the page; extract it generically
            async for output in self.parse_listing(response):
                yield output
            return
        run.listing_pages += 1
        run.strategies[f'platform:{adapter.name}'] += len(finals)
        if self.snapshots:
            result = extraction.PageResult(single=False, strategy=f'platform:{adapter.name}')
            self.snapshots.consider(self, response, result, finals, kind='listing', platform=adapter.name)
        for output in self._emit_listing_events(finals, response, meta, run):
            yield output
        next_url = adapter.next_url(response)
        if next_url and run.scheduled_listings < self.config.max_listing_pages:
            yield self._platform_request(next_url, adapter, meta, run)
        elif next_url:
            run.complete = False

    # ── Detail pages ──────────────────────────────────────────────────────────

    async def parse_event(self, response):
        """
        A detail page. meta[PARTIAL] is this same event as its listing showed
        it; the page wins, the partial fills its gaps (scraper/extraction.py).
        """
        result, events = await extraction.parse_page_async(
            response, kind='detail', context=response.meta.get(CONTEXT, {}),
            partial=response.meta.get(PARTIAL), ai=self._ai_extractor(response))
        run = self._run(response)
        if run:
            run.detail_pages += 1
            run.strategies[result.strategy or 'partial'] += max(1, len(result.events))
        self.page_parsed(response, result, events, kind='detail')
        for final in events:
            yield self.build_item(final, response)

    def page_parsed(self, response, result, events, kind):
        if kind == 'detail':
            self._record_detail(response, events)
        if kind == 'detail' and response.meta.get(DETAIL_REASON) == 'soft':
            partial = response.meta.get(PARTIAL) or {}
            self.tracker.soft_detail_parsed(
                response.request, any(extraction.added_info(partial, e) for e in events))
        if self.snapshots:
            self.snapshots.consider(self, response, result, events, kind=kind)

    def _record_detail(self, response, events):
        """Remember what this detail page parsed to; its fingerprints are added at flush."""
        url, run = response.meta.get(PAGE_URL), self._run(response)
        if not url or run is None:
            return
        partial = response.meta.get(PARTIAL)
        new_hash = page_state.content_hash(events)
        if page_state.lastmod_missed_change(run.pages.get(url), content_hash=new_hash,
                                            lastmod=run.lastmod.get(sitemap.key(url)),
                                            version=extraction.EXTRACTION_VERSION):
            logger.info("%s changed but its sitemap lastmod didn't; not trusting lastmod", url)
            self.tracker.learn(run, 'heuristic', lastmod_trusted=False)
        now = now_iso()
        run.pages.update(
            url, 'detail', listing_url=response.meta.get(LISTING_URL) or '',
            listing_data={k: v for k, v in partial.items() if not k.startswith('_')} if partial else None,
            listing_hash=page_state.listing_hash(partial) if partial else '',
            content_hash=new_hash, fingerprints=[], extraction_version=extraction.EXTRACTION_VERSION,
            fetched_at=now, last_listed_at=now)

    def build_item(self, data: dict, response) -> EventItem:
        return event_item(data, source_url=response.url, source_id=response.meta.get(SOURCE_ID))

    def _ai_extractor(self, response):
        """The AI callable for extract_page, or None when AI is disabled."""
        api_key = self.config.api_key
        if not api_key:
            return None
        from scraper.extractors import ai
        # The answer is kept on the response so a page snapshot can carry it.
        record = lambda answer: response.meta.__setitem__(AI_RESPONSE, answer)  # noqa: E731
        return ai.extractor(api_key, venue=(response.meta.get(CONTEXT) or {}).get('location_title'),
                            respond=ai.recorder(api_key, record))

    # ── Requests & bookkeeping ────────────────────────────────────────────────

    def _request(self, url, callback, meta, stage, run, dont_filter=False, render=None, extra=None):
        meta = {**meta, STAGE: stage, **(extra or {})}
        if render is None:
            render = stage in _RENDERED_STAGES or meta.get(RECIPE, {}).get('render_js')
        if render:
            meta.update(render_meta(self.settings))
        if stage == 'listing':
            run.scheduled_listings += 1
        elif stage == 'detail':
            run.scheduled_details += 1
        return scrapy.Request(url, callback=callback, errback=self._errback, meta=meta,
                              dont_filter=dont_filter)

    def _listing_request(self, url, meta, run, conditional=False, dont_filter=False):
        return self._request(url, self.parse_listing, meta, 'listing', run, dont_filter=dont_filter,
                             extra={CONDITIONAL: conditional, CACHEABLE: True})

    def _detail_request(self, url, meta, run, listing_url, partial=None, detail_reason=None,
                        refetch=None):
        return self._request(url, self.parse_event, meta, 'detail', run, extra={
            PARTIAL: partial, LISTING_URL: listing_url, DETAIL_REASON: detail_reason,
            PAGE_URL: url, REFETCH: refetch})

    def _errback(self, failure):
        request = failure.request
        if failure.check(NotModified):
            yield from self._refresh_stale_details(request)
            return
        retry = None if failure.check(HttpError, IgnoreRequest) else raw_retry(request)
        if retry is not None:
            run = self.tracker.for_request(request)
            if run:
                run.errors.append(f'render_failed: {request.url}: {failure.value}'[:300])
            logger.warning("Render failed: %s — %s; fetching it raw", request.url, failure.value)
            yield retry
            return
        stage = request.meta.get(STAGE, 'detail')
        self.tracker.error(request, failure, stage)
        logger.warning("%s request failed: %s — %s", stage, request.url, failure.value)
        partial = request.meta.get(PARTIAL)
        if stage == 'detail' and partial:
            # The listing already had this event; don't lose it with its page.
            item = self.build_item(partial, request)
            yield item

    def _carry(self, response) -> dict:
        return {k: response.meta[k] for k in CARRIED if k in response.meta}

    def _run(self, response) -> Optional[SourceRun]:
        return self.tracker.for_request(response.request if hasattr(response, 'request') else response)

    def _flush(self):
        self.tracker.flush()
        if self.snapshots:
            self.snapshots.flush()

    def _on_closed(self, spider, reason):
        self._flush()
        if self.client:
            self.client.close()
