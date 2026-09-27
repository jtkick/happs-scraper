"""
Tests for BaseEventSpider — the extractor waterfall and the context system.

Responses are built in-process; nothing here makes a request.
"""
import pytest
from scrapy.http import HtmlResponse, Request
from scrapy.settings import Settings

from scraper.spiders.base import BaseEventSpider


def make_spider(cls=BaseEventSpider, **settings):
    """
    Build a spider outside a crawl.

    Scrapy normally attaches `.settings` when the crawler instantiates the
    spider; the AI fallback reads ANTHROPIC_API_KEY from it, so tests supply
    an empty settings object to keep the waterfall offline.
    """
    spider = cls(name=cls.name if cls is not BaseEventSpider else 'test')
    spider.settings = Settings({'ANTHROPIC_API_KEY': '', **settings})
    return spider


@pytest.fixture
def spider():
    return make_spider()


def response(html: str, url: str = 'https://x.test/e', context: dict | None = None):
    request = Request(url, meta={'context': context} if context is not None else {})
    return HtmlResponse(url, body=html.encode(), encoding='utf-8', request=request)


def jsonld_page(body: str) -> str:
    return f'<html><head><script type="application/ld+json">{body}</script></head><body></body></html>'


EVENT_PAGE = jsonld_page('''{
  "@type": "Event", "name": "Trivia Night",
  "startDate": "2026-06-04T19:00:00-04:00",
  "description": "Pub quiz with prizes."
}''')


def one_item(spider, resp):
    items = list(spider.parse_event(resp))
    assert len(items) == 1, f'expected exactly one item, got {len(items)}'
    return items[0]


# ── Waterfall precedence ──────────────────────────────────────────────────────

def test_jsonld_is_used_first(spider):
    item = one_item(spider, response(EVENT_PAGE))
    assert item['title'] == 'Trivia Night'
    assert item['extraction_method'] == 'jsonld'


def test_opengraph_used_when_jsonld_absent(spider):
    html = ('<html><head>'
            '<meta property="og:title" content="Comedy Showcase"/>'
            '<meta property="og:start_time" content="2026-08-01T20:00:00-04:00"/>'
            '</head></html>')
    item = one_item(spider, response(html))
    assert item['title'] == 'Comedy Showcase'
    assert item['extraction_method'] == 'opengraph'


def test_selectors_used_when_structured_data_absent():
    class SelectorSpider(BaseEventSpider):
        name = 'selectors'
        event_selectors = {
            'title': 'h1.event-title::text',
            'start_datetime': 'time.start::attr(datetime)',
        }

    html = ('<html><body><h1 class="event-title"> Karaoke Night </h1>'
            '<time class="start" datetime="2026-06-04T21:00:00-04:00"></time></body></html>')
    item = one_item(make_spider(SelectorSpider), response(html))
    assert item['title'] == 'Karaoke Night'
    assert item['extraction_method'] == 'selectors'


def test_jsonld_beats_opengraph_on_the_same_page(spider):
    html = EVENT_PAGE.replace(
        '<body>', '<body><meta property="og:title" content="Wrong Title"/>')
    assert one_item(spider, response(html))['title'] == 'Trivia Night'


def test_inline_json_supplements_structured_data(spider):
    """inline_json always runs, so it can fill fields JSON-LD left blank."""
    html = EVENT_PAGE.replace('</head>', '''<script type="application/json">
      {"name": "Trivia Night", "startDate": "2026-06-04T19:00:00-04:00",
       "venue": "The Rusty Tap", "address": "1 Main St", "price": 5}
      </script></head>''')
    item = one_item(spider, response(html))
    assert item['location_title'] == 'The Rusty Tap'
    assert item['ticket_price'] == 5.0


def test_longer_description_from_inline_json_wins(spider):
    html = EVENT_PAGE.replace('</head>', '''<script type="application/json">
      {"name": "Trivia Night", "startDate": "2026-06-04T19:00:00-04:00",
       "venue": "The Rusty Tap", "description": "Pub quiz with prizes, and a cash bar all night."}
      </script></head>''')
    assert one_item(spider, response(html))['description'].endswith('cash bar all night.')


def test_page_without_a_title_yields_nothing(spider):
    assert list(spider.parse_event(response('<html><body>nothing here</body></html>'))) == []


# ── Inherited context ─────────────────────────────────────────────────────────

def test_context_fills_gaps(spider):
    ctx = {'location_title': 'The Rusty Tap', 'location_lat': 39.0, 'location_lon': -76.6}
    item = one_item(spider, response(EVENT_PAGE, context=ctx))
    assert item['location_title'] == 'The Rusty Tap'
    assert (item['location_lat'], item['location_lon']) == (39.0, -76.6)


def test_page_data_outranks_context(spider):
    html = jsonld_page('''{"@type": "Event", "name": "Trivia Night",
      "startDate": "2026-06-04T19:00:00-04:00",
      "location": {"@type": "Place", "name": "Back Room"}}''')
    item = one_item(spider, response(html, context={'location_title': 'The Rusty Tap'}))
    assert item['location_title'] == 'Back Room'


@pytest.mark.xfail(
    strict=True,
    reason="extract_page_context is documented as 'Do NOT return start_datetime "
           "or title', but parse_event does not enforce it: a context title is "
           "merged in and emitted, so a mis-written spider labels every event on "
           "the site with the venue name instead of failing loudly. "
           "NormalizePipeline drops the item later only because it has no date.",
)
def test_context_never_supplies_a_title(spider):
    """A venue-level title would label every event on the site identically."""
    resp = response('<html><body>nothing</body></html>', context={'title': 'The Rusty Tap'})
    assert list(spider.parse_event(resp)) == []


def test_missing_context_key_is_harmless(spider):
    assert one_item(spider, response(EVENT_PAGE))['location_title'] is None


# ── Unconditional passes ──────────────────────────────────────────────────────

def test_recurrence_is_extracted_from_description(spider):
    html = jsonld_page('''{"@type": "Event", "name": "Trivia Night",
      "startDate": "2026-06-04T19:00:00-04:00",
      "description": "Every Thursday. Pub quiz with prizes."}''')
    item = one_item(spider, response(html))
    assert item['recurrence_freq'] == 'weekly'
    assert item['recurrence_byday'] == ['TH']


def test_tags_matched_from_text(spider):
    assert 'Trivia' in one_item(spider, response(EVENT_PAGE))['tag_names']


def test_tags_inferred_from_venue_context(spider):
    item = one_item(spider, response(EVENT_PAGE, context={'venue_type': 'pub'}))
    assert '21+' in item['tag_names']


def test_schedule_text_is_not_emitted_on_the_item(spider):
    """_schedule_text is an internal hint and must not reach the API payload."""
    html = jsonld_page('''{"@type": "Event", "name": "Gig",
      "startDate": "2026-06-04T19:00:00-04:00"}''').replace(
        '</head>', '''<script type="application/json">
        {"name": "Gig", "startDate": "2026-06-04T19:00:00-04:00", "venue": "Hall",
         "address": "1 Main St", "times": "June 5: 6 to 10 p.m.; June 6: noon to 10 p.m."}
        </script></head>''')
    assert '_schedule_text' not in dict(one_item(spider, response(html)))


# ── Listing pages ─────────────────────────────────────────────────────────────

class ListingSpider(BaseEventSpider):
    name = 'listing'
    listing_link_css = 'a.event::attr(href)'

    def extract_page_context(self, response):
        return {'location_title': (response.css('h1::text').get() or '').strip() or None}


LISTING_HTML = ('<html><body><h1>The Rusty Tap</h1>'
                '<a class="event" href="/events/1">One</a>'
                '<a class="event" href="/events/2">Two</a>'
                '<a class="other" href="/about">About</a>'
                '</body></html>')


def test_listing_follows_only_matching_links():
    spider = make_spider(ListingSpider)
    requests = list(spider.parse_listing(response(LISTING_HTML, url='https://x.test/events')))
    assert [r.url for r in requests] == ['https://x.test/events/1', 'https://x.test/events/2']


def test_listing_context_propagates_to_children():
    spider = make_spider(ListingSpider)
    requests = list(spider.parse_listing(response(LISTING_HTML, url='https://x.test/events')))
    assert all(r.meta['context']['location_title'] == 'The Rusty Tap' for r in requests)


def test_child_page_context_overrides_parent():
    spider = make_spider(ListingSpider)
    resp = response(LISTING_HTML, url='https://x.test/events',
                    context={'location_title': 'Stale Venue', 'location_lat': 39.0})
    request = next(iter(spider.parse_listing(resp)))
    assert request.meta['context']['location_title'] == 'The Rusty Tap'
    assert request.meta['context']['location_lat'] == 39.0   # inherited key survives


def test_intermediate_links_recurse_through_parse_listing():
    class TwoLevel(ListingSpider):
        name = 'two-level'

        def is_intermediate_page(self, url, response=None):
            return '/month/' in url

    html = ('<html><body><h1>Venue</h1>'
            '<a class="event" href="/month/june">June</a>'
            '<a class="event" href="/events/1">One</a></body></html>')
    requests = list(make_spider(TwoLevel).parse_listing(response(html, url='https://x.test/')))
    callbacks = {r.url: r.callback.__name__ for r in requests}
    assert callbacks['https://x.test/month/june'] == 'parse_listing'
    assert callbacks['https://x.test/events/1'] == 'parse_event'


def test_listing_with_no_links_is_retried_as_an_event_page():
    spider = make_spider(ListingSpider)
    items = list(spider.parse_listing(response(EVENT_PAGE, url='https://x.test/e')))
    assert items and items[0]['title'] == 'Trivia Night'


def test_pagination_carries_context_forward():
    class Paged(ListingSpider):
        name = 'paged'

        def _next_page(self, response):
            return '/events?page=2'

    requests = list(make_spider(Paged).parse_listing(response(LISTING_HTML, url='https://x.test/events')))
    last = requests[-1]
    assert last.url == 'https://x.test/events?page=2'
    assert last.meta['context']['location_title'] == 'The Rusty Tap'


# ── Routing and seeds ─────────────────────────────────────────────────────────

def test_parse_routes_event_pages_to_parse_event(spider):
    items = list(spider.parse(response(EVENT_PAGE)))
    assert items[0]['title'] == 'Trivia Night'


def test_parse_routes_non_event_pages_to_parse_listing():
    spider = make_spider(ListingSpider)
    requests = list(spider.parse(response(LISTING_HTML, url='https://x.test/events')))
    assert [r.url for r in requests] == ['https://x.test/events/1', 'https://x.test/events/2']


def test_start_seeds_inject_venue_context():
    from scraper.seeds.overpass import VenueSeed

    class SeededSpider(BaseEventSpider):
        name = 'seeded'
        start_seeds = [VenueSeed(url='https://v.test', location_title='The Pub',
                                 location_lat=39.0, venue_type='pub')]

    [request] = list(make_spider(SeededSpider).start_requests())
    assert request.url == 'https://v.test'
    assert request.meta['context'] == {
        'location_title': 'The Pub', 'location_lat': 39.0, 'venue_type': 'pub',
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('data, sufficient', [
    ({'title': 'x', 'start_datetime': 'y'}, True),
    ({'title': 'x'},                        False),
    ({'start_datetime': 'y'},               False),
    ({},                                    False),
])
def test_sufficient(data, sufficient):
    assert BaseEventSpider._sufficient(data) is sufficient


def test_merge_does_not_clobber():
    base = {'title': 'Kept', 'description': None}
    BaseEventSpider._merge(base, {'title': 'Ignored', 'description': 'Added', 'url': None})
    assert base == {'title': 'Kept', 'description': 'Added'}


def test_merge_enriched_upgrades_only_longer_descriptions():
    base = {'description': 'Short blurb'}
    BaseEventSpider._merge_enriched(base, {'description': 'Tiny'})
    assert base['description'] == 'Short blurb'
    BaseEventSpider._merge_enriched(base, {'description': 'A considerably longer blurb'})
    assert base['description'] == 'A considerably longer blurb'
