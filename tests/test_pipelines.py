"""
Tests for the item pipelines: 100 Normalize → 200 FingerprintDedup → 300 APISubmit.

Date assertions use timezone-aware input strings. Naive strings are resolved
against the *machine's* local timezone by dateparser, which is not stable
across dev machines and CI.
"""
import pytest
from scrapy.exceptions import DropItem

from scraper.items import EventItem
from scraper.pipelines import APISubmitPipeline, NormalizePipeline


@pytest.fixture
def normalize():
    return NormalizePipeline()


def _item(**fields) -> EventItem:
    item = EventItem()
    item.setdefault('title', 'Jazz Night')
    item.setdefault('start_datetime', '2026-06-05T19:00:00-05:00')
    for key, value in fields.items():
        item[key] = value
    return item


# ── 100 · Normalize: required fields ──────────────────────────────────────────

def test_missing_title_is_dropped(normalize):
    item = EventItem()
    item['start_datetime'] = '2026-06-05T19:00:00-05:00'
    with pytest.raises(DropItem, match='Missing title'):
        normalize.process_item(item, None)


def test_unparseable_start_is_dropped(normalize):
    with pytest.raises(DropItem, match='Unparseable start_datetime'):
        normalize.process_item(_item(start_datetime='sometime next week'), None)


def test_missing_start_is_dropped(normalize):
    item = EventItem()
    item['title'] = 'Jazz Night'
    with pytest.raises(DropItem):
        normalize.process_item(item, None)


# ── 100 · Normalize: text cleaning ────────────────────────────────────────────

def test_whitespace_is_collapsed(normalize):
    result = normalize.process_item(_item(description='  lots\n  of   space  '), None)
    assert result['description'] == 'lots of space'


def test_empty_strings_become_none(normalize):
    result = normalize.process_item(_item(description='   ', location_address=''), None)
    assert result['description'] is None
    assert result['location_address'] is None


def test_venue_suffix_stripped_from_title(normalize):
    result = normalize.process_item(
        _item(title='  Jazz   Night — Blues Alley ', location_title='Blues Alley'), None)
    assert result['title'] == 'Jazz Night'


def test_title_falls_back_to_raw_when_cleaners_empty_it(normalize):
    """clean_title returning None must not blank out the title."""
    result = normalize.process_item(_item(title='Blues Alley', location_title='Blues Alley'), None)
    assert result['title'] == 'Blues Alley'


# ── 100 · Normalize: dates ────────────────────────────────────────────────────

def test_offset_is_converted_to_utc(normalize):
    result = normalize.process_item(_item(start_datetime='2026-06-05T19:00:00-05:00'), None)
    assert result['start_datetime'] == '2026-06-06T00:00:00+00:00'


def test_datetime_objects_pass_through(normalize):
    from datetime import datetime
    result = normalize.process_item(_item(start_datetime=datetime(2026, 6, 5, 19, 0)), None)
    assert result['start_datetime'] == '2026-06-05T19:00:00'


def test_naive_dates_come_out_timezone_aware(normalize):
    result = normalize.process_item(_item(start_datetime='2026-06-05T19:00:00'), None)
    assert result['start_datetime'].endswith('+00:00')


def test_end_datetime_optional(normalize):
    assert normalize.process_item(_item(), None)['end_datetime'] is None


# ── 100 · Normalize: rdates / exdates ─────────────────────────────────────────

def test_rdates_are_parsed_and_bad_entries_dropped(normalize):
    result = normalize.process_item(
        _item(rdates=['2026-06-12T19:00:00-05:00', 'garbage']), None)
    assert result['rdates'] == ['2026-06-13T00:00:00+00:00']


def test_exdate_dicts_keep_their_reason(normalize):
    result = normalize.process_item(
        _item(exdates=[{'datetime': '2026-07-04T19:00:00-05:00', 'reason': 'holiday'}]), None)
    assert result['exdates'] == [{'datetime': '2026-07-05T00:00:00+00:00', 'reason': 'holiday'}]


def test_bare_exdate_strings_are_wrapped(normalize):
    result = normalize.process_item(_item(exdates=['2026-07-04T19:00:00-05:00']), None)
    assert result['exdates'] == [{'datetime': '2026-07-05T00:00:00+00:00', 'reason': ''}]


def test_missing_occurrence_lists_default_to_empty(normalize):
    result = normalize.process_item(_item(), None)
    assert result['rdates'] == [] and result['exdates'] == []


# ── 100 · Normalize: other fields ─────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    (25,          25.0),
    ('25',        25.0),
    ('1,250.00',  1250.0),
    ('free',      None),
    (None,        None),
])
def test_ticket_price_coercion(normalize, raw, expected):
    assert normalize.process_item(_item(ticket_price=raw), None)['ticket_price'] == expected


def test_non_list_tag_names_are_reset(normalize):
    assert normalize.process_item(_item(tag_names='Music'), None)['tag_names'] == []


def test_recurrence_defaults_applied(normalize):
    result = normalize.process_item(_item(), None)
    assert result['recurrence_freq'] == 'none'
    assert result['recurrence_interval'] == 1
    assert result['recurrence_byday'] == []
    assert result['recurrence_month_mode'] == 'day'


def test_existing_recurrence_values_are_kept(normalize):
    result = normalize.process_item(
        _item(recurrence_freq='weekly', recurrence_byday=['TH']), None)
    assert result['recurrence_freq'] == 'weekly'
    assert result['recurrence_byday'] == ['TH']


@pytest.mark.xfail(
    strict=True,
    reason="NormalizePipeline strips only commas before float(), so a currency "
           "symbol from a CSS selector ('$25.00') raises ValueError and the "
           "price is silently discarded.",
)
def test_currency_symbol_price(normalize):
    assert normalize.process_item(_item(ticket_price='$25.00'), None)['ticket_price'] == 25.0


# ── 200 · Dedup: one-off events ───────────────────────────────────────────────

ONEOFF = {
    'title': 'Jazz Night',
    'start_datetime': '2026-06-05T19:00:00',
    'location_title': 'Blues Alley',
    'recurrence_freq': 'none',
    'source_url': 'https://x.test/1',
}


def test_first_sighting_passes_through(dedup_pipeline):
    result = dedup_pipeline.process_item(dict(ONEOFF), None)
    assert result['fingerprint']
    assert result['is_recurring_update'] is False


def test_exact_duplicate_is_dropped(dedup_pipeline):
    dedup_pipeline.process_item(dict(ONEOFF), None)
    with pytest.raises(DropItem, match='Duplicate event'):
        dedup_pipeline.process_item(dict(ONEOFF), None)


@pytest.mark.parametrize('title', ['JAZZ NIGHT', 'Jazz  Night', 'Jazz Night!'])
def test_case_and_punctuation_variants_are_the_same_event(dedup_pipeline, title):
    dedup_pipeline.process_item(dict(ONEOFF), None)
    with pytest.raises(DropItem):
        dedup_pipeline.process_item(dict(ONEOFF, title=title), None)


def test_same_event_on_another_day_is_distinct(dedup_pipeline):
    dedup_pipeline.process_item(dict(ONEOFF), None)
    result = dedup_pipeline.process_item(
        dict(ONEOFF, start_datetime='2026-06-12T19:00:00'), None)
    assert result['fingerprint']


def test_time_of_day_does_not_affect_the_fingerprint(dedup_pipeline):
    """Only the date part is fingerprinted, so a shifted start time still dedups."""
    dedup_pipeline.process_item(dict(ONEOFF), None)
    with pytest.raises(DropItem):
        dedup_pipeline.process_item(dict(ONEOFF, start_datetime='2026-06-05T21:30:00'), None)


def test_different_venue_is_distinct(dedup_pipeline):
    dedup_pipeline.process_item(dict(ONEOFF), None)
    result = dedup_pipeline.process_item(dict(ONEOFF, location_title='The Annex'), None)
    assert result['fingerprint']


def test_address_substitutes_for_a_missing_venue_name(dedup_pipeline):
    a = dict(ONEOFF, location_title=None, location_address='1073 Wisconsin Ave NW')
    dedup_pipeline.process_item(a, None)
    with pytest.raises(DropItem):
        dedup_pipeline.process_item(dict(a), None)


def test_dedup_survives_reopening_the_database(dedup_pipeline, dedup_spider):
    from scraper.pipelines import FingerprintDedupPipeline
    dedup_pipeline.process_item(dict(ONEOFF), None)
    dedup_pipeline.close_spider(dedup_spider)

    reopened = FingerprintDedupPipeline()
    reopened.open_spider(dedup_spider)
    try:
        with pytest.raises(DropItem):
            reopened.process_item(dict(ONEOFF), None)
    finally:
        reopened.close_spider(dedup_spider)
    # Re-open so the fixture's teardown has a live connection to close.
    dedup_pipeline.open_spider(dedup_spider)


# ── 200 · Dedup: recurring events ─────────────────────────────────────────────

RECURRING = {
    'title': 'Trivia',
    'location_title': 'The Pub',
    'start_datetime': '2026-06-04T19:00:00',
    'recurrence_freq': 'weekly',
    'recurrence_interval': 1,
    'recurrence_byday': ['TH'],
    'recurrence_month_mode': 'day',
    'source_url': 'https://x.test/trivia',
}


def _register(pipeline, item, backend_id='uuid-123'):
    """Run a recurring item through, then record the backend ID as APISubmit would."""
    result = pipeline.process_item(dict(item), None)
    pipeline.store_recurring_backend_id(
        fingerprint=result['fingerprint'], backend_id=backend_id,
        title=item['title'], source_url=item['source_url'],
        start_datetime=item['start_datetime'],
    )
    return result


def test_first_recurring_sighting_is_a_create(dedup_pipeline):
    result = dedup_pipeline.process_item(dict(RECURRING), None)
    assert result['is_recurring_update'] is False
    assert result['backend_event_id'] is None


def test_unchanged_recurring_event_is_dropped(dedup_pipeline):
    _register(dedup_pipeline, RECURRING)
    with pytest.raises(DropItem, match='already up-to-date'):
        dedup_pipeline.process_item(dict(RECURRING), None)


def test_new_occurrence_date_becomes_a_patch(dedup_pipeline):
    _register(dedup_pipeline, RECURRING)
    result = dedup_pipeline.process_item(
        dict(RECURRING, start_datetime='2026-06-11T19:00:00'), None)
    assert result['is_recurring_update'] is True
    assert result['backend_event_id'] == 'uuid-123'


def test_patched_date_is_persisted(dedup_pipeline):
    _register(dedup_pipeline, RECURRING)
    moved = dict(RECURRING, start_datetime='2026-06-11T19:00:00')
    dedup_pipeline.process_item(dict(moved), None)
    with pytest.raises(DropItem, match='already up-to-date'):
        dedup_pipeline.process_item(dict(moved), None)


def test_changed_pattern_is_a_different_event(dedup_pipeline):
    """A weekly event moving from Thursdays to Fridays fingerprints separately."""
    _register(dedup_pipeline, RECURRING)
    result = dedup_pipeline.process_item(dict(RECURRING, recurrence_byday=['FR']), None)
    assert result['is_recurring_update'] is False


def test_recurring_and_oneoff_use_separate_tables(dedup_pipeline):
    dedup_pipeline.process_item(dict(ONEOFF), None)
    same_but_recurring = dict(ONEOFF, recurrence_freq='weekly', recurrence_byday=['FR'])
    assert dedup_pipeline.process_item(same_but_recurring, None)['fingerprint']


# ── 200 · Dedup: URL tracking for ConditionalFetchMiddleware ──────────────────

def test_url_tracking_round_trip(dedup_pipeline):
    url = 'https://x.test/page'
    assert dedup_pipeline.url_seen(url) is False
    dedup_pipeline.mark_url(url, etag='"abc"', last_modified='Wed, 01 Jan 2026 00:00:00 GMT')
    assert dedup_pipeline.url_seen(url) is True

    row = dedup_pipeline.conn.execute(
        'SELECT etag, last_modified FROM scraped_urls WHERE url = ?', (url,)).fetchone()
    assert row == ('"abc"', 'Wed, 01 Jan 2026 00:00:00 GMT')


def test_mark_url_overwrites_previous_validators(dedup_pipeline):
    url = 'https://x.test/page'
    dedup_pipeline.mark_url(url, etag='"old"')
    dedup_pipeline.mark_url(url, etag='"new"')
    row = dedup_pipeline.conn.execute(
        'SELECT etag FROM scraped_urls WHERE url = ?', (url,)).fetchone()
    assert row == ('"new"',)


# ── 300 · API submit: payload shape ───────────────────────────────────────────

def test_payload_carries_every_backend_field():
    payload = APISubmitPipeline._build_payload({
        'title': 'Jazz Night', 'start_datetime': '2026-06-06T00:00:00+00:00',
    })
    assert payload['title'] == 'Jazz Night'
    assert payload['recurrence_freq'] == 'none'
    assert payload['recurrence_interval'] == 1
    assert payload['rdates'] == [] and payload['exdates'] == []


def test_payload_falls_back_to_source_url():
    payload = APISubmitPipeline._build_payload({'source_url': 'https://x.test/1'})
    assert payload['url'] == 'https://x.test/1'


def test_payload_prefers_the_events_own_url():
    payload = APISubmitPipeline._build_payload(
        {'url': 'https://venue.test/e', 'source_url': 'https://x.test/1'})
    assert payload['url'] == 'https://venue.test/e'


def test_payload_excludes_scraper_internals():
    payload = APISubmitPipeline._build_payload({
        'title': 'x', 'is_recurring_update': True, 'backend_event_id': 'uuid-123',
    })
    assert 'is_recurring_update' not in payload
    assert 'backend_event_id' not in payload
