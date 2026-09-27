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
    body = '<html><head><meta name="generator" content="Wix.com"></head><body></body></html>'
    _, [again] = split(spider.parse_home(respond(home, body)))
    assert again.url == HOME and again.meta['playwright'] and again.callback == spider.parse_home
    assert spider.tracker.for_request(home).learned['render_js'] is True


# ── Listings ──────────────────────────────────────────────────────────────────

def listing_request(spider, **src):
    [request] = spider.entry_requests(source(effective_recipe={'events_urls': ['https://venue.test/events']}, **src))
    return request


def test_complete_listing_events_are_emitted_directly(spider):
    request = listing_request(spider)
    body = html(ld(('Jazz', '2026-06-05T19:00', {'description': 'Live jazz', 'url': '/events/jazz'}),
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
    try:
        raise ConnectionError('boom')
    except ConnectionError:
        fail = Failure()
    fail.request = detail
    [item] = list(spider._errback(fail))
    assert item['title'] == 'Jazz' and item['source_id'] == 'src-1'
    assert spider.tracker.for_request(detail).complete is False


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
