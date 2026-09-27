"""Tests for scraper/platforms — detection and feed parsing, offline."""
import json
from datetime import datetime, timedelta, timezone

from scrapy.http import HtmlResponse, TextResponse

from scraper import platforms
from scraper.platforms.ical import ICalFeed
from scraper.platforms.squarespace import Squarespace
from scraper.platforms.wordpress_tribe import WordPressTribe

URL = 'https://venue.test/'


def html(body: str, url: str = URL, head: str = '') -> HtmlResponse:
    return HtmlResponse(url, body=f'<html><head>{head}</head><body>{body}</body></html>'.encode(),
                        encoding='utf-8')


def feed(body, url='https://venue.test/feed', ctype=b'application/json') -> TextResponse:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return TextResponse(url, body=data, encoding='utf-8', headers={'Content-Type': ctype})


# ── Registry ──────────────────────────────────────────────────────────────────

def test_plain_page_matches_nothing():
    assert platforms.detect(html('<p>Hello, lots of ordinary text here.</p>')) is None


def test_non_html_is_never_detected():
    assert platforms.detect(feed({'events': []})) is None


# ── WordPress / The Events Calendar ───────────────────────────────────────────

def test_tribe_detect_and_feed_url():
    resp = html('<div class="tribe-events"></div>',
                head='<link rel="https://api.w.org/" href="https://venue.test/blog/wp-json/">')
    adapter = platforms.detect(resp)
    assert adapter.name == 'wordpress_tribe'
    assert adapter.feed_urls(resp) == [
        'https://venue.test/blog/wp-json/tribe/events/v1/events?per_page=50&start_date=now']


def test_tribe_parse_and_pagination():
    resp = feed({'events': [{
        'title': 'Jazz &amp; Blues', 'description': '<p>Live <b>jazz</b></p>',
        'url': 'https://venue.test/event/jazz/', 'start_date': '2026-06-05 19:00:00',
        'utc_start_date': '2026-06-05 23:00:00', 'utc_end_date': '2026-06-06 02:00:00',
        'timezone': 'America/New_York', 'image': {'url': 'https://venue.test/j.jpg'},
        'cost_details': {'values': ['15']},
        'venue': {'venue': 'Main Hall', 'address': '1 Main St', 'city': 'Cincinnati',
                  'state': 'OH', 'zip': '45202', 'geo_lat': '39.1', 'geo_lng': '-84.5'},
    }], 'next_rest_url': 'https://venue.test/wp-json/tribe/events/v1/events?page=2'})
    [event] = WordPressTribe().parse(resp)
    assert event['title'] == 'Jazz & Blues'
    assert event['description'] == 'Live jazz'
    assert event['start_datetime'] == '2026-06-05T23:00:00+00:00'
    assert event['location_address'] == '1 Main St, Cincinnati, OH, 45202'
    assert event['location_lat'] == 39.1 and event['ticket_price'] == '15'
    assert WordPressTribe().next_url(resp).endswith('page=2')


# ── Squarespace ───────────────────────────────────────────────────────────────

def test_squarespace_only_matches_events_collections():
    home = html('<script src="https://static1.squarespace.com/x.js"></script><p>Welcome</p>')
    events = html('<script src="https://static1.squarespace.com/x.js"></script>'
                  '<div class="eventlist eventlist--upcoming"></div>', URL + 'events')
    assert platforms.detect(home) is None
    assert platforms.detect(events).name == 'squarespace'
    assert Squarespace().feed_urls(events) == ['https://venue.test/events?format=json']


def test_squarespace_parse():
    start = int(datetime(2026, 6, 5, 23, tzinfo=timezone.utc).timestamp() * 1000)
    resp = feed({'upcoming': [{
        'title': 'Open Mic', 'fullUrl': '/events/open-mic', 'startDate': start,
        'excerpt': '<p>Bring a guitar</p>', 'assetUrl': 'https://img/x.jpg',
        'location': {'addressTitle': 'Back Room', 'addressLine1': '1 Main St',
                     'addressLine2': 'Cincinnati, OH', 'mapLat': 39.1, 'mapLng': -84.5},
    }, {'title': 'No Address', 'startDate': start, 'location': {'mapLat': 40.7, 'mapLng': -74.0}}]},
        url='https://venue.test/events?format=json')
    first, second = Squarespace().parse(resp)
    assert first['start_datetime'] == '2026-06-05T23:00:00+00:00'
    assert first['url'] == 'https://venue.test/events/open-mic'
    assert first['description'] == 'Bring a guitar'
    assert second['location_lat'] is None          # Squarespace's default pin


# ── iCal ──────────────────────────────────────────────────────────────────────

def test_google_calendar_embed_becomes_ics_feed():
    resp = html('<iframe src="https://calendar.google.com/calendar/embed?src=abc%40group.calendar.google.com&ctz=America/New_York"></iframe>')
    assert platforms.detect(resp).name == 'ical'
    assert ICalFeed().feed_urls(resp) == [
        'https://calendar.google.com/calendar/ical/abc%40group.calendar.google.com/public/basic.ics']


def test_webcal_subscribe_link():
    resp = html('<a href="webcal://venue.test/cal.ics">Subscribe</a>')
    assert ICalFeed().feed_urls(resp) == ['https://venue.test/cal.ics']


def test_many_per_event_ics_links_are_not_a_feed():
    resp = html(''.join(f'<a href="/e/{i}.ics">Add to calendar</a>' for i in range(5)))
    assert ICalFeed().feed_urls(resp) == []


def _ics(*vevents):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n' + ''.join(vevents) +
            'END:VCALENDAR\r\n').encode()


def _vevent(uid, summary, start, extra=''):
    return (f'BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART:{start}\r\n'
            f'{extra}END:VEVENT\r\n')


def test_ical_parse_upcoming_recurring_and_cancelled():
    soon = (datetime.now(timezone.utc) + timedelta(days=10)).strftime('%Y%m%dT%H%M%SZ')
    body = _ics(
        _vevent('1', 'Trivia', '20250102T190000Z', 'RRULE:FREQ=WEEKLY;BYDAY=TH\r\n'),
        _vevent('2', 'Big Gig', soon, 'LOCATION:Main Hall\r\nURL:https://venue.test/gig\r\n'),
        _vevent('3', 'Old Show', '20200101T190000Z'),
        _vevent('4', 'Called Off', soon, 'STATUS:CANCELLED\r\n'),
        _vevent('5', 'Monthly Jam', '20250107T200000Z', 'RRULE:FREQ=MONTHLY;BYDAY=1TU;UNTIL=20200101T000000Z\r\n'),
        _vevent('1b', 'Trivia (moved)', soon, 'RECURRENCE-ID:20250109T190000Z\r\n'),
    )
    events = ICalFeed().parse(feed(body, ctype=b'text/calendar'))
    by_title = {e['title']: e for e in events}
    assert set(by_title) == {'Trivia', 'Big Gig'}
    assert by_title['Trivia']['recurrence_freq'] == 'weekly'
    assert by_title['Trivia']['recurrence_byday'] == ['TH']
    assert by_title['Big Gig']['location_title'] == 'Main Hall'
    assert by_title['Big Gig']['extraction_method'] == 'ical'


def test_monthly_weekday_rule_sets_month_mode():
    body = _ics(_vevent('1', 'First Tuesday Jam', '20250107T200000Z',
                        'RRULE:FREQ=MONTHLY;BYDAY=1TU\r\n'))
    [event] = ICalFeed().parse(feed(body, ctype=b'text/calendar'))
    assert event['recurrence_byday'] == ['1TU'] and event['recurrence_month_mode'] == 'weekday'


# ── Link-out and render-only adapters ─────────────────────────────────────────

def test_eventbrite_organizer_link():
    resp = html('<a href="https://www.eventbrite.com/o/the-venue-123?aff=x">Tickets</a>')
    adapter = platforms.detect(resp)
    assert adapter.name == 'eventbrite'
    assert adapter.feed_urls(resp) == ['https://www.eventbrite.com/o/the-venue-123']
    assert adapter.parse(resp) is None


def test_single_eventbrite_ticket_link_is_not_a_calendar():
    assert platforms.detect(html('<a href="https://www.eventbrite.com/e/one-show-1">Buy</a>')) is None


def test_wix_is_render_only():
    resp = html('<p>x</p>', head='<meta name="generator" content="Wix.com Website Builder">')
    adapter = platforms.detect(resp)
    assert adapter.name == 'wix' and adapter.rerender_only


def test_empty_spa_shell_is_render_only():
    resp = html('<div id="root"></div><script src="/app.js"></script>')
    assert platforms.detect(resp).name == 'script_rendered'


def test_server_rendered_app_with_content_is_not_rerendered():
    resp = html('<div id="root"></div>' + '<p>' + 'Real content. ' * 40 + '</p>')
    assert platforms.detect(resp) is None
