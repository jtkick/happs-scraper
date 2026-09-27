"""Tests for the JSON-LD extractor — the first and cheapest step of the waterfall."""
import pytest

from scraper.extractors import jsonld

URL = 'https://venue.test/jazz'


def _page(ld: str) -> str:
    return f'<html><head><script type="application/ld+json">{ld}</script></head><body></body></html>'


FULL_EVENT = _page('''{
  "@context": "https://schema.org",
  "@type": "Event",
  "name": "Jazz Night",
  "description": "An evening of jazz.",
  "startDate": "2026-06-05T19:00:00-05:00",
  "endDate": "2026-06-05T22:00:00-05:00",
  "location": {
    "@type": "Place",
    "name": "Blues Alley",
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "1073 Wisconsin Ave NW",
      "addressLocality": "Washington",
      "addressRegion": "DC",
      "postalCode": "20007"
    }
  },
  "offers": {"@type": "Offer", "price": "25.00", "url": "https://tix.test/jazz"},
  "image": ["https://img.test/jazz.jpg"],
  "url": "https://venue.test/jazz"
}''')


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('field, expected', [
    ('title',            'Jazz Night'),
    ('description',      'An evening of jazz.'),
    ('start_datetime',   '2026-06-05T19:00:00-05:00'),
    ('end_datetime',     '2026-06-05T22:00:00-05:00'),
    ('location_title',   'Blues Alley'),
    ('location_address', '1073 Wisconsin Ave NW, Washington, DC, 20007'),
    ('ticket_price',     25.0),
    ('ticket_url',       'https://tix.test/jazz'),
    ('url',              'https://venue.test/jazz'),
    ('image_url',        'https://img.test/jazz.jpg'),
])
def test_full_event_fields(field, expected):
    assert jsonld.extract(FULL_EVENT, URL)[field] == expected


# ── Shape variations ──────────────────────────────────────────────────────────

def test_graph_container_yields_all_events():
    html = _page('''{
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "WebPage", "name": "Calendar"},
        {"@type": "MusicEvent", "name": "Show A", "startDate": "2026-07-01T20:00:00"},
        {"@type": "Event", "name": "Show B", "startDate": "2026-07-02T20:00:00"}
      ]
    }''')
    assert [e['title'] for e in jsonld.extract_all(html, URL)] == ['Show A', 'Show B']


def test_extract_returns_first_of_many():
    html = _page('''{"@context": "https://schema.org", "@graph": [
        {"@type": "Event", "name": "Show A", "startDate": "2026-07-01T20:00:00"},
        {"@type": "Event", "name": "Show B", "startDate": "2026-07-02T20:00:00"}]}''')
    assert jsonld.extract(html, URL)['title'] == 'Show A'


def test_event_subtypes_are_recognised():
    """MusicEvent, TheaterEvent etc. all contain 'Event' and must be picked up."""
    html = _page('{"@type": "TheaterEvent", "name": "Hamlet", "startDate": "2026-07-01T20:00:00"}')
    assert jsonld.extract(html, URL)['title'] == 'Hamlet'


def test_type_given_as_list():
    html = _page('{"@type": ["Event", "MusicEvent"], "name": "Gig", "startDate": "2026-07-01T20:00:00"}')
    assert jsonld.extract(html, URL)['title'] == 'Gig'


def test_location_as_list_uses_first():
    html = _page('''{"@type": "Event", "name": "Gig", "startDate": "2026-07-01T20:00:00",
      "location": [{"@type": "Place", "name": "Main Stage"}, {"@type": "Place", "name": "Annex"}]}''')
    assert jsonld.extract(html, URL)['location_title'] == 'Main Stage'


def test_plain_string_address():
    html = _page('''{"@type": "Event", "name": "Gig", "startDate": "2026-07-01T20:00:00",
      "location": {"@type": "Place", "name": "Hall", "address": "101 Main St"}}''')
    assert jsonld.extract(html, URL)['location_address'] == '101 Main St'


def test_image_as_object():
    html = _page('''{"@type": "Event", "name": "Gig", "startDate": "2026-07-01T20:00:00",
      "image": {"@type": "ImageObject", "url": "https://img.test/g.jpg"}}''')
    assert jsonld.extract(html, URL)['image_url'] == 'https://img.test/g.jpg'


def test_offers_low_price_fallback():
    html = _page('''{"@type": "Event", "name": "Gig", "startDate": "2026-07-01T20:00:00",
      "offers": {"@type": "AggregateOffer", "lowPrice": "12.50"}}''')
    assert jsonld.extract(html, URL)['ticket_price'] == 12.5


def test_offers_as_list_uses_first():
    html = _page('''{"@type": "Event", "name": "Gig", "startDate": "2026-07-01T20:00:00",
      "offers": [{"price": "10"}, {"price": "20"}]}''')
    assert jsonld.extract(html, URL)['ticket_price'] == 10.0


def test_value_wrapper_objects():
    """JSON-LD values may arrive as {'@value': ...} rather than bare strings."""
    html = _page('{"@type": "Event", "name": {"@value": "Wrapped"}, "startDate": "2026-07-01T20:00:00"}')
    assert jsonld.extract(html, URL)['title'] == 'Wrapped'


# ── Negative cases ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('html', [
    '<html><body>no structured data</body></html>',
    _page('{not valid json'),
    _page('{"@type": "Organization", "name": "Some Venue"}'),
    _page('{"@type": "Event"}'),
], ids=['no-script', 'malformed-json', 'non-event-type', 'empty-event'])
def test_returns_none_when_no_event(html):
    assert jsonld.extract(html, URL) is None


def test_extract_all_returns_empty_list_not_none():
    assert jsonld.extract_all('<html></html>', URL) == []


def test_non_numeric_price_is_none():
    html = _page('''{"@type": "Event", "name": "Gig", "startDate": "2026-07-01T20:00:00",
      "offers": {"price": "Donations welcome"}}''')
    assert jsonld.extract(html, URL)['ticket_price'] is None


def test_geo_coordinates_are_read():
    html = _page('''{"@type": "Event", "name": "Show", "startDate": "2026-06-01",
      "location": {"@type": "Place", "name": "Hall",
                   "geo": {"@type": "GeoCoordinates", "latitude": "39.1", "longitude": -84.5}}}''')
    event = jsonld.extract(html, URL)
    assert (event['location_lat'], event['location_lon']) == (39.1, -84.5)


def test_microdata_events_are_found():
    html = '''<html><body>
      <div itemscope itemtype="https://schema.org/Event">
        <span itemprop="name">Microdata Show</span>
        <meta itemprop="startDate" content="2026-06-01T20:00">
      </div></body></html>'''
    assert jsonld.extract(html, URL)['title'] == 'Microdata Show'


def test_duplicate_events_across_syntaxes_are_merged():
    ld = _page('{"@type": "Event", "name": "Dup", "startDate": "2026-06-01"}')
    md = ('<div itemscope itemtype="https://schema.org/Event"><span itemprop="name">Dup</span>'
          '<meta itemprop="startDate" content="2026-06-01"></div>')
    assert len(jsonld.extract_all(ld.replace('</body>', md + '</body>'), URL)) == 1
