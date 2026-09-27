"""
BaseEventSpider — foundation that all site-specific spiders extend.

Multi-level page support
------------------------
Data is accumulated in a 'context' dict that flows down the request chain
via Scrapy's request.meta.  Each level extracts what it can; the next level
inherits it and fills any remaining gaps.

  Venue page (location, hours)          ← extract_page_context()
    └── Category/month page             ← is_intermediate_page() → True
          └── Individual event page     ← parse_event() fills in dates, etc.

Priority rule (most specific wins):
  detail page data  >  listing page context  >  venue page context

Extraction per page lives in scraper/extraction.py: listing pages yield every
event (JSON-LD / inline JSON / recipe / AI list); detail pages run the
JSON-LD → inline JSON → OpenGraph → selectors → AI waterfall.

Subclass contract (minimum):
  name        = 'my_spider'
  start_urls  = ['https://example.com/events']

Useful overrides for multi-level sites:
  extract_page_context(response) → dict
      Return ambient data from a listing/venue page that should be inherited
      by all child pages (e.g. location, recurring time slot).
      title / start_datetime / end_datetime are ignored — they must come per-event.

  is_intermediate_page(url, response) → bool
      Return True if the URL leads to another listing rather than an event
      detail. The spider will follow with parse_listing so context continues
      to accumulate. Default: False (all followed links → parse_event).

  listing_link_css / listing_link_xpath
      CSS or XPath selector for event (or sub-listing) links on a listing page.

  event_selectors  dict[field, css_selector]
      Site-specific field extraction before the AI fallback.

  _next_page(response) → str | None
      Return the next-page URL for pagination support.

  is_event_page(response) → bool
      Heuristic for deciding whether a start_url is already an event detail.
"""

from __future__ import annotations
import logging
from typing import Optional

import scrapy

from scraper import extraction
from scraper.extractors import jsonld
from scraper.items import EventItem

logger = logging.getLogger(__name__)


class BaseEventSpider(scrapy.Spider):

    # ── Subclass overrides ────────────────────────────────────────────────────

    listing_link_css:   Optional[str]       = None
    listing_link_xpath: Optional[str]       = None
    event_selectors:    dict[str, str]      = {}

    # List of VenueSeed objects (from scraper.seeds.overpass).
    # When set, start_requests uses these instead of start_urls, and each
    # seed's location context (lat/lon/address/title) is injected into the
    # initial request so it propagates to every event scraped from that site.
    start_seeds: list = []

    # ── Entry point ───────────────────────────────────────────────────────────

    def start_requests(self):
        if self.start_seeds:
            for seed in self.start_seeds:
                yield scrapy.Request(
                    seed.url,
                    callback=self.parse,
                    meta={'context': seed.as_context()},
                    errback=self._handle_error,
                )
        else:
            yield from super().start_requests()

    def parse(self, response, **kwargs):
        """Route the start URL to the appropriate handler."""
        if self.is_event_page(response):
            yield from self.parse_event(response)
        else:
            yield from self.parse_listing(response)

    # ── Listing / intermediate pages ──────────────────────────────────────────

    def parse_listing(self, response, **kwargs):
        """
        Process a listing or intermediate page.

        1. Extracts ambient page context (venue info, shared location, etc.)
           and merges it with any context inherited from a parent page.
        2. Follows each found link.  Links that lead to another listing are
           followed with parse_listing (context accumulates further); links
           that lead to event detail pages are followed with parse_event.
        3. Handles pagination.
        """
        inherited: dict = response.meta.get('context', {})

        # Pull ambient data from this page (venue name, location, etc.)
        page_ctx: dict = self.extract_page_context(response)

        # Build the context to pass to child pages.
        # Page-level data overrides inherited data; neither overrides None.
        context: dict = dict(inherited)
        for key, value in page_ctx.items():
            if value is not None:
                context[key] = value

        links = self._collect_links(response)

        if not links:
            logger.debug("No links found on listing page: %s — trying as event page", response.url)
            yield from self.parse_event(response)
            return

        for url in links:
            if self.is_intermediate_page(url, response):
                yield response.follow(
                    url,
                    callback=self.parse_listing,
                    meta={'context': context},
                    errback=self._handle_error,
                )
            else:
                yield response.follow(
                    url,
                    callback=self.parse_event,
                    meta={'context': context},
                    errback=self._handle_error,
                )

        # Pagination — carry context through to the next page
        next_page = self._next_page(response)
        if next_page:
            yield response.follow(
                next_page,
                callback=self.parse_listing,
                meta={'context': context},
                errback=self._handle_error,
            )

    # ── Event detail pages ────────────────────────────────────────────────────

    def parse_event(self, response, **kwargs):
        """
        Extract every event on a page (one for a detail page, many for a
        listing) — see scraper/extraction.py for the waterfall.

        Inherited context arrives via response.meta['context'] and only fills
        gaps. response.meta['partial'] is this same event as seen on its
        listing page; it may supply a title/date the detail page lacks.
        """
        inherited: dict = response.meta.get('context', {})
        partial: Optional[dict] = response.meta.get('partial')

        result = self.extract_page(response)
        text = self._clean_text(response.text) if result.single else None
        for data, node in result.events:
            final = extraction.finalize(
                data, context=inherited, partial=partial, jsonld_node=node, page_text=text)
            if final is None:
                logger.debug("No event data found on %s — skipping", response.url)
                continue
            yield self.build_item(final, response)

        if not result.events and partial:
            final = extraction.finalize(partial, context=inherited)
            if final:
                yield self.build_item(final, response)

    def extract_page(self, response) -> extraction.PageResult:
        return extraction.extract_page(
            response.text, response.url,
            selectors=self._extract_selectors(response),
            ai=self._ai_extractor(response),
        )

    def build_item(self, data: dict, response) -> EventItem:
        item = EventItem()
        item['source_url']        = response.url
        item['extraction_method'] = data.get('extraction_method', 'unknown')
        for field in (
            'title', 'description', 'start_datetime', 'end_datetime',
            'location_title', 'location_address', 'location_lat', 'location_lon',
            'ticket_price', 'ticket_url', 'url', 'image_url', 'tag_names',
            'recurrence_freq', 'recurrence_interval', 'recurrence_byday',
            'recurrence_month_mode', 'recurrence_until', 'recurrence_count',
            'rdates', 'exdates', 'timezone', 'evidence', 'drop_reason',
        ):
            item[field] = data.get(field)
        return item

    # ── Hooks for subclasses ──────────────────────────────────────────────────

    def extract_page_context(self, response) -> dict:
        """
        Extract ambient data from a listing or venue page that should be
        inherited by all child event pages.

        Override this in site-specific spiders to capture data like:
          - Venue name / address (if every event on the page is at the same venue)
          - A shared default price
          - A recurring time slot implied by the page ("Every Thursday at 8pm")

        Return only fields that genuinely apply to all events on the page.
        title / start_datetime / end_datetime are ignored — they must come per-event.

        Example override:
            def extract_page_context(self, response):
                return {
                    'location_title':   response.css('h1.venue-name::text').get('').strip(),
                    'location_address': response.css('.address::text').get('').strip(),
                }
        """
        return {}

    def is_intermediate_page(self, url: str, response=None) -> bool:
        """
        Return True if `url` leads to another listing/category page rather
        than an individual event detail page.

        When True, the spider follows with parse_listing so context continues
        to accumulate through another level before reaching event details.

        Default: False — assumes all followed links lead directly to events.

        Override with URL-pattern logic for multi-level sites:
            def is_intermediate_page(self, url, response=None):
                return '/calendar/' in url or '/month/' in url
        """
        return False

    def is_event_page(self, response) -> bool:
        """
        Heuristic for start_urls: is this response an event detail page?
        Default: True if schema.org/Event JSON-LD is present.
        """
        return bool(jsonld.extract(response.text, response.url))

    def _next_page(self, response) -> Optional[str]:
        """Return the next-page URL for pagination, or None. Override in subclass."""
        return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _collect_links(self, response) -> list[str]:
        links: list[str] = []
        if self.listing_link_css:
            links = response.css(self.listing_link_css).getall()
        elif self.listing_link_xpath:
            links = response.xpath(self.listing_link_xpath).getall()
        return [response.urljoin(url) for url in links]

    def _extract_selectors(self, response) -> dict:
        result = {}
        for field, selector in self.event_selectors.items():
            value = response.css(selector).get()
            if value:
                result[field] = value.strip()
        return result

    def _ai_extractor(self, response):
        """The AI callable for extract_page, or None when AI is disabled."""
        api_key = self.settings.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return None
        from scraper.extractors import ai
        venue = (response.meta.get('context') or {}).get('location_title')
        return lambda html, url: ai.extract_many(html, url, api_key, venue=venue)

    _sufficient = staticmethod(extraction.sufficient)
    _merge = staticmethod(extraction.merge)
    _merge_enriched = staticmethod(extraction.merge_enriched)
    _clean_text = staticmethod(extraction.page_text)

    def _handle_error(self, failure):
        logger.error("Request failed: %s — %s", failure.request.url, failure.value)
