"""Tests for scraper/page_state.py — hashing pages and deciding whether they changed."""
from datetime import datetime, timedelta, timezone

import pytest

from scraper import page_state
from scraper.page_state import PageState, refetch_reason

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
WEEK = timedelta(days=7)
EVENT = {'title': 'Jazz', 'start_datetime': '2026-10-10T19:00', 'description': 'Live jazz…'}


def test_listing_hash_ignores_bookkeeping_private_keys_and_empties():
    noisy = {**EVENT, 'extraction_method': 'jsonld', 'tag_names': ['Music'], '_schedule_text': 'x',
             'confidence': 0.9, 'image_url': None, 'rdates': []}
    assert page_state.listing_hash(noisy) == page_state.listing_hash(EVENT)
    assert page_state.listing_hash({**EVENT, 'title': 'Blues'}) != page_state.listing_hash(EVENT)


def test_content_hash_ignores_event_order():
    other = {**EVENT, 'title': 'Blues'}
    assert page_state.content_hash([EVENT, other]) == page_state.content_hash([other, EVENT])
    assert page_state.content_hash([EVENT]) != page_state.content_hash([EVENT, other])


def record(**extra):
    return {'url': 'u', 'fetched_at': (NOW - timedelta(days=1)).isoformat(), 'extraction_version': 1,
            'listing_hash': 'abc', **extra}


@pytest.mark.parametrize('rec, kwargs, reason', [
    (None, {'listing': 'abc'}, 'new'),
    (record(fetched_at=None), {'listing': 'abc'}, 'new'),
    (record(extraction_version=0), {'listing': 'abc'}, 'version'),
    (record(fetched_at=(NOW - timedelta(days=8)).isoformat()), {'listing': 'abc'}, 'max_age'),
    (record(), {'listing': 'changed'}, 'listing'),
    (record(), {'listing': 'abc', 'lastmod': NOW}, 'lastmod'),
    (record(), {'listing': 'abc', 'lastmod': NOW - timedelta(days=2)}, None),
    (record(), {'listing': 'abc'}, None),
    (record(), {}, 'no_signal'),
    (record(), {'lastmod': NOW - timedelta(days=2)}, None),
])
def test_refetch_reason(rec, kwargs, reason):
    assert refetch_reason(rec, version=1, max_age=WEEK, now=NOW, **kwargs) == reason


def test_date_only_and_zulu_times_parse():
    assert page_state.parse_time('2026-10-03') == datetime(2026, 10, 3, tzinfo=timezone.utc)
    assert page_state.parse_time('2026-10-03T10:46:15Z') == datetime(2026, 10, 3, 10, 46, 15, tzinfo=timezone.utc)
    assert page_state.parse_time('soon') is None


def test_only_changes_are_reported():
    state = PageState([{'url': 'a', 'kind': 'detail', 'listing_hash': 'x'}, {'url': 'b', 'kind': 'listing'}])
    state.update('a', 'detail', last_listed_at=NOW)
    assert state.get('a')['listing_hash'] == 'x'
    assert state.to_report() == [{'url': 'a', 'kind': 'detail', 'last_listed_at': NOW.isoformat()}]


def test_details_of_a_listing():
    state = PageState([{'url': 'a', 'kind': 'detail', 'listing_url': 'L'},
                       {'url': 'b', 'kind': 'detail', 'listing_url': 'M'}, {'url': 'L', 'kind': 'listing'}])
    assert [p['url'] for p in state.details_of('L')] == ['a']
