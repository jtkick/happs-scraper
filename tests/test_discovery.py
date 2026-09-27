"""Tests for scraper/discovery — finding events pages and classifying listing links."""
from scrapy.http import HtmlResponse

from scraper.discovery import events_page, links

HOME = 'https://www.venue.test/'


def page(body: str, url: str = HOME) -> HtmlResponse:
    return HtmlResponse(url, body=f'<html><body>{body}</body></html>'.encode(), encoding='utf-8')


# ── events_page ───────────────────────────────────────────────────────────────

def test_nav_events_link_wins():
    resp = page('''<nav><a href="/about">About</a><a href="/whats-on">What's On</a>
                   <a href="/menu">Menu</a></nav><a href="/events/past">Past events</a>''')
    assert events_page.find_events_pages(resp) == ['https://www.venue.test/whats-on']


def test_external_and_junk_links_are_ignored():
    resp = page('''<a href="https://eventbrite.test/events">Events</a><a href="mailto:x@y">Events</a>
                   <a href="/events.pdf">Events</a><a href="#events">Events</a>''')
    assert events_page.find_events_pages(resp) == []


def test_subdomains_count_as_the_same_site():
    resp = page('<a href="https://events.venue.test/calendar">Calendar</a>')
    assert events_page.find_events_pages(resp) == ['https://events.venue.test/calendar']


def test_listing_preferred_over_its_items():
    resp = page('''<a href="/events/jazz-night">Jazz Night — events</a>
                   <nav><a href="/events">Events</a></nav>''')
    assert events_page.find_events_pages(resp) == ['https://www.venue.test/events']


def test_sitemap_candidates():
    locs = ['https://venue.test/', 'https://venue.test/about', 'https://venue.test/calendar',
            'https://venue.test/calendar/2026-06-01-show']
    assert events_page.events_urls_from_sitemap(locs) == ['https://venue.test/calendar']


def test_site_of_handles_multi_part_suffixes():
    assert events_page.site_of('https://www.pub.co.uk/x') == 'pub.co.uk'


# ── links ─────────────────────────────────────────────────────────────────────

LISTING = 'https://venue.test/events'


def card(slug):
    return f'<div class="card"><h3><a href="/events/{slug}">{slug}</a></h3></div>'


def test_repeated_event_cards_are_detail_links():
    body = '<nav><a href="/a">A</a><a href="/b">B</a><a href="/c">C</a></nav>' + \
           ''.join(card(s) for s in ('jazz', 'trivia', 'karaoke'))
    found = links.classify(page(body, LISTING))
    assert found.details == [f'https://venue.test/events/{s}' for s in ('jazz', 'trivia', 'karaoke')]


def test_repeated_non_event_links_are_not_details():
    body = ''.join(f'<div class="post"><a href="/blog/{i}">Post {i}</a></div>' for i in range(5))
    assert links.classify(page(body, LISTING)).details == []


def test_rel_next_pagination():
    resp = page('<a rel="next" href="/events?page=2">2</a>', LISTING)
    assert links.classify(resp).next_page == 'https://venue.test/events?page=2'


def test_next_text_pagination():
    resp = page('<a class="x" href="/events/list/page/2/">Next Events »</a>', LISTING)
    assert links.classify(resp).next_page == 'https://venue.test/events/list/page/2/'


def test_incremented_page_param():
    resp = page('<a href="/events?page=3">3</a>', LISTING + '?page=2')
    assert links.classify(resp).next_page == 'https://venue.test/events?page=3'


def test_page_param_does_not_match_lookalikes():
    resp = page('<a href="/events?map=2">Map</a>', LISTING)
    assert links.classify(resp).next_page is None


def test_recipe_selectors_override_heuristics():
    body = '<ul><li class="e"><a href="/x/1">1</a></li></ul><a class="more" href="/p2">more stuff</a>'
    found = links.classify(page(body, LISTING), detail_css='li.e a::attr(href)',
                           pagination_css='a.more::attr(href)')
    assert found.details == ['https://venue.test/x/1']
    assert found.next_page == 'https://venue.test/p2'
