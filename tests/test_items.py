"""Tests for scraper/items.py — the event fields and event_item."""
from scraper.items import EVENT_FIELDS, PAYLOAD_FIELDS, EventItem, default, event_item


def test_every_listed_field_is_an_item_field():
    assert set(EVENT_FIELDS) | set(PAYLOAD_FIELDS) <= set(EventItem.fields)


def test_event_item_keeps_only_event_fields():
    item = event_item({'title': 'Gig', '_schedule_text': 'Fri 8pm', 'bogus': 1},
                      source_url='https://v.test/e', source_id='src-1')
    assert item['title'] == 'Gig' and item['source_id'] == 'src-1'
    assert item['extraction_method'] == 'unknown' and item['recurrence_freq'] is None
    assert '_schedule_text' not in item and 'bogus' not in item


def test_defaults_are_fresh_copies():
    first = default('recurrence_byday')
    first.append('MO')
    assert default('recurrence_byday') == [] and default('rdates') == [] and default('title') is None
