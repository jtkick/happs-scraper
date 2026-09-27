"""
GenericEventSpider — one spider for every venue website.

    scrapy crawl generic -a limit=200          # crawl sources the backend says are due
    scrapy crawl generic -a source=venue.com   # one registered source (debugging)
    scrapy crawl generic -a url=https://…      # ad hoc, nothing reported to the backend
    … -a relearn=1                             # ignore the saved recipe; rediscover

Per source:
  1. Saved recipe? → go straight to its platform feed or events pages.
     Otherwise discover: platform detection → scored homepage links →
     sitemap.xml → Claude picks from the homepage links → the homepage itself.
  2. Listing pages: extract every event (scraper/extraction.py). Follow an
     event's detail page only when the listing lacks its date or description,
     carrying the listing data along as meta['partial']. Paginate.
  3. Anything learned (events URLs, platform, selectors) and a run report go
     back to the backend when the spider closes (scraper/sources/tracker.py).
"""
from __future__ import annotations
import logging
from typing import Optional
from urllib.parse import urlparse

import scrapy
from scrapy import signals
from scrapy.exceptions import IgnoreRequest

from scraper import extraction, platforms
from scraper.discovery import links as link_classifier
from scraper.discovery.events_page import (
    events_urls_from_sitemap, find_events_pages, page_links, same_site, site_of,
)
from scraper.extractors import recipe as recipe_extractor
from scraper.sources.client import BackendClient
from scraper.sources.tracker import RunTracker, SourceRun
from scraper.spiders.base import BaseEventSpider

logger = logging.getLogger(__name__)


class GenericEventSpider(BaseEventSpider):
    name = 'generic'

    def __init__(self, limit=50, source=None, url=None, relearn=False, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limit = int(limit)
        self.source_domain = source
        self.adhoc_url = url
        self.force_relearn = str(relearn).lower() in ('1', 'true', 'yes')
        self.tracker = RunTracker()
        self.client: Optional[BackendClient] = None

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.client = BackendClient.from_settings(crawler.settings)
        if not spider.adhoc_url:
            spider.tracker.client = spider.client
        crawler.signals.connect(spider.tracker.item_scraped, signal=signals.item_scraped)
        crawler.signals.connect(spider.tracker.item_dropped, signal=signals.item_dropped)
        crawler.signals.connect(spider._on_closed, signal=signals.spider_closed)
        return spider

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
        sources = self.client.due_sources(self.limit)
        logger.info("Crawling %d due sources", len(sources))
        return sources

    def entry_requests(self, source: dict):
        relearn = bool(self.force_relearn or source.get('relearn_requested')) \
            and not source.get('recipe_locked')
        recipe = dict(source.get('override') or {}) if relearn else dict(source.get('effective_recipe') or {})
        run = self.tracker.start(source, relearning=relearn)
        meta = {
            'run_key': run.key,
            'source_id': source.get('id'),
            'context': source.get('context') or {},
            'recipe': recipe,
        }
        # Only a healthy, known site may skip unchanged pages.
        conditional = not relearn and source.get('status') == 'active'

        adapter = platforms.get(recipe.get('platform'))
        if adapter and recipe.get('platform_urls'):
            for url in recipe['platform_urls']:
                yield self._platform_request(url, adapter, meta, run)
        elif recipe.get('events_urls'):
            for url in recipe['events_urls']:
                yield self._listing_request(url, meta, run, conditional=conditional)
        elif self.adhoc_url and urlparse(self.adhoc_url).path.strip('/'):
            yield self._listing_request(self.adhoc_url, meta, run)
        else:
            yield self._request(source['homepage_url'], self.parse_home, meta, 'home', run)

    # ── Discovery ─────────────────────────────────────────────────────────────

    def parse_home(self, response):
        run = self._run(response)
        run.listing_pages += 1
        meta = self._carry(response)

        found = yield from self._maybe_platform(response, meta, run)
        if found:
            return

        urls = find_events_pages(response)
        if urls:
            yield from self._follow_listings(urls, meta, run, origin='heuristic')
            return

        meta['home_links'] = [(text, url) for text, url, _ in page_links(response)]
        meta['home_url'] = response.url
        yield scrapy.Request(response.urljoin('/sitemap.xml'), callback=self.parse_sitemap,
                             errback=self._sitemap_failed, meta={**meta, 'stage': 'sitemap'})

    def parse_sitemap(self, response):
        run = self._run(response)
        locs = response.xpath('//*[local-name()="loc"]/text()').getall()
        urls = [u for u in events_urls_from_sitemap(locs) if same_site(u, response.url)]
        if urls:
            yield from self._follow_listings(urls, self._carry(response), run, origin='heuristic')
        else:
            yield from self._ai_or_homepage(response.meta, run)

    def _sitemap_failed(self, failure):
        request = failure.request
        yield from self._ai_or_homepage(request.meta, self.tracker.for_request(request))

    def _ai_or_homepage(self, raw_meta: dict, run: SourceRun):
        home_links = raw_meta.get('home_links', [])
        home = raw_meta.get('home_url') or run.source['homepage_url']
        meta = {k: raw_meta[k] for k in self._CARRIED if k in raw_meta and not k.startswith('home_')}
        api_key = self.settings.get('ANTHROPIC_API_KEY', '')
        if api_key and home_links:
            from scraper.extractors import ai
            urls = ai.pick_events_links(home_links, home, api_key)
            if urls:
                yield from self._follow_listings(urls, meta, run, origin='ai')
                return
        # Many small sites list events on the homepage itself.
        yield from self._follow_listings([home], meta, run, origin='heuristic', dont_filter=True)

    def _follow_listings(self, urls, meta, run, origin, dont_filter=False):
        self.tracker.learn(run, origin, events_urls=list(urls))
        for url in urls:
            yield self._listing_request(url, meta, run, dont_filter=dont_filter)

    # ── Listings ──────────────────────────────────────────────────────────────

    def parse_listing(self, response):
        run = self._run(response)
        run.listing_pages += 1
        self.tracker.page_fetched(response.request, response.url)
        meta = self._carry(response)
        recipe = meta['recipe']

        if not recipe.get('platform'):
            found = yield from self._maybe_platform(response, meta, run)
            if found:
                return

        recipe_events = recipe_extractor.extract(response, recipe) if recipe.get('item_css') else None
        result = extraction.extract_page(
            response.text, response.url,
            recipe_events=recipe_events, ai=self._ai_extractor(response))
        run.strategies[result.strategy] += len(result.events)
        if recipe.get('item_css') and result.strategy == 'ai':
            run.strategy_fallback = True
        if result.ai_truncated:
            run.complete = False
        if result.strategy == 'ai' and len(result.events) >= 2 and not recipe.get('item_css'):
            learned = recipe_extractor.learn(response, [d for d, _ in result.events],
                                             self.settings.get('ANTHROPIC_API_KEY', ''))
            if learned:
                self.tracker.learn(run, 'ai', **learned)

        found = link_classifier.classify(response, detail_css=recipe.get('detail_link_css'),
                                         pagination_css=recipe.get('pagination_css'))
        text = self._clean_text(response.text) if result.single else None
        events = [e for e in (
            extraction.finalize(d, context=meta['context'], jsonld_node=n, page_text=text)
            for d, n in result.events) if e]

        yield from self._emit_listing_events(events, response, meta, run)

        if not events:
            budget = self._detail_budget(run)
            for url in found.details[:budget]:
                yield self._detail_request(url, meta, run, response.url)
            if len(found.details) > budget:
                run.complete = False

        if found.next_page:
            if run.scheduled_listings < self.settings.getint('GENERIC_MAX_LISTING_PAGES', 15):
                yield self._listing_request(found.next_page, meta, run)
            else:
                run.complete = False

    def _emit_listing_events(self, events, response, meta, run):
        for data in events:
            detail_url = self._detail_url(data, response)
            if detail_url and self._needs_detail(data):
                if self._detail_budget(run) > 0:
                    yield self._detail_request(detail_url, meta, run, response.url, partial=data)
                    continue
            yield self.build_item(data, response)

    @staticmethod
    def _needs_detail(data: dict) -> bool:
        return not (data.get('start_datetime') and data.get('description'))

    @staticmethod
    def _detail_url(data: dict, response) -> Optional[str]:
        url = data.get('url')
        if not url:
            return None
        url = response.urljoin(url)
        if url.rstrip('/') == response.url.rstrip('/') or not same_site(url, response.url):
            return None
        return url

    def _detail_budget(self, run: SourceRun) -> int:
        return max(0, self.settings.getint('GENERIC_MAX_DETAIL_PAGES', 60) - run.scheduled_details)

    # ── Platforms ─────────────────────────────────────────────────────────────

    def _maybe_platform(self, response, meta, run):
        """Generator: yields platform requests; returns True when a platform took over."""
        adapter = platforms.detect(response)
        if adapter is None:
            return False
        if adapter.rerender_only:
            if response.meta.get('playwright'):
                return False            # already rendered — carry on normally
            if not self.settings.getbool('PLAYWRIGHT_ENABLED'):
                message = f'needs_js ({adapter.name}): {response.url}'
                if message not in run.errors:
                    run.errors.append(message)
                return False
            logger.info("%s: %s page, re-requesting rendered", response.url, adapter.name)
            self.tracker.learn(run, 'heuristic', render_js=True)
            meta['recipe'] = {**meta['recipe'], 'render_js': True}
            yield response.request.replace(
                meta={**meta, 'stage': response.meta.get('stage'), 'playwright': True},
                dont_filter=True)
            return True
        urls = adapter.feed_urls(response)
        if not urls:
            return False
        logger.info("%s: detected platform %s", response.url, adapter.name)
        self.tracker.learn(run, 'platform', platform=adapter.name, platform_urls=urls)
        meta = {**meta, 'recipe': {**meta['recipe'], 'platform': adapter.name}}
        for url in urls:
            yield self._platform_request(url, adapter, meta, run)
        return True

    def _platform_request(self, url, adapter, meta, run):
        request = self._request(url, self.parse_platform, {**meta, 'platform': adapter.name},
                                'listing', run)
        if adapter.render_js and self.settings.getbool('PLAYWRIGHT_ENABLED'):
            request.meta['playwright'] = True
        return request

    def parse_platform(self, response):
        run = self._run(response)
        adapter = platforms.get(response.meta['platform'])
        events = adapter.parse(response)
        if events is None:           # adapter only locates the page; extract it generically
            yield from self.parse_listing(response)
            return
        run.listing_pages += 1
        run.strategies[f'platform:{adapter.name}'] += len(events)
        meta = self._carry(response)
        for data in events:
            data.setdefault('extraction_method', f'platform:{adapter.name}')
            final = extraction.finalize(data, context=meta['context'])
            if final:
                yield from self._emit_listing_events([final], response, meta, run)
        next_url = adapter.next_url(response)
        if next_url and run.scheduled_listings < self.settings.getint('GENERIC_MAX_LISTING_PAGES', 15):
            yield self._platform_request(next_url, adapter, meta, run)
        elif next_url:
            run.complete = False

    # ── Detail pages ──────────────────────────────────────────────────────────

    def parse_event(self, response, **kwargs):
        run = self._run(response)
        if run:
            run.detail_pages += 1
        yield from super().parse_event(response, **kwargs)

    def extract_page(self, response) -> extraction.PageResult:
        partial = response.meta.get('partial') or {}
        ai = None if extraction.sufficient(partial) else self._ai_extractor(response)
        result = extraction.extract_page(response.text, response.url, ai=ai)
        run = self._run(response)
        if run:
            run.strategies[result.strategy or 'partial'] += max(1, len(result.events))
        return result

    def build_item(self, data: dict, response):
        item = super().build_item(data, response)
        item['source_id'] = response.meta.get('source_id')
        return item

    # ── Requests & bookkeeping ────────────────────────────────────────────────

    def _request(self, url, callback, meta, stage, run, dont_filter=False, **extra_meta):
        meta = {**meta, 'stage': stage, **extra_meta}
        if meta.get('recipe', {}).get('render_js') and self.settings.getbool('PLAYWRIGHT_ENABLED'):
            meta['playwright'] = True
        if stage == 'listing':
            run.scheduled_listings += 1
        elif stage == 'detail':
            run.scheduled_details += 1
        return scrapy.Request(url, callback=callback, errback=self._errback, meta=meta,
                              dont_filter=dont_filter)

    def _listing_request(self, url, meta, run, conditional=False, dont_filter=False):
        return self._request(url, self.parse_listing, meta, 'listing', run,
                             dont_filter=dont_filter, conditional=conditional, cacheable=True)

    def _detail_request(self, url, meta, run, listing_url, partial=None):
        return self._request(url, self.parse_event, meta, 'detail', run,
                             partial=partial, listing_url=listing_url)

    def _errback(self, failure):
        request = failure.request
        if failure.check(IgnoreRequest) and '304' in str(failure.value):
            return
        stage = request.meta.get('stage', 'detail')
        self.tracker.error(request, failure, stage)
        logger.warning("%s request failed: %s — %s", stage, request.url, failure.value)
        partial = request.meta.get('partial')
        if stage == 'detail' and partial:
            # The listing already had this event; don't lose it with its page.
            item = self.build_item(partial, request)
            yield item

    _CARRIED = ('run_key', 'source_id', 'context', 'recipe', 'home_links', 'home_url')

    def _carry(self, response) -> dict:
        return {k: response.meta[k] for k in self._CARRIED if k in response.meta}

    def _run(self, response) -> Optional[SourceRun]:
        return self.tracker.for_request(response.request if hasattr(response, 'request') else response)

    def _on_closed(self, spider, reason):
        self.tracker.flush()
        if self.client:
            self.client.close()
