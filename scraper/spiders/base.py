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

Extractors within a single page follow their own waterfall:
  1. JSON-LD  (schema.org/Event — free, structured)
  2. OpenGraph event meta tags
  3. Site-specific CSS selectors  (event_selectors dict)
  4. AI fallback via Claude Haiku  (only if ANTHROPIC_API_KEY is set)

Subclass contract (minimum):
  name        = 'my_spider'
  start_urls  = ['https://example.com/events']

Useful overrides for multi-level sites:
  extract_page_context(response) → dict
      Return ambient data from a listing/venue page that should be inherited
      by all child pages (e.g. location, recurring time slot).
      Do NOT return start_datetime or title — those must come per-event.

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

from scraper.extractors import (
    jsonld,
    opengraph,
    inline_json,
    tags as tag_matcher,
    recurrence as recurrence_extractor,
    dates as dates_extractor,
)
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
        Extract one event from a detail page.

        Inherited context arrives via response.meta['context'] and acts as a
        fallback: it fills fields that the page's own extractors didn't find,
        but it never overrides them.

        Priority (high → low):
          Page JSON-LD  >  Page OpenGraph  >  Page selectors  >  Page AI
          >  Inherited context from parent listing/venue pages
        """
        inherited: dict = response.meta.get('context', {})

        # page_data collects what this specific page's extractors find.
        # These always win over inherited context.
        page_data: dict = {}

        # ── 1 · JSON-LD ───────────────────────────────────────────────────────
        jl_node = None
        jl = jsonld.extract(response.text, response.url)
        if jl:
            self._merge(page_data, jl)
            page_data.setdefault('extraction_method', 'jsonld')
            # Keep the raw node for the recurrence extractor below.
            try:
                import extruct
                from scraper.extractors.jsonld import _find_event
                jl_data = extruct.extract(
                    response.text, base_url=response.url,
                    syntaxes=['json-ld'], uniform=True,
                )
                for node in jl_data.get('json-ld', []):
                    jl_node = _find_event(node)
                    if jl_node:
                        break
            except Exception:
                pass

        # ── 2 · Inline JS data (always runs — free, supplements structured data) ──
        inline = inline_json.extract(response.text, response.url)
        if inline:
            self._merge_enriched(page_data, inline)
            page_data.setdefault('extraction_method', 'inline_json')

        # ── 3 · OpenGraph ─────────────────────────────────────────────────────
        if not self._sufficient(page_data):
            og = opengraph.extract(response.text, response.url)
            if og:
                self._merge(page_data, og)
                page_data.setdefault('extraction_method', 'opengraph')

        # ── 4 · Site-specific selectors ───────────────────────────────────────
        if not self._sufficient(page_data):
            sel = self._extract_selectors(response)
            if sel:
                self._merge(page_data, sel)
                page_data.setdefault('extraction_method', 'selectors')

        # ── 5 · AI fallback ───────────────────────────────────────────────────
        if not self._sufficient(page_data):
            ai_data = self._ai_extract(response)
            if ai_data:
                self._merge(page_data, ai_data)
                page_data.setdefault('extraction_method', 'ai')

        # ── Merge: page data first, inherited context fills remaining gaps ─────
        data: dict = dict(page_data)
        for key, value in inherited.items():
            if value is not None and not data.get(key):
                data[key] = value

        if not data.get('title'):
            logger.debug("No event data found on %s — skipping", response.url)
            return

        # ── Recurrence ────────────────────────────────────────────────────────
        rec = recurrence_extractor.extract(
            title=data.get('title', ''),
            description=data.get('description', ''),
            jsonld_node=jl_node,
        )
        if rec:
            # Only set recurrence fields that aren't already in inherited context
            for key, value in rec.items():
                if not data.get(key):
                    data[key] = value

        # ── Explicit dates (rdates / exdates) ────────────────────────────────
        # Try clean page text, then inline JS schedule hint (_schedule_text
        # captured by inline_json from times/schedule/hours fields), then
        # description as a last resort.
        clean_text = self._clean_text(response.text)
        for text_src in filter(None, [clean_text, data.get('_schedule_text'), data.get('description')]):
            date_info = dates_extractor.extract(text_src)
            if not date_info:
                continue
            if date_info.get('start_datetime') and not data.get('start_datetime'):
                data['start_datetime'] = date_info['start_datetime']
            if date_info.get('end_datetime') and not data.get('end_datetime'):
                data['end_datetime'] = date_info['end_datetime']
            if date_info.get('rdates'):
                existing = data.get('rdates') or []
                data['rdates'] = list(dict.fromkeys(existing + date_info['rdates']))
            if date_info.get('exdates'):
                data.setdefault('exdates', [])
                seen = {e['datetime'] for e in data['exdates']}
                data['exdates'] += [e for e in date_info['exdates'] if e['datetime'] not in seen]
            break  # stop after first source that yields results

        # ── Tag matching ──────────────────────────────────────────────────────
        matched_tags = tag_matcher.match(
            data.get('title', ''),
            data.get('description', ''),
            ctx=data,
        )
        existing = data.get('tag_names') or []
        data['tag_names'] = list(dict.fromkeys(existing + matched_tags))

        # ── Build item ────────────────────────────────────────────────────────
        item = EventItem()
        item['source_url']        = response.url
        item['extraction_method'] = data.get('extraction_method', 'unknown')

        for field in (
            'title', 'description', 'start_datetime', 'end_datetime',
            'location_title', 'location_address', 'location_lat', 'location_lon',
            'ticket_price', 'ticket_url', 'url', 'image_url', 'tag_names',
            'recurrence_freq', 'recurrence_interval', 'recurrence_byday',
            'recurrence_month_mode', 'recurrence_until', 'recurrence_count',
            'rdates', 'exdates',
        ):
            item[field] = data.get(field)

        yield item

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
        Do NOT return start_datetime or title — those must come per-event.

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

    def _ai_extract(self, response) -> Optional[dict]:
        from scraper.extractors import ai
        api_key = self.settings.get('ANTHROPIC_API_KEY', '')
        if not api_key:
            return None
        return ai.extract(response.text, response.url, api_key)

    @staticmethod
    def _sufficient(data: dict) -> bool:
        """True if we have the minimum two fields the backend requires."""
        return bool(data.get('title') and data.get('start_datetime'))

    @staticmethod
    def _merge(base: dict, override: dict) -> None:
        """
        Copy non-null values from `override` into `base` without clobbering.
        'First extractor wins' within a single page.
        """
        for key, value in override.items():
            if value is not None and not base.get(key):
                base[key] = value

    @staticmethod
    def _merge_enriched(base: dict, override: dict) -> None:
        """Like _merge, but also overrides description with a longer value."""
        for key, value in override.items():
            if value is None:
                continue
            if not base.get(key):
                base[key] = value
            elif key == 'description' and isinstance(value, str) and len(value) > len(str(base[key])):
                base[key] = value

    @staticmethod
    def _clean_text(html: str) -> Optional[str]:
        """Extract readable text from HTML using trafilatura, or None if unavailable."""
        try:
            import trafilatura
            return trafilatura.extract(html, include_comments=False, include_tables=False)
        except ImportError:
            return None

    def _handle_error(self, failure):
        logger.error("Request failed: %s — %s", failure.request.url, failure.value)
