"""
Tests for the item pipelines: 100 Normalize → 200 FingerprintDedup → 300 APISubmit.

Date assertions use timezone-aware input strings or an explicit item
`timezone`. Naive strings without one fall back to the machine's local zone,
which is not stable across dev machines and CI.
"""
from datetime import datetime

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
    with pytest.raises(DropItem, match='missing_title'):
        normalize.process_item(item, None)


def test_unparseable_start_is_dropped(normalize):
    with pytest.raises(DropItem, match='unparseable_start'):
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
    result = normalize.process_item(_item(start_datetime=datetime(2026, 6, 5, 19, 0)), None)
    assert result['start_datetime'] == '2026-06-05T19:00:00'


def test_naive_dates_come_out_timezone_aware(normalize):
    result = normalize.process_item(_item(start_datetime='2026-06-05T19:00:00'), None)
    assert result['start_datetime'].endswith('+00:00')


def test_naive_dates_are_read_in_the_venue_timezone(normalize):
    result = normalize.process_item(
        _item(start_datetime='June 5 2026 7pm', end_datetime='2026-06-05T22:00:00',
              rdates=['2026-06-12 19:00'], timezone='America/Chicago'), None)
    assert result['start_datetime'] == '2026-06-06T00:00:00+00:00'
    assert result['end_datetime'] == '2026-06-06T03:00:00+00:00'
    assert result['rdates'] == ['2026-06-13T00:00:00+00:00']


def test_explicit_offset_beats_venue_timezone(normalize):
    result = normalize.process_item(
        _item(start_datetime='2026-06-05T19:00:00-04:00', timezone='America/Chicago'), None)
    assert result['start_datetime'] == '2026-06-05T23:00:00+00:00'


def test_naive_datetime_object_gets_venue_timezone(normalize):
    result = normalize.process_item(
        _item(start_datetime=datetime(2026, 6, 5, 19, 0), timezone='America/Chicago'), None)
    assert result['start_datetime'] == '2026-06-05T19:00:00-05:00'


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
    ('free',      0.0),
    ('$10 - $15', 10.0),
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


def test_exact_duplicate_is_dropped(dedup_pipeline):
    dedup_pipeline.process_item(dict(ONEOFF), None)
    with pytest.raises(DropItem, match='duplicate_in_run'):
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


def test_fingerprint_is_prefixed_with_the_source(dedup_pipeline):
    a = dedup_pipeline.process_item(dict(ONEOFF, source_id='src-1'), None)
    b = dedup_pipeline.process_item(dict(ONEOFF, source_id='src-2'), None)
    assert a['fingerprint'].startswith('src-1:') and b['fingerprint'].startswith('src-2:')


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


def test_recurring_fingerprint_ignores_the_next_occurrence_date(dedup_pipeline):
    """Sites bump the 'next date' every week; the backend must see one event."""
    first = dedup_pipeline.process_item(dict(RECURRING), None)['fingerprint']
    dedup_pipeline.seen.clear()  # a later run
    later = dedup_pipeline.process_item(
        dict(RECURRING, start_datetime='2026-06-11T19:00:00'), None)['fingerprint']
    assert first == later


def test_changed_pattern_is_a_different_event(dedup_pipeline):
    """A weekly event moving from Thursdays to Fridays fingerprints separately."""
    a = dedup_pipeline.process_item(dict(RECURRING), None)['fingerprint']
    b = dedup_pipeline.process_item(dict(RECURRING, recurrence_byday=['FR']), None)['fingerprint']
    assert a != b


def test_recurring_and_oneoff_fingerprint_differently(dedup_pipeline):
    dedup_pipeline.process_item(dict(ONEOFF), None)
    same_but_recurring = dict(ONEOFF, recurrence_freq='weekly', recurrence_byday=['FR'])
    assert dedup_pipeline.process_item(same_but_recurring, None)['fingerprint']


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


def test_payload_carries_upsert_metadata():
    payload = APISubmitPipeline._build_payload({
        'title': 'x', 'fingerprint': 'src:abc', 'source_id': 'src', 'confidence': 0.5,
        'review_required': True, 'evidence': 'June 5 7pm', 'drop_reason': None,
    })
    assert payload['source_fingerprint'] == 'src:abc'
    assert payload['source_id'] == 'src'
    assert payload['review_required'] is True
    assert 'evidence' not in payload and 'drop_reason' not in payload


class FakeClient:
    token = 't'

    def __init__(self, status, body):
        self.status, self.body, self.payloads = status, body, []

    def ingest(self, payload):
        self.payloads.append(payload)
        return self.status, self.body


@pytest.mark.parametrize('status, body, expected', [
    (201, {'id': 'e1'},                          'created'),
    (200, {'id': 'e1', 'changed': ['title']},    'updated'),
    (200, {'id': 'e1', 'changed': []},           'unchanged'),
    (400, {'detail': 'bad'},                     'failed'),
    (0,   {'detail': 'connection refused'},      'failed'),
])
def test_submit_records_ingest_status(status, body, expected):
    pipeline = APISubmitPipeline()
    pipeline.client = FakeClient(status, body)
    item = pipeline.process_item(_item(fingerprint='src:abc'), None)
    assert item['ingest_status'] == expected


# ── 150 · Validate ────────────────────────────────────────────────────────────

@pytest.fixture
def validate():
    from scraper.pipelines import ValidatePipeline
    return ValidatePipeline()


def _future(days=7):
    from datetime import timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _valid(**fields):
    fields.setdefault('start_datetime', _future())
    return _item(**fields)


def test_upcoming_event_passes_and_is_scored(validate):
    item = validate.process_item(_valid(extraction_method='jsonld', description='d'), None)
    assert item['confidence'] == 0.95
    assert item['review_required'] is False


def test_ai_events_go_to_review(validate):
    item = validate.process_item(_valid(extraction_method='ai'), None)
    assert item['review_required'] is True


def test_review_threshold_is_configurable(validate):
    from tests.conftest import FakeSpider
    item = validate.process_item(_valid(extraction_method='ai'), FakeSpider(REVIEW_THRESHOLD=0.4))
    assert item['review_required'] is False


@pytest.mark.parametrize('fields, reason', [
    ({'drop_reason': 'ai_unverified'},                       'ai_unverified'),
    ({'title': 'Opening Hours'},                             'not_an_event'),
    ({'title': 'Gift Cards'},                                'not_an_event'),
    ({'title': 'Private Events'},                            'not_an_event'),
    ({'title': 'The Rusty Tap', 'location_title': 'The Rusty Tap'}, 'title_is_venue'),
    ({'title': 'ab'},                                        'bad_title_length'),
    ({'start_datetime': _future(-3)},                        'past_event'),
    ({'start_datetime': _future(900)},                       'too_far_future'),
])
def test_drops_record_a_reason(validate, fields, reason):
    with pytest.raises(DropItem, match=f'^{reason}:'):
        validate.process_item(_valid(**fields), None)


def test_ongoing_multi_day_event_is_not_past(validate):
    validate.process_item(_valid(start_datetime=_future(-2), end_datetime=_future(2)), None)


def test_recurring_event_with_past_anchor_is_kept(validate):
    validate.process_item(_valid(start_datetime=_future(-30), recurrence_freq='weekly'), None)


def test_recurring_event_that_ended_is_dropped(validate):
    with pytest.raises(DropItem, match='^past_event'):
        validate.process_item(_valid(start_datetime=_future(-30), recurrence_freq='weekly',
                                     recurrence_until='2020-01-01'), None)


def test_future_rdates_keep_a_past_anchor(validate):
    validate.process_item(_valid(start_datetime=_future(-30), rdates=[_future(10)]), None)
