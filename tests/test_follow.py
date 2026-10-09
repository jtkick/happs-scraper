"""Tests for scraper/follow.py — which listing events are worth their own page."""
import pytest

from scraper import follow


@pytest.mark.parametrize('extra, reason', [
    ({}, None),
    ({'description': None}, 'required'),
    ({'start_datetime': None}, 'required'),
    ({'description': 'An evening of jazz from the trio, with…'}, 'soft'),
    ({'description': 'Live jazz'}, 'soft'),
    ({'location_address': None}, 'soft'),
    ({'location_address': None, 'location_lat': 39.1}, None),
    ({'end_datetime': None}, 'soft'),
    ({'end_datetime': '2026-10-31'}, 'soft'),
    ({'end_datetime': '2026-10-31', 'recurrence_freq': 'daily'}, None),
])
def test_detail_reason(extra, reason):
    data = {'title': 'Jazz', 'start_datetime': '2026-10-01T19:00', 'end_datetime': '2026-10-01T22:00',
            'description': 'Live jazz.', 'location_address': '1 Main St', **extra}
    assert follow.detail_reason(data) == reason


@pytest.mark.parametrize('reason, budget, useful, scheduled, expected', [
    ('required', 1, None, 0, True),
    ('required', 0, None, 0, False),
    ('soft', 3, None, 0, False),                        # the reserve is for required follows
    ('soft', 4, None, 0, True),
    ('soft', 4, False, follow.DETAIL_SAMPLE_MIN - 1, True),
    ('soft', 4, False, follow.DETAIL_SAMPLE_MIN, False),
])
def test_may_follow(reason, budget, useful, scheduled, expected):
    assert follow.may_follow(reason, budget=budget, reserve=3, detail_useful=useful,
                             soft_scheduled=scheduled) is expected


def test_judge_useful_waits_for_a_sample():
    assert follow.judge_useful(follow.DETAIL_SAMPLE_MIN - 1, 0) is None
    assert follow.judge_useful(10, 3) is True and follow.judge_useful(10, 2) is False


@pytest.mark.parametrize('url, expected', [
    ('/e/jazz', 'https://venue.test/e/jazz'),
    ('https://venue.test/events/', None),               # the listing itself
    ('https://tickets.example/e/1', None),               # another site
    (None, None),
])
def test_detail_url(url, expected):
    assert follow.detail_url({'url': url}, 'https://venue.test/events') == expected
