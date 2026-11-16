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


def test_a_post_pager_s_dates_are_other_events():
    text = ('Redtree Wine Party\nSaturday, November 7, 2026\n6:00 PM 11:00 PM\nCome sip and snack.\n'
            'Previous\nPrevious\nMarch 7\nKaleidoscope\nNext\nNext\nJune 13\nBeyond Color')
    data = extraction.finalize({'title': 'Redtree Wine Party', 'start_datetime': '2026-11-07T18:00:00'},
                               page_text=text)
    assert not data.get('rdates')


def test_a_day_inside_the_event_s_own_run_is_not_another_occurrence():
    text = 'Oct 31, 2026, 4:00 PM – Nov 01, 2026, 2:00 AM'
    data = extraction.finalize({'title': 'Halloween Party', 'start_datetime': '2026-10-31T16:00:00-04:00',
                                'end_datetime': '2026-11-01T02:00:00-05:00', 'timezone': 'America/New_York'},
                               page_text=text)
    assert not data.get('rdates')


def test_an_extra_date_without_a_time_takes_the_start_time():
    data = extraction.finalize({'title': 'Show', 'start_datetime': '2026-06-05T19:30:00'},
                               page_text='Also on June 12, 2026 and June 19, 2026.')
    assert data['rdates'] and all(r.endswith('T19:30:00') for r in data['rdates'])


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


# ── Titles ────────────────────────────────────────────────────────────────────

def test_a_soft_404_keeps_the_listing_entry():
    from scrapy.http import HtmlResponse
    partial = {'title': 'Adult Archery', 'start_datetime': '2026-11-01T14:00:00-04:00',
               'url': 'https://venue.test/event/archery/'}
    page = ('<html><head><meta property="og:title" content="Page not found - Venue">'
            '<meta property="og:description" content="Sorry."></head><body></body></html>')
    response = HtmlResponse(url=partial['url'], body=page.encode(), encoding='utf-8')
    _, [event] = extraction.parse_page(response, kind='detail', partial=partial)
    assert event['title'] == 'Adult Archery' and not event.get('description')


def test_an_opengraph_title_with_nothing_in_common_yields_to_the_listing_s():
    data = extraction.finalize({'title': 'Book Your Experience Today', 'extraction_method': 'opengraph'},
                               partial={'title': 'Miracle at the Overlook', 'start_datetime': '2026-11-15'})
    assert data['title'] == 'Miracle at the Overlook'
    data = extraction.finalize({'title': 'Miracle | Overlook Lodge', 'extraction_method': 'opengraph'},
                               partial={'title': 'Miracle at the Overlook', 'start_datetime': '2026-11-15'})
    assert data['title'] == 'Miracle | Overlook Lodge'


def test_a_listing_s_one_model_event_beats_the_page_s_opengraph():
    from scrapy.http import HtmlResponse
    page = ('<html><head><meta property="og:title" content="Events | The ABB">'
            '<meta property="og:description" content="We take part in events every year.">'
            '<meta property="og:image" content="https://venue.test/i.jpg"></head><body></body></html>')
    response = HtmlResponse(url='https://venue.test/events', body=page.encode(), encoding='utf-8')
    ai = lambda html, url: AIResult(events=[{'title': 'Art of the Chase', 'start_datetime': '2026-09-26',
                                             'description': 'A photography show.', 'evidence': 'x'}])
    _, [event] = extraction.parse_page(response, kind='listing', ai=ai)
    assert event['title'] == 'Art of the Chase' and event['description'] == 'A photography show.'
    assert event['image_url'] == 'https://venue.test/i.jpg' and event['extraction_method'] == 'ai'


def test_structured_data_on_another_day_than_the_page_and_listing_loses():
    text = 'Fright Night Kickoff\nSat, Oct 31\nJoin us this Halloween!\nStarts\n12:00 PM'
    data = extraction.finalize(
        {'title': 'Fright Night Kickoff', 'start_datetime': '2026-10-28T12:00:00-04:00',
         'end_datetime': '2026-10-31T00:00:00-04:00', 'extraction_method': 'jsonld'},
        partial={'title': 'Fright Night Kickoff', 'start_datetime': '2026-10-31T12:00:00-04:00'}, page_text=text)
    assert data['start_datetime'] == '2026-10-31T12:00:00-04:00' and not data.get('end_datetime')


# ── Sessions of a series ──────────────────────────────────────────────────────

@pytest.mark.parametrize('title, session', [
    ('First Friday November 6, 2026', True),
    ('2nd Sunday Showtime: COMER, RONSTADT & FEIST', True),
    ('Second Sunday Workshops: Gratitude Trees', True),
    ('First Friday Art Walk', False),
    ('Trivia: Every Tuesday', False),
    ('Young Adult Lab: Paper Portraits', False),
    ('Every Friday through Dec 18', False),
])
def test_session_titles(title, session):
    assert extraction._session_title(title) is session


def test_a_dated_session_doesn_t_take_the_series_schedule():
    data = extraction.finalize({'title': 'First Friday November 6, 2026', 'start_datetime': '2026-11-06T17:00:00',
                                'description': 'Studios are open the first Friday of every month.'})
    assert data.get('recurrence_freq') in (None, 'none')


def test_a_listing_s_dated_sessions_of_one_series_are_one_offs():
    blurb = 'Join us every Thursday for Young Adult Lab.'
    result = extraction.PageResult(single=False, events=[
        ({'title': f'Young Adult Lab: {theme}', 'start_datetime': day, 'description': blurb}, None)
        for theme, day in (('Paper Portraits', '2026-10-01T16:00:00'), ('Glow Face Paint', '2026-10-08T16:00:00'))
    ] + [({'title': 'Trivia', 'start_datetime': '2026-10-06T20:00:00', 'description': 'Every Tuesday.'}, None)])
    events = extraction.finalize_page(result, '<html></html>')
    assert [e['recurrence_freq'] for e in events] == ['none', 'none', 'weekly']


# ── Ticket links ──────────────────────────────────────────────────────────────

def _page(body, url='https://venue.test/shows/mockball'):
    from scrapy.http import HtmlResponse
    return HtmlResponse(url=url, body=f'<html><body>{body}</body></html>'.encode(), encoding='utf-8')


def test_the_event_page_s_first_ticket_link_outside_the_site_chrome():
    page = _page('<header><a href="https://box.cincyticket.com">Buy Tickets</a></header>'
                 '<h1>Mockball</h1><a href="https://www.cincyticket.com/e.asp?evt=7894">BUY TICKETS</a>'
                 '<aside><a href="https://www.cincyticket.com/e.asp?evt=7724">BUY TICKETS</a></aside>')
    assert extraction.ticket_link(page, 'Mockball') == 'https://www.cincyticket.com/e.asp?evt=7894'


@pytest.mark.parametrize('body', [
    '<a href="/events">Tickets</a>',                                                    # the site's own menu
    '<a href="https://events.humanitix.com/rumble-at-venue-nightmare-on-elm">Get Tickets</a>',  # another event
    '<a href="https://www.eventbrite.com/e/mockball-tickets-1">Share</a>',              # doesn't say tickets
])
def test_links_that_aren_t_this_event_s_tickets(body):
    assert extraction.ticket_link(_page(body), 'Mockball') is None


def test_a_page_s_ticket_link_replaces_a_listing_link_back_into_the_site():
    page = _page('<a href="https://www.eventbrite.com/e/mockball-tickets-1">Tickets (opens in a new tab)</a>')
    partial = {'title': 'Mockball', 'start_datetime': '2026-10-30T18:00', 'url': page.url,
               'ticket_url': 'https://venue.test/shows'}
    _, [event] = extraction.parse_page(page, kind='detail', partial=partial)
    assert event['ticket_url'] == 'https://www.eventbrite.com/e/mockball-tickets-1'


def test_midnight_to_23_59_is_the_day():
    data = extraction.finalize({'title': 'Thanksgiving Buffet', 'start_datetime': '2026-11-26T00:00:00+00:00',
                                'end_datetime': '2026-11-26T23:59:59+00:00'})
    assert (data['start_datetime'], data['end_datetime']) == ('2026-11-26', '2026-11-26')


@pytest.mark.parametrize('text, start, end', [
    ('Murder Mystery\nFebruary 26, 2027 07:00 PM until February 26, 2027 10:00 PM\nBecome immersed…',
     '2027-02-26T19:00:00', '2027-02-26T22:00:00'),
    ('First Friday\nNovember 6, 2026\nStudios are open from 5-9pm and the next day 11am-3pm.',
     '2026-11-06T17:00:00', '2026-11-06T21:00:00'),
])
def test_a_date_only_start_takes_the_time_the_page_states(text, start, end):
    title = text.split('\n')[0]
    data = extraction.finalize({'title': title, 'start_datetime': start[:10], 'end_datetime': start[:10]},
                               page_text=text)
    assert (data['start_datetime'], data['end_datetime']) == (start, end)


def test_a_team_schedule_s_road_games_are_dropped():
    result = extraction.PageResult(single=False, events=[
        ({'title': title, 'start_datetime': day}, None) for title, day in (
            ('Cincinnati vs. Utah', '2026-10-31T15:45:00'), ('Cincinnati at BYU', '2026-11-28T12:00:00'),
            ('Jazz at the Park', '2026-11-29T19:00:00'))])
    events = extraction.finalize_page(result, '<html></html>')
    assert [e.get('drop_reason') for e in events] == [None, 'away_game', None]
