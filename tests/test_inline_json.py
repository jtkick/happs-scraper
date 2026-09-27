"""
Tests for the inline-JSON extractor.

This one carries the JS-heavy sites: Next.js/Nuxt page data and `var data = {}`
CMS dumps that JSON-LD and OpenGraph never see.
"""
import pytest

from scraper.extractors import inline_json

URL = 'https://x.test/event'

NEXT_DATA = '''<html><body><script id="__NEXT_DATA__" type="application/json">
{"props": {"pageProps": {"event": {
  "title": "Warehouse Rave",
  "startDate": "2026-09-12T22:00:00",
  "endDate": "2026-09-13T04:00:00",
  "description": "<p>All night <b>techno</b></p>",
  "venue": {"name": "The Warehouse"},
  "address": {"streetAddress": "12 Dock St", "city": "Baltimore", "state": "MD", "zip": "21201"},
  "latitude": "39.29",
  "longitude": -76.61,
  "price": "$15.00",
  "ticketUrl": "https://t.test/rave",
  "imageUrl": ["https://i.test/r.jpg"],
  "times": "Doors 10 p.m. to 4 a.m."
}}}}
</script></body></html>'''


# ── Next.js page data ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('field, expected', [
    ('title',            'Warehouse Rave'),
    ('start_datetime',   '2026-09-12T22:00:00'),
    ('end_datetime',     '2026-09-13T04:00:00'),
    ('location_title',   'The Warehouse'),
    ('location_address', '12 Dock St, Baltimore, MD, 21201'),
    ('location_lat',     39.29),
    ('location_lon',     -76.61),
    ('ticket_price',     15.0),
    ('ticket_url',       'https://t.test/rave'),
    ('image_url',        'https://i.test/r.jpg'),
])
def test_next_data_fields(field, expected):
    assert inline_json.extract(NEXT_DATA, URL)[field] == expected


def test_html_in_description_is_stripped():
    assert inline_json.extract(NEXT_DATA, URL)['description'] == 'All night techno'


def test_schedule_text_captured_for_dates_extractor():
    """_schedule_text is a private hint for dates.py, not an EventItem field."""
    assert inline_json.extract(NEXT_DATA, URL)['_schedule_text'] == 'Doors 10 p.m. to 4 a.m.'


# ── Assignment patterns ───────────────────────────────────────────────────────

@pytest.mark.parametrize('assignment', [
    'var data = {"eventName": "Farmers Market", "dateStart": "2026-05-01T08:00:00", '
    '"dateEnd": "2026-05-01T13:00:00", "venueName": "Town Square"};',
    'window.__INITIAL_STATE__ = {"eventName": "Farmers Market", "dateStart": "2026-05-01T08:00:00", '
    '"dateEnd": "2026-05-01T13:00:00", "venueName": "Town Square"};',
], ids=['var-assignment', 'window-assignment'])
def test_assignment_patterns(assignment):
    html = f'<html><body><script>{assignment}</script></body></html>'
    result = inline_json.extract(html, URL)
    assert result['title'] == 'Farmers Market'
    assert result['start_datetime'] == '2026-05-01T08:00:00'
    assert result['location_title'] == 'Town Square'


def test_braces_inside_strings_do_not_break_balancing():
    html = '''<html><body><script>
    var data = {"eventName": "Party {with} braces", "dateStart": "2026-05-01T08:00:00",
                "venueName": "Hall", "description": "a \\" quote and a }"};
    </script></body></html>'''
    assert inline_json.extract(html, URL)['title'] == 'Party {with} braces'


def test_nested_event_found_in_deep_structure():
    html = '''<html><body><script type="application/json">
    {"a": {"b": {"c": [{"irrelevant": 1}, {"name": "Buried Gig",
     "startDate": "2026-05-01T20:00:00", "venue": "Basement", "price": 5}]}}}
    </script></body></html>'''
    assert inline_json.extract(html, URL)['title'] == 'Buried Gig'


def test_highest_scoring_object_wins():
    """A richer event object should beat a sparser one on the same page."""
    html = '''<html><body><script type="application/json">
    {"teaser": {"name": "Sparse", "startDate": "2026-05-01T20:00:00", "venue": "X"},
     "main": {"name": "Rich", "startDate": "2026-05-02T20:00:00", "endDate": "2026-05-02T23:00:00",
              "venue": "Y", "address": "1 Main St", "price": 10}}
    </script></body></html>'''
    assert inline_json.extract(html, URL)['title'] == 'Rich'


# ── Coercion ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    (10,        10.0),
    ('10',      10.0),
    ('$10.50',  10.5),
    ('Free',    None),
])
def test_price_coercion(raw, expected):
    price = raw if isinstance(raw, (int, float)) else f'"{raw}"'
    html = f'''<html><body><script type="application/json">
    {{"name": "Gig", "startDate": "2026-05-01T20:00:00", "venue": "Hall", "price": {price}}}
    </script></body></html>'''
    assert inline_json.extract(html, URL).get('ticket_price') == expected


def test_non_numeric_coordinates_are_dropped():
    html = '''<html><body><script type="application/json">
    {"name": "Gig", "startDate": "2026-05-01T20:00:00", "venue": "Hall",
     "address": "1 Main St", "latitude": "not-a-number"}
    </script></body></html>'''
    assert inline_json.extract(html, URL).get('location_lat') is None


# ── Negative cases ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('html', [
    '<html><body>no scripts here</body></html>',
    '<html><body><script>var d = {broken,,};</script></body></html>',
    '<html><body><script>var x = {"title": "Hi", "url": "/a"};</script></body></html>',
    '<html><body><script></script></body></html>',
], ids=['no-script', 'invalid-json', 'below-min-score', 'empty-script'])
def test_returns_none_when_nothing_event_like(html):
    assert inline_json.extract(html, URL) is None
