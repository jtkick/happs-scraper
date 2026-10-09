"""Tests for scraper/extraction.py — page → events, and per-event finalize."""
import json

import pytest

from scraper import extraction
from scraper.extractors.ai import AIResult

URL = 'https://venue.test/events'


def ld_page(*nodes) -> str:
    blocks = ''.join(f'<script type="application/ld+json">{json.dumps(n)}</script>' for n in nodes)
    return f'<html><head>{blocks}</head><body></body></html>'


def ev(name, start, **extra):
    return {'@type': 'Event', 'name': name, 'startDate': start, **extra}


def fake_ai(*events, truncated=False):
    calls = []

    def run(html, url):
        calls.append(url)
        return AIResult(events=[dict(e) for e in events], truncated=truncated)
    run.calls = calls
    return run


# ── Listing pages ─────────────────────────────────────────────────────────────

def test_every_jsonld_event_on_a_listing_is_returned():
    html = ld_page(ev('Show A', '2026-06-01T20:00'), ev('Show B', '2026-06-02T20:00'))
    result = extraction.extract_page(html, URL)
    assert [d['title'] for d, _ in result.events] == ['Show A', 'Show B']
    assert not result.single and result.strategy == 'jsonld'
    assert all(d['extraction_method'] == 'jsonld' for d, _ in result.events)


def test_itemlist_wrapped_events_are_found():
    html = ld_page({'@type': 'ItemList', 'itemListElement': [
        {'@type': 'ListItem', 'item': ev('Show A', '2026-06-01')},
        {'@type': 'ListItem', 'item': ev('Show B', '2026-06-02')},
    ]})
    assert len(extraction.extract_page(html, URL).events) == 2


def test_inline_json_listing():
    data = {'events': [
        {'title': 'Show A', 'startDate': '2026-06-01T20:00', 'venue': 'Hall'},
        {'title': 'Show B', 'startDate': '2026-06-02T20:00', 'venue': 'Hall'},
        {'name': 'Hall', 'address': '1 Main St', 'lat': 1, 'lng': 2},  # venue, not an event
    ]}
    html = f'<html><script>window.__DATA__ = {json.dumps(data)};</script></html>'
    result = extraction.extract_page(html, URL)
    assert [d['title'] for d, _ in result.events] == ['Show A', 'Show B']
    assert result.strategy == 'inline_json'


def test_recipe_events_used_when_no_structured_data():
    recipe = [{'title': 'A', 'start_datetime': 'June 1'}, {'title': 'B', 'start_datetime': 'June 2'}]
    result = extraction.extract_page('<html></html>', URL, recipe_events=recipe)
    assert result.strategy == 'recipe' and len(result.events) == 2


def test_structured_data_beats_recipe_and_ai():
    ai = fake_ai({'title': 'X', 'start_datetime': '2026-01-01'})
    html = ld_page(ev('Show A', '2026-06-01'), ev('Show B', '2026-06-02'))
    result = extraction.extract_page(html, URL, recipe_events=[{'title': 'R'}] * 2, ai=ai)
    assert result.strategy == 'jsonld' and ai.calls == []


# ── Single-event pages and AI ─────────────────────────────────────────────────

def test_single_event_page_does_not_call_ai():
    ai = fake_ai()
    result = extraction.extract_page(ld_page(ev('Solo', '2026-06-01T20:00')), URL, ai=ai)
    assert result.single and len(result.events) == 1 and ai.calls == []


def test_ai_list_replaces_empty_page():
    ai = fake_ai({'title': 'A', 'start_datetime': '2026-06-01'},
                 {'title': 'B', 'start_datetime': '2026-06-02'}, truncated=True)
    result = extraction.extract_page('<html><p>text</p></html>', URL, ai=ai)
    assert result.strategy == 'ai' and not result.single
    assert result.ai_used and result.ai_truncated
    assert [d['extraction_method'] for d, _ in result.events] == ['ai', 'ai']


def test_single_ai_event_fills_gaps_of_partial_page():
    html = '<html><head><meta property="og:title" content="Jazz Night"></head></html>'
    ai = fake_ai({'title': 'Other', 'start_datetime': '2026-06-05T19:00', 'evidence': 'June 5 7pm'})
    [(data, _)] = extraction.extract_page(html, URL, ai=ai).events
    assert data['title'] == 'Jazz Night'
    assert data['start_datetime'] == '2026-06-05T19:00'


def test_unverified_ai_event_is_kept_for_drop_accounting():
    ai = fake_ai({'title': 'Ghost', 'start_datetime': '2026-06-05', 'drop_reason': 'ai_unverified'})
    [(data, _)] = extraction.extract_page('<html></html>', URL, ai=ai).events
    assert data['drop_reason'] == 'ai_unverified'


def test_unverified_ai_event_never_merges_into_real_data():
    html = '<html><head><meta property="og:title" content="Jazz Night"></head></html>'
    ai = fake_ai({'title': 'Ghost', 'start_datetime': '2026-06-05', 'drop_reason': 'ai_unverified'})
    [(data, _)] = extraction.extract_page(html, URL, ai=ai).events
    assert 'start_datetime' not in data and 'drop_reason' not in data


# ── finalize ──────────────────────────────────────────────────────────────────

def test_context_cannot_supply_per_event_fields():
    ctx = {'title': 'Venue', 'start_datetime': '2026-01-01', 'location_title': 'Venue'}
    assert extraction.finalize({}, context=ctx) is None
    data = extraction.finalize({'title': 'Show'}, context=ctx)
    assert data['location_title'] == 'Venue' and 'start_datetime' not in data


def test_partial_fills_gaps_but_page_wins():
    partial = {'title': 'Listing Title', 'start_datetime': '2026-06-01T20:00', 'image_url': 'x.jpg'}
    data = extraction.finalize({'title': 'Detail Title', 'description': 'Long'}, partial=partial)
    assert data['title'] == 'Detail Title'
    assert data['start_datetime'] == '2026-06-01T20:00'
    assert data['image_url'] == 'x.jpg'


def test_page_text_dates_only_apply_when_given():
    text = 'Runs June 5, June 12 and June 19, 2026 at 7 p.m.'
    listing = extraction.finalize({'title': 'Show', 'start_datetime': '2026-07-01'})
    detail = extraction.finalize({'title': 'Show', 'start_datetime': '2026-07-01'}, page_text=text)
    assert not listing.get('rdates')
    assert detail.get('rdates')


# ── Recurrence over a date span ───────────────────────────────────────────────

def test_recurring_run_becomes_until_and_first_day():
    data = extraction.finalize({'title': 'Biennial', 'start_datetime': '2026-09-30',
                                'end_datetime': '2026-10-31', '_schedule_text': 'Recurring daily'})
    assert data['recurrence_freq'] == 'daily'
    assert data['recurrence_until'] == '2026-10-31'
    assert data['end_datetime'] is None


def test_timed_run_keeps_the_end_time_on_the_first_day():
    data = extraction.finalize({'title': 'Exhibit', 'description': 'Open every Saturday.',
                                'start_datetime': '2026-09-05T10:00:00-04:00',
                                'end_datetime': '2026-12-19T17:00:00-04:00'})
    assert data['recurrence_until'] == '2026-12-19'
    assert data['end_datetime'] == '2026-09-05T17:00:00-04:00'


def test_span_is_left_alone_when_until_is_elsewhere():
    data = extraction.finalize({'title': 'Exhibit', 'description': 'Every Saturday until Nov 1, 2026.',
                                'start_datetime': '2026-09-05', 'end_datetime': '2026-12-19'})
    assert data['recurrence_until'] == '2026-11-01'
    assert data['end_datetime'] == '2026-12-19'


def test_one_off_spans_are_untouched():
    data = extraction.finalize({'title': 'Festival', 'start_datetime': '2026-10-08',
                                'end_datetime': '2026-10-11'})
    assert not data.get('recurrence_freq') and data['end_datetime'] == '2026-10-11'


def test_detail_page_recurrence_is_read_from_its_full_text():
    page = ld_page(ev('Trivia Night', '2026-10-06T19:00')).replace(
        '<body></body>', '<body><h1>Trivia Night</h1><ul><li>Recurring weekly on Tuesday</li></ul></body>')
    [data] = extraction.finalize_page(extraction.extract_page(page, URL), page)
    assert data['recurrence_freq'] == 'weekly' and data['recurrence_byday'] == ['TU']


def test_listing_pages_ignore_page_text_for_recurrence():
    page = ld_page(ev('A', '2026-10-06'), ev('B', '2026-10-07')).replace(
        '<body></body>', '<body><p>A</p><p>Recurring daily until October 31, 2026</p></body>')
    events = extraction.finalize_page(extraction.extract_page(page, URL), page)
    assert not any(e.get('recurrence_freq') for e in events)


# ── Detail pages adding information ───────────────────────────────────────────

def test_added_info():
    partial = {'title': 'Jazz', 'start_datetime': '2026-06-05', 'description': 'Live jazz…',
               'tag_names': [], 'extraction_method': 'jsonld'}
    same = {**partial, 'extraction_method': 'waterfall', 'tag_names': ['Music'], 'timezone': 'X'}
    assert not extraction.added_info(partial, same)
    assert extraction.added_info(partial, {**partial, 'location_address': '1 Main St'})
    assert extraction.added_info(partial, {**partial, 'description': 'Live jazz' + ' and more' * 10})
    assert extraction.added_info(partial, {**partial, 'recurrence_freq': 'weekly'})
    assert not extraction.added_info(partial, {**partial, 'recurrence_freq': 'none'})
    assert extraction.added_info({**partial, 'end_datetime': '2026-06-05'},
                                 {**partial, 'end_datetime': '2026-06-05T22:00'})


def test_thin_main_text_falls_back_to_all_text():
    shell = '<html><body><p>Loading</p><script>var data = {"recurrence": "Recurring daily", ' \
            '"name": "Show"}</script></body></html>'
    assert 'Recurring daily' in extraction.page_text(shell)


def test_span_shorter_than_the_repeat_is_one_occurrence():
    data = extraction.finalize({'title': 'BLINK', 'description': 'Four days of light, every other year.',
                                'start_datetime': '2026-10-08', 'end_datetime': '2026-10-11'})
    assert data['recurrence_freq'] == 'yearly' and data['recurrence_interval'] == 2
    assert data['end_datetime'] == '2026-10-11' and not data.get('recurrence_until')


# ── Merge helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('data, sufficient', [
    ({'title': 'x', 'start_datetime': 'y'}, True),
    ({'title': 'x'},                        False),
    ({'start_datetime': 'y'},               False),
    ({},                                    False),
])
def test_sufficient(data, sufficient):
    assert extraction.sufficient(data) is sufficient


def test_merge_does_not_clobber():
    base = {'title': 'Kept', 'description': None}
    extraction.merge(base, {'title': 'Ignored', 'description': 'Added', 'url': None})
    assert base == {'title': 'Kept', 'description': 'Added'}


def test_merge_enriched_upgrades_only_longer_descriptions():
    base = {'description': 'Short blurb'}
    extraction.merge_enriched(base, {'description': 'Tiny'})
    assert base['description'] == 'Short blurb'
    extraction.merge_enriched(base, {'description': 'A considerably longer blurb'})
    assert base['description'] == 'A considerably longer blurb'
