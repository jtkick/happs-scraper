"""Tests for scraper/extraction.py — page → events, and per-event finalize."""
import json
from datetime import datetime

import pytest
import time_machine

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


# ── parse_page / parse_feed: one page as the crawl parses it ──────────────────

CARDS = ('<html><body><div class="card"><h3>Card A</h3><time>June 1, 2026</time></div>'
         '<div class="card"><h3>Card B</h3><time>June 2, 2026</time></div></body></html>')
CARD_RECIPE = {'item_css': 'div.card', 'fields': {'title': 'h3::text', 'date': 'time::text'}}


def page_response(html, url=URL):
    from scrapy.http import HtmlResponse
    return HtmlResponse(url, body=html.encode(), encoding='utf-8')


def test_listing_uses_the_recipe():
    result, events = extraction.parse_page(page_response(CARDS), kind='listing', recipe=CARD_RECIPE)
    assert result.strategy == 'recipe' and [e['title'] for e in events] == ['Card A', 'Card B']


def test_detail_page_ignores_the_listing_recipe():
    result, _ = extraction.parse_page(page_response(CARDS), kind='detail', recipe=CARD_RECIPE)
    assert result.strategy != 'recipe'


def test_detail_partial_fills_gaps_and_spares_the_ai():
    ai = fake_ai({'title': 'X', 'start_datetime': '2026-01-01'})
    partial = {'title': 'Card A', 'start_datetime': '2026-06-01T20:00'}
    _, [event] = extraction.parse_page(page_response('<html></html>'), kind='detail', partial=partial, ai=ai)
    assert event['title'] == 'Card A' and ai.calls == []


def test_listing_never_takes_a_partial():
    partial = {'title': 'Somebody else'}
    _, events = extraction.parse_page(page_response('<html></html>'), kind='listing', partial=partial)
    assert events == []


def test_feed_events_are_finalized_with_their_platform():
    class Feed:
        name = 'feed'

        def parse(self, response):
            return [{'title': 'Quiz', 'start_datetime': '2026-06-01T20:00'}, {'start_datetime': 'x'}]
    [event] = extraction.parse_feed(Feed(), page_response(''), context={'location_title': 'Hall'})
    assert event['extraction_method'] == 'platform:feed' and event['location_title'] == 'Hall'


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


# ── Starts from text ──────────────────────────────────────────────────────────

# Friday noon in New York.
FRIDAY = datetime(2026, 10, 9, 16, 0)
NY = 'America/New_York'


@time_machine.travel(FRIDAY, tick=False)
def test_single_date_and_show_time_from_the_page():
    text = 'POP-PUNK TRIBUTE FEST 2027\nSaturday, January 9\n6:30 doors, 7 show\n18+'
    data = extraction.finalize({'title': 'Pop-Punk Tribute Fest 2027', 'timezone': NY}, page_text=text)
    assert data['start_datetime'] == '2027-01-09T19:00:00'


@time_machine.travel(FRIDAY, tick=False)
def test_undated_recipe_start_becomes_the_next_occurrence():
    data = extraction.finalize({'title': 'The Social Groove', 'start_datetime': 'EVERY THU 8–11pm', 'timezone': NY})
    assert data['start_datetime'] == '2026-10-15T20:00:00'
    assert data['end_datetime'] == '2026-10-15T23:00:00'
    assert data['recurrence_freq'] == 'weekly' and data['recurrence_byday'] == ['TH']


@time_machine.travel(FRIDAY, tick=False)
def test_weekly_event_without_a_date_starts_on_its_next_day():
    data = extraction.finalize({'title': 'Trivia', 'description': 'Every Thursday. Teams of up to six, starts at 8.',
                                'timezone': NY})
    assert data['start_datetime'] == '2026-10-15T20:00:00'


@time_machine.travel(FRIDAY, tick=False)
def test_weekly_page_text_without_a_date():
    text = 'In Between the Curtains\nThursdays at 8\nA live blind dating show.'
    data = extraction.finalize({'title': 'In Between the Curtains', 'timezone': NY}, page_text=text, full_text=text)
    assert data['start_datetime'] == '2026-10-15T20:00:00' and data['recurrence_byday'] == ['TH']


@time_machine.travel(FRIDAY, tick=False)
def test_ai_evidence_gives_the_recurrence():
    data = extraction.finalize({'title': 'Evening Blues', 'start_datetime': '2026-10-14T19:00',
                                'evidence': 'EVERY WED EVENING BLUES Live blues. Doors at 7pm.'})
    assert data['recurrence_freq'] == 'weekly' and data['recurrence_byday'] == ['WE']
    assert data['start_datetime'] == '2026-10-14T19:00'


@time_machine.travel(FRIDAY, tick=False)
def test_a_title_naming_a_day_is_weekly_only_without_a_date():
    data = extraction.finalize({'title': 'Tasting Tuesdays', 'description': '5 pours for $15, 5–8pm.',
                                'timezone': NY})
    assert data['start_datetime'] == '2026-10-13T17:00:00' and data['recurrence_byday'] == ['TU']
    dated = extraction.finalize({'title': 'Tasting Tuesdays', 'start_datetime': '2026-10-13T17:00'})
    assert not dated.get('recurrence_freq')


@time_machine.travel(FRIDAY, tick=False)
def test_a_listing_that_comes_out_as_one_event_gets_no_guessed_start():
    html = ('<html><head><meta property="og:title" content="Events at Alcove"></head><body>'
            '<h1>Events at Alcove</h1><p>Brunch at Alcove every Saturday &amp; Sunday at 10</p>'
            '<p>Halloween Drag Brunch – October 25th 11AM-2PM</p></body></html>')
    _, [listing] = extraction.parse_page(page_response(html, URL), kind='listing')
    _, [detail] = extraction.parse_page(page_response(html, URL), kind='detail')
    assert not listing.get('start_datetime')
    assert detail['start_datetime']


LISTED = ('<body><h1>Upcoming Events</h1>'
          '<ul><li>Fundraiser Wed, Oct 21</li><li>Spells Sat, Oct 31</li></ul></body>')


@time_machine.travel(FRIDAY, tick=False)
def test_a_listing_that_comes_out_as_one_event_takes_no_dates_from_its_text():
    html = ('<html><head><meta property="og:title" content="Upcoming Events"></head>'
            f'{LISTED}</html>')
    _, [listing] = extraction.parse_page(page_response(html, URL), kind='listing')
    _, [detail] = extraction.parse_page(page_response(html, URL), kind='detail')
    assert not listing.get('start_datetime')
    assert detail['start_datetime']


def test_a_listing_asks_the_model_even_with_one_complete_event():
    ai = fake_ai({'title': 'Fundraiser', 'start_datetime': '2026-10-21T19:00'},
                 {'title': 'Spells', 'start_datetime': '2026-10-31T12:00'})
    html = ld_page(ev('Fundraiser', '2026-10-21T19:00')).replace('<body></body>', LISTED)
    result, events = extraction.parse_page(page_response(html, URL), kind='listing', ai=ai)
    assert result.strategy == 'ai' and [e['title'] for e in events] == ['Fundraiser', 'Spells']
    _, [detail] = extraction.parse_page(page_response(html, URL), kind='detail', ai=ai)
    assert detail['title'] == 'Fundraiser' and len(ai.calls) == 1


def test_undated_start_without_a_schedule_is_left_for_normalize():
    data = extraction.finalize({'title': 'Show', 'start_datetime': 'Tonight 8pm'})
    assert data['start_datetime'] == 'Tonight 8pm'


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
