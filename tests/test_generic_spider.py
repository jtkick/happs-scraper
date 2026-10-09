"""
Tests for GenericEventSpider — discovery → listing → detail flow, driven
offline: each test feeds hand-built responses to the spider's callbacks.
"""
import json

import pytest
import scrapy
from scrapy.http import HtmlResponse, TextResponse
from scrapy.settings import Settings
from twisted.python.failure import Failure

from scraper.items import EventItem
from scraper.spiders.generic import GenericEventSpider

HOME = 'https://venue.test/'


def make_spider(**settings):
    spider = GenericEventSpider()
    spider.settings = Settings({'ANTHROPIC_API_KEY': '', 'GENERIC_MAX_DETAIL_PAGES': 60,
                                'GENERIC_MAX_LISTING_PAGES': 15, **settings})
    return spider


def source(**overrides):
    base = {'id': 'src-1', 'domain': 'venue.test', 'homepage_url': HOME, 'status': 'active',
            'recipe': {}, 'effective_recipe': {}, 'override': {},
            'context': {'location_title': 'The Venue', 'timezone': 'America/New_York'}}
    base.update(overrides)
    return base


def respond(request, body, cls=HtmlResponse, **headers):
    data = body.encode() if isinstance(body, str) else body
    return cls(request.url, body=data, encoding='utf-8', request=request, headers=headers or None)


def html(body):
    return f'<html><body>{body}</body></html>'


def ld(*events):
    nodes = [{'@type': 'Event', 'name': n, 'startDate': d, **extra} for n, d, extra in events]
    return f'<script type="application/ld+json">{json.dumps(nodes)}</script>'


def split(output):
    output = list(output)
    return ([o for o in output if isinstance(o, EventItem)],
            [o for o in output if isinstance(o, scrapy.Request)])


@pytest.fixture
def spider():
    return make_spider()


# ── Entry ─────────────────────────────────────────────────────────────────────

def test_new_source_starts_at_the_homepage(spider):
    [request] = spider.entry_requests(source(status='new'))
    assert request.url == HOME and request.callback == spider.parse_home


def test_known_source_goes_straight_to_saved_listings(spider):
    recipe = {'events_urls': ['https://venue.test/events']}
    [request] = spider.entry_requests(source(effective_recipe=recipe))
    assert request.callback == spider.parse_listing
    assert request.meta['conditional'] is True


def test_relearn_ignores_the_learned_recipe_but_not_overrides(spider):
    recipe = {'events_urls': ['https://venue.test/old']}
    [request] = spider.entry_requests(source(effective_recipe=recipe, relearn_requested=True))
    assert request.url == HOME and not request.meta.get('conditional')
    override = {'events_urls': ['https://venue.test/pinned']}
    [request] = spider.entry_requests(source(effective_recipe={**recipe, **override},
                                             override=override, relearn_requested=True))
    assert request.url == 'https://venue.test/pinned'


def test_saved_platform_feed_is_used_directly(spider):
    recipe = {'platform': 'wordpress_tribe', 'platform_urls': ['https://venue.test/wp-json/feed']}
    [request] = spider.entry_requests(source(effective_recipe=recipe))
    assert request.callback == spider.parse_platform


# ── Discovery ─────────────────────────────────────────────────────────────────

def test_homepage_link_discovery(spider):
    [home] = spider.entry_requests(source(status='new'))
    _, requests = split(spider.parse_home(respond(home, html('<nav><a href="/events">Events</a></nav>'))))
    assert [r.url for r in requests] == ['https://venue.test/events']
    run = spider.tracker.for_request(home)
    assert run.learned == {'events_urls': ['https://venue.test/events']}


def test_no_links_falls_back_to_sitemap_then_homepage(spider):
    [home] = spider.entry_requests(source(status='new'))
    _, [sitemap] = split(spider.parse_home(respond(home, html('<a href="/about">About</a>'))))
    assert sitemap.url == 'https://venue.test/sitemap.xml'
    empty = respond(sitemap, '<urlset><url><loc>https://venue.test/about</loc></url></urlset>',
                    cls=TextResponse)
    _, [again] = split(spider.parse_sitemap(empty))
    assert again.url == HOME and again.callback == spider.parse_listing and again.dont_filter


def test_sitemap_listing_is_followed(spider):
    [home] = spider.entry_requests(source(status='new'))
    _, [sitemap] = split(spider.parse_home(respond(home, html(''))))
    body = '<urlset><url><loc>https://venue.test/calendar</loc></url></urlset>'
    _, [listing] = split(spider.parse_sitemap(respond(sitemap, body, cls=TextResponse)))
    assert listing.url == 'https://venue.test/calendar'


def test_platform_on_homepage_takes_over(spider):
    [home] = spider.entry_requests(source(status='new'))
    body = html('<iframe src="https://calendar.google.com/calendar/embed?src=abc"></iframe>')
    _, [feed] = split(spider.parse_home(respond(home, body)))
    assert feed.callback == spider.parse_platform and feed.url.endswith('/abc/public/basic.ics')
    assert spider.tracker.for_request(home).learned['platform'] == 'ical'


def test_render_only_page_without_playwright_is_reported(spider):
    [home] = spider.entry_requests(source(status='new'))
    body = '<html><head><meta name="generator" content="Wix.com"></head><body>' \
           '<a href="/events">Events</a></body></html>'
    _, requests = split(spider.parse_home(respond(home, body)))
    assert [r.url for r in requests] == ['https://venue.test/events']   # carried on
    assert 'needs_js' in spider.tracker.for_request(home).errors[0]


def test_render_only_page_is_rerendered_with_playwright():
    spider = make_spider(PLAYWRIGHT_ENABLED=True)
    [home] = spider.entry_requests(source(status='new'))
    home = home.replace(meta={k: v for k, v in home.meta.items() if k != 'playwright'})
    body = '<html><head><meta name="generator" content="Wix.com"></head><body></body></html>'
    _, [again] = split(spider.parse_home(respond(home, body)))
    assert again.url == HOME and again.meta['playwright'] and again.callback == spider.parse_home
    assert spider.tracker.for_request(home).learned['render_js'] is True


def test_rendered_page_is_not_rendered_again():
    spider = make_spider(PLAYWRIGHT_ENABLED=True)
    [home] = spider.entry_requests(source(status='new'))
    body = '<html><head><meta name="generator" content="Wix.com"></head><body>' \
           '<a href="/events">Events</a></body></html>'
    _, requests = split(spider.parse_home(respond(home, body)))
    assert [r.url for r in requests] == ['https://venue.test/events']


# ── Rendering ─────────────────────────────────────────────────────────────────

def test_html_pages_are_rendered_and_feeds_are_not():
    spider = make_spider(PLAYWRIGHT_ENABLED=True)
    [home] = spider.entry_requests(source(status='new'))
    assert home.meta['playwright'] is True
    _, [sitemap] = split(spider.parse_home(respond(home, html(''))))
    assert not sitemap.meta.get('playwright')
    [listing] = spider.entry_requests(source(effective_recipe={'events_urls': ['https://venue.test/events']}))
    assert listing.meta['playwright'] is True
    recipe = {'platform': 'ical', 'platform_urls': ['https://venue.test/cal.ics']}
    [feed] = spider.entry_requests(source(effective_recipe=recipe))
    assert not feed.meta.get('playwright')


def test_nothing_is_rendered_without_playwright(spider):
    [home] = spider.entry_requests(source(status='new'))
    assert not home.meta.get('playwright')


def _failure(request, exc=ConnectionError('boom')):
    try:
        raise exc
    except type(exc):
        fail = Failure()
    fail.request = request
    return fail


def test_failed_render_is_fetched_raw_once():
    spider = make_spider(PLAYWRIGHT_ENABLED=True)
    request = listing_request(spider)
    body = html(ld(('Jazz', '2026-06-05T19:00', {'url': '/events/jazz'})))
    _, [detail] = split(spider.parse_listing(respond(request, body)))
    [retry] = list(spider._errback(_failure(detail, TimeoutError('render timed out'))))
    assert retry.url == detail.url and retry.dont_filter
    assert not retry.meta.get('playwright') and retry.meta['render_failed']
    assert retry.meta['partial']['title'] == 'Jazz'
    assert 'render_failed' in spider.tracker.for_request(detail).errors[0]
    [item] = list(spider._errback(_failure(retry)))          # raw failed too: keep the partial
    assert item['title'] == 'Jazz'


# ── Listings ──────────────────────────────────────────────────────────────────

def listing_request(spider, **src):
    [request] = spider.entry_requests(source(effective_recipe={'events_urls': ['https://venue.test/events']}, **src))
    return request


COMPLETE = {'description': 'Live jazz.', 'endDate': '2026-06-05T22:00',
            'location': {'@type': 'Place', 'name': 'The Venue', 'address': '1 Main St'}}


def test_complete_listing_events_are_emitted_directly(spider):
    request = listing_request(spider)
    body = html(ld(('Jazz', '2026-06-05T19:00', {**COMPLETE, 'url': '/events/jazz'}),
                   ('Trivia', '2026-06-06T20:00', {'description': 'Quiz'})))
    items, requests = split(spider.parse_listing(respond(request, body)))
    assert [i['title'] for i in items] == ['Jazz', 'Trivia']
    assert all(i['source_id'] == 'src-1' and i['location_title'] == 'The Venue' for i in items)
    assert items[0]['timezone'] == 'America/New_York'
    assert requests == []


def test_incomplete_events_fetch_their_detail_page_with_the_partial(spider):
    request = listing_request(spider)
    body = html(ld(('Jazz', '2026-06-05T19:00', {'url': '/events/jazz'}),
                   ('Trivia', '2026-06-06T20:00', {'url': 'https://tickets.other.test/t'})))
    items, [detail] = split(spider.parse_listing(respond(request, body)))
    assert [i['title'] for i in items] == ['Trivia']           # external link: emit as-is
    assert detail.url == 'https://venue.test/events/jazz'
    assert detail.meta['partial']['title'] == 'Jazz'

    [item] = list(spider.parse_event(respond(detail, html('<p>nothing useful</p>'))))
    assert item['title'] == 'Jazz' and item['start_datetime'] == '2026-06-05T19:00'


def test_failed_detail_page_still_emits_the_partial(spider):
    request = listing_request(spider)
    body = html(ld(('Jazz', '2026-06-05T19:00', {'url': '/events/jazz'}),
                   ('Blues', '2026-06-07T19:00', {'url': '/events/blues'})))
    _, [detail, _] = split(spider.parse_listing(respond(request, body)))
    [item] = list(spider._errback(_failure(detail)))
    assert item['title'] == 'Jazz' and item['source_id'] == 'src-1'
    assert spider.tracker.for_request(detail).complete is False


@pytest.mark.parametrize('extra, reason', [
    ({}, None),
    ({'description': None}, 'required'),
    ({'start_datetime': None}, 'required'),
    ({'description': 'An evening of jazz from the trio, with…'}, 'soft'),
    ({'description': 'Live jazz'}, 'soft'),
    ({'location_address': None}, 'soft'),
    ({'location_address': None, 'location_lat': 39.1}, None),
    ({'end_datetime': None}, 'soft'),
    ({'end_datetime': '2026-10-31'}, 'soft'),
    ({'end_datetime': '2026-10-31', 'recurrence_freq': 'daily'}, None),
])
def test_detail_reason(extra, reason):
    data = {'title': 'Jazz', 'start_datetime': '2026-10-01T19:00', 'end_datetime': '2026-10-01T22:00',
            'description': 'Live jazz.', 'location_address': '1 Main St', **extra}
    assert GenericEventSpider._detail_reason(data) == reason


def soft_listing(n):
    return html(ld(*[(f'Show {i}', '2026-06-05T19:00', {'description': 'Live jazz.', 'url': f'/e/{i}'})
                     for i in range(n)]))


def test_soft_follows_leave_the_reserve_for_required_ones():
    spider = make_spider(GENERIC_MAX_DETAIL_PAGES=4, GENERIC_DETAIL_RESERVE=2)
    request = listing_request(spider)
    items, requests = split(spider.parse_listing(respond(request, soft_listing(4))))
    assert len(requests) == 2 and len(items) == 2
    assert all(r.meta['detail_reason'] == 'soft' for r in requests)
    body = html(ld(*[(f'Bare {i}', '2026-06-05T19:00', {'url': f'/b/{i}'}) for i in range(3)]))
    _, required = split(spider.parse_listing(respond(request, body)))
    assert len(required) == 2 and required[0].meta['detail_reason'] == 'required'


def test_unhelpful_detail_pages_are_only_sampled(spider):
    from scraper.sources.tracker import DETAIL_SAMPLE_MIN
    [request] = spider.entry_requests(source(effective_recipe={
        'events_urls': ['https://venue.test/events'], 'detail_useful': False}))
    items, requests = split(spider.parse_listing(respond(request, soft_listing(DETAIL_SAMPLE_MIN + 3))))
    assert len(requests) == DETAIL_SAMPLE_MIN and len(items) == 3


def test_soft_detail_pages_are_scored(spider):
    request = listing_request(spider)
    _, requests = split(spider.parse_listing(respond(request, soft_listing(2))))
    address = ld(('Show 0', '2026-06-05T19:00', {'location': {'@type': 'Place', 'address': '1 Main St'}}))
    list(spider.parse_event(respond(requests[0], html(address))))
    list(spider.parse_event(respond(requests[1], html('<p>nothing</p>'))))
    run = spider.tracker.for_request(request)
    assert (run.soft_details, run.soft_details_useful) == (2, 1)


def test_listing_without_events_follows_detail_links(spider):
    request = listing_request(spider)
    cards = ''.join(f'<div class="c"><a href="/events/{s}">{s}</a></div>' for s in ('a', 'b', 'c'))
    _, requests = split(spider.parse_listing(respond(request, html(cards))))
    assert [r.url for r in requests] == [f'https://venue.test/events/{s}' for s in 'abc']
    assert all(r.callback == spider.parse_event for r in requests)


def test_detail_budget_marks_run_incomplete():
    spider = make_spider(GENERIC_MAX_DETAIL_PAGES=2)
    request = listing_request(spider)
    cards = ''.join(f'<div class="c"><a href="/events/{s}">{s}</a></div>' for s in 'abcd')
    _, requests = split(spider.parse_listing(respond(request, html(cards))))
    assert len(requests) == 2
    assert spider.tracker.for_request(request).complete is False


def test_pagination_is_followed_within_budget():
    spider = make_spider(GENERIC_MAX_LISTING_PAGES=1)
    request = listing_request(spider)
    body = html(ld(('Jazz', '2026-06-05T19:00', {'description': 'd'})) + '<a rel="next" href="?page=2">2</a>')
    _, requests = split(spider.parse_listing(respond(request, body)))
    assert requests == []                                   # budget of 1 already used
    assert spider.tracker.for_request(request).complete is False


def test_recipe_fallback_to_ai_is_flagged(monkeypatch):
    from scraper.extractors.ai import AIResult
    spider = make_spider(ANTHROPIC_API_KEY='k')
    monkeypatch.setattr('scraper.extractors.ai.extract_many', lambda *a, **k: AIResult(events=[
        {'title': 'A', 'start_datetime': '2026-06-05', 'description': 'd'},
        {'title': 'B', 'start_datetime': '2026-06-06', 'description': 'd'}]))
    [request] = spider.entry_requests(source(effective_recipe={
        'events_urls': ['https://venue.test/events'], 'item_css': 'div.gone',
        'fields': {'title': 'h3', 'date': 'time'}}))
    items, _ = split(spider.parse_listing(respond(request, html('<p>redesigned</p>'))))
    assert len(items) == 2
    run = spider.tracker.for_request(request)
    assert run.strategy_fallback is True and run.strategies['ai'] == 2


# ── Platforms ─────────────────────────────────────────────────────────────────

def test_platform_feed_events_become_items(spider):
    recipe = {'platform': 'wordpress_tribe', 'platform_urls': ['https://venue.test/wp-json/feed']}
    [request] = spider.entry_requests(source(effective_recipe=recipe))
    body = json.dumps({'events': [{'title': 'Jazz', 'description': 'd',
                                   'utc_start_date': '2026-06-05 23:00:00'}]})
    items, _ = split(spider.parse_platform(respond(request, body, cls=TextResponse)))
    [item] = items
    assert item['extraction_method'] == 'platform:wordpress_tribe'
    assert item['start_datetime'] == '2026-06-05T23:00:00+00:00'


def test_changed_page_from_a_raw_check_is_fetched_rendered():
    spider = make_spider(PLAYWRIGHT_ENABLED=True)
    request = listing_request(spider)
    check = request.replace(meta={k: v for k, v in request.meta.items() if not k.startswith('playwright')}
                            | {'render_if_changed': True}, headers={'If-None-Match': '"abc"'})
    items, [again] = split(spider.parse_listing(respond(check, html(ld(('Jazz', '2026-06-05T19:00', COMPLETE))))))
    assert items == [] and again.meta['playwright'] and again.dont_filter
    assert not again.meta.get('conditional') and b'If-None-Match' not in again.headers
    assert spider.tracker.for_request(request).listing_pages == 0


# ── Skipping unchanged pages ──────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402

from scrapy.exceptions import DontCloseSpider, IgnoreRequest  # noqa: E402

from scraper import extraction, page_state  # noqa: E402

EVENTS = 'https://venue.test/events'
JAZZ = 'https://venue.test/events/jazz'
JAZZ_LISTING = html(ld(('Jazz', '2026-06-05T19:00', {'description': 'Live jazz…', 'url': '/events/jazz'})))


class FakeClient:
    def __init__(self, pages=(), batches=()):
        self._pages, self.batches = list(pages), list(batches)

    def pages(self, source_id):
        return self._pages

    def due_sources(self, limit):
        return self.batches.pop(0) if self.batches else []


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def jazz_partial():
    spider = make_spider()
    [request] = spider.entry_requests(source(effective_recipe={'events_urls': [EVENTS]}))
    _, [detail] = split(spider.parse_listing(respond(request, JAZZ_LISTING)))
    return detail.meta['partial']


def jazz_record(**extra):
    partial = jazz_partial()
    return {'url': JAZZ, 'kind': 'detail', 'listing_url': EVENTS, 'listing_data': partial,
            'listing_hash': page_state.listing_hash(partial), 'content_hash': 'old',
            'fingerprints': ['fp-jazz'], 'extraction_version': extraction.EXTRACTION_VERSION,
            'fetched_at': ago(1), **extra}


def spider_with(*records, recipe=None):
    spider = make_spider()
    spider.client = FakeClient(records)
    [first, *_] = spider.entry_requests(source(effective_recipe={
        'events_urls': [EVENTS], 'sitemap_urls': [], **(recipe or {})}))
    return spider, first


def test_unchanged_detail_page_is_skipped_and_its_events_carried():
    spider, request = spider_with(jazz_record())
    items, requests = split(spider.parse_listing(respond(request, JAZZ_LISTING)))
    assert items == [] and requests == []
    run = spider.tracker.for_request(request)
    assert run.seen == {'fp-jazz'} and run.skipped_unchanged == 1
    assert run.page_fps[EVENTS] == {'fp-jazz'}
    assert run.pages.get(JAZZ)['last_listed_at']


def test_changed_listing_entry_refetches_the_detail_page():
    spider, request = spider_with(jazz_record(listing_hash='something else'))
    _, [detail] = split(spider.parse_listing(respond(request, JAZZ_LISTING)))
    assert detail.url == JAZZ and detail.meta['refetch'] == 'listing' and detail.meta['page_url'] == JAZZ


def test_old_detail_page_is_refetched():
    spider, request = spider_with(jazz_record(fetched_at=ago(8)))
    _, [detail] = split(spider.parse_listing(respond(request, JAZZ_LISTING)))
    assert detail.meta['refetch'] == 'max_age'


def test_parsed_detail_page_is_recorded():
    spider, request = spider_with()
    _, [detail] = split(spider.parse_listing(respond(request, JAZZ_LISTING)))
    page = ld(('Jazz', '2026-06-05T19:00', {'description': 'Live jazz all night.'}))
    [item] = list(spider.parse_event(respond(detail, html(page))))
    record = spider.tracker.for_request(detail).pages.get(JAZZ)
    assert record['listing_hash'] == page_state.listing_hash(detail.meta['partial'])
    assert record['listing_url'] == EVENTS and record['listing_data']['title'] == 'Jazz'
    assert record['extraction_version'] == extraction.EXTRACTION_VERSION and record['fetched_at']
    assert record['content_hash'] == page_state.content_hash([dict(item)])


def test_304_listing_refreshes_its_stale_detail_pages():
    spider, request = spider_with(jazz_record(fetched_at=ago(8)),
                                  {**jazz_record(), 'url': 'https://venue.test/events/fresh'})
    failure = _failure(request, IgnoreRequest('304 Not Modified: ' + EVENTS))
    [detail] = list(spider._errback(failure))
    assert detail.url == JAZZ and detail.meta['refetch'] == 'max_age'
    assert detail.meta['partial']['title'] == 'Jazz'


def test_sitemap_lastmod_is_read_before_listings_and_learned():
    spider = make_spider()
    spider.client = FakeClient([jazz_record()])
    [robots] = spider.entry_requests(source(effective_recipe={'events_urls': [EVENTS]}))
    assert robots.url == 'https://venue.test/robots.txt' and not robots.meta.get('playwright')
    _, [sm] = split(spider.parse_robots_sitemaps(respond(robots, 'Sitemap: https://venue.test/sm.xml',
                                                         cls=TextResponse)))
    body = (f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{JAZZ}/</loc>'
            f'<lastmod>{datetime.now(timezone.utc).isoformat()}</lastmod></url></urlset>')
    _, [listing] = split(spider.parse_lastmod(respond(sm, body, cls=TextResponse)))
    assert listing.url == EVENTS
    run = spider.tracker.for_request(listing)
    assert run.learned['sitemap_urls'] == ['https://venue.test/sm.xml']
    _, [detail] = split(spider.parse_listing(respond(listing, JAZZ_LISTING)))
    assert detail.meta['refetch'] == 'lastmod'


def test_missing_sitemap_is_learned_as_none():
    spider = make_spider()
    spider.client = FakeClient([jazz_record()])
    [robots] = spider.entry_requests(source(effective_recipe={'events_urls': [EVENTS]}))
    [fallback] = list(spider._lastmod_failed(_failure(robots)))
    assert fallback.url == 'https://venue.test/sitemap.xml'
    [listing] = list(spider._lastmod_failed(_failure(fallback)))
    assert listing.url == EVENTS
    assert spider.tracker.for_request(listing).learned['sitemap_urls'] == []


def test_nothing_to_skip_means_no_sitemap_fetch(spider):
    [request] = spider.entry_requests(source(effective_recipe={'events_urls': [EVENTS]}))
    assert request.url == EVENTS


def test_lastmod_that_missed_a_change_is_not_trusted():
    spider, request = spider_with(jazz_record(listing_hash='changed'))
    _, [detail] = split(spider.parse_listing(respond(request, JAZZ_LISTING)))
    run = spider.tracker.for_request(detail)
    from scraper.discovery import sitemap
    run.lastmod[sitemap.key(JAZZ)] = datetime.now(timezone.utc) - timedelta(days=3)
    list(spider.parse_event(respond(detail, html(ld(('Jazz', '2026-06-05T19:00', {}))))))
    assert run.learned['lastmod_trusted'] is False


# ── Claiming more sources ─────────────────────────────────────────────────────

class FakeEngine:
    def __init__(self):
        self.crawled = []

    def crawl(self, request):
        self.crawled.append(request)


def claiming_spider(batches, budget_minutes=30):
    spider = make_spider()
    spider.keep_claiming, spider.budget_seconds = True, budget_minutes * 60
    spider.client = FakeClient(batches=batches)
    spider.crawler = type('C', (), {'engine': FakeEngine()})()
    return spider


def test_idle_spider_claims_the_next_batch():
    spider = claiming_spider([[source(id='a', domain='a.test', homepage_url='https://a.test/')]])
    with pytest.raises(DontCloseSpider):
        spider._on_idle()
    assert [r.url for r in spider.crawler.engine.crawled] == ['https://a.test/']
    spider._on_idle()                                   # queue empty: let it close


def test_idle_spider_stops_claiming_after_its_budget():
    spider = claiming_spider([[source()]], budget_minutes=0)
    spider._on_idle()
    assert spider.crawler.engine.crawled == []


def test_idle_spider_without_keep_claiming_closes(spider):
    spider._on_idle()
