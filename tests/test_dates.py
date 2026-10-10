"""
Tests for the explicit-date extractor.

Every test passes an explicit `year_hint` or an in-text year so results do not
drift as the real calendar advances — `_best_year` otherwise resolves bare
month/day pairs against today's date.
"""
from datetime import date, datetime

import pytest
import time_machine

from scraper.extractors import dates

YEAR = 2026


# ── Time-range parsing ────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('6 to 10 p.m.',         (18, 0, 22, 0)),
    ('6:30 to 10:45 p.m.',   (18, 30, 22, 45)),
    ('10 a.m. to 6 p.m.',    (10, 0, 18, 0)),
    ('noon to 10 p.m.',      (12, 0, 22, 0)),
    ('12 to 10 p.m.',        (12, 0, 22, 0)),
    ('10 to 2 p.m.',         (10, 0, 14, 0)),   # start stays in the morning
    ('8 p.m. to midnight',   (20, 0, 0, 0)),
    ('6–10 p.m.',            (18, 0, 22, 0)),   # en dash
    ('6—10 p.m.',            (18, 0, 22, 0)),   # em dash
])
def test_parse_time_range(text, expected):
    assert dates._parse_time_range(text) == expected


@pytest.mark.parametrize('text', ['sometime soon', 'doors at 7 p.m.', ''])
def test_parse_time_range_returns_none(text):
    assert dates._parse_time_range(text) is None


# ── Multi-day festival segments ───────────────────────────────────────────────

MULTI_DAY = ('Friday, June 5: 6 to 10 p.m.; '
             'Saturday, June 6: noon to 10 p.m.; '
             'Sunday, June 7: 10 a.m. to 6 p.m.')


def test_multi_day_start_and_end():
    result = dates.extract(MULTI_DAY, year_hint=YEAR)
    assert result['start_datetime'] == '2026-06-05T18:00:00'
    assert result['end_datetime'] == '2026-06-05T22:00:00'


def test_multi_day_remaining_days_become_rdates():
    result = dates.extract(MULTI_DAY, year_hint=YEAR)
    assert result['rdates'] == ['2026-06-06T12:00:00', '2026-06-07T10:00:00']


def test_multi_day_needs_two_segments():
    """A single dated segment is left to the upstream extractors."""
    assert dates.extract('Friday, June 5: 6 to 10 p.m.', year_hint=YEAR) is None


# ── Compact date ranges ───────────────────────────────────────────────────────

def test_date_range_expands_to_one_occurrence_per_day():
    result = dates.extract('The festival runs June 5-7, 2026 from 11 a.m. to 8 p.m.')
    assert result['start_datetime'] == '2026-06-05T11:00:00'
    assert result['rdates'] == ['2026-06-06T11:00:00', '2026-06-07T11:00:00']


def test_date_range_uses_in_text_year_over_hint():
    result = dates.extract('June 5-7, 2027', year_hint=YEAR)
    assert result['start_datetime'].startswith('2027-06-05')


@pytest.mark.parametrize('text', [
    'June 7-5, 2026',     # reversed
    'June 1-30, 2026',    # longer than the 14-day cap
])
def test_implausible_ranges_are_rejected(text):
    result = dates.extract(text, year_hint=YEAR)
    assert result is None or 'rdates' not in result


# ── Explicit date lists ───────────────────────────────────────────────────────

def test_date_list_collects_each_date():
    result = dates.extract('Join us June 5, June 12, and June 19', year_hint=YEAR)
    assert result['start_datetime'] == '2026-06-05T00:00:00'
    assert result['rdates'] == ['2026-06-12T00:00:00', '2026-06-19T00:00:00']


def test_date_list_deduplicates_repeats():
    result = dates.extract('June 5, June 12 — see you June 5!', year_hint=YEAR)
    assert result['rdates'] == ['2026-06-12T00:00:00']


def test_single_date_is_not_claimed():
    """dates.py only handles multi-occurrence text; one date belongs upstream."""
    assert dates.extract('Yoga in the park June 5, 2026 at 9 a.m.') is None


# ── Exceptions ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('phrase', [
    'Trivia every Friday, except July 4, 2026',
    'Trivia every Friday. No event on July 4, 2026',
    'Trivia every Friday — closed on July 4, 2026',
    'Trivia every Friday, not held on July 4, 2026',
])
def test_exception_phrasings(phrase):
    result = dates.extract(phrase)
    assert result['exdates'] == [{'datetime': '2026-07-04T00:00:00', 'reason': ''}]


def test_multiple_exceptions_in_one_clause():
    result = dates.extract('Open mic every Thursday, no event on July 4 and July 11, 2026')
    assert [e['datetime'] for e in result['exdates']] == [
        '2026-07-04T00:00:00', '2026-07-11T00:00:00',
    ]


def test_exception_dates_are_not_reused_as_occurrences():
    """The exception clause is stripped before the occurrence parsers run."""
    result = dates.extract('Market June 5 and June 12, 2026; except June 19, 2026')
    occurrences = [result.get('start_datetime')] + (result.get('rdates') or [])
    assert '2026-06-19T00:00:00' not in occurrences


# ── Nothing to find ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'Come on down for a good time.',
    '',
    'Tickets are $20 and doors open at 7.',
])
def test_returns_none_when_no_dates(text):
    assert dates.extract(text, year_hint=YEAR) is None


# ── Known gap ─────────────────────────────────────────────────────────────────

@pytest.mark.xfail(
    strict=True,
    reason="_parse_time_range only recognises ranges ('7 to 9 p.m.'), so a "
           "single shared start time on a date list is dropped and every "
           "occurrence lands at midnight.",
)
def test_date_list_applies_a_single_shared_time():
    result = dates.extract('Join us June 5, June 12, and June 19 at 7 p.m.', year_hint=YEAR)
    assert result['start_datetime'] == '2026-06-05T19:00:00'


# ── Single dates ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    'October 31, 2026', 'Oct. 31, 2026', 'Oct 31st 2026', '31 October 2026',
    'Saturday, October 31, 2026', '10/31/2026', '10/31/26', '2026-10-31', 'October 31, 2026 at 9pm',
])
def test_parse_date_formats(text):
    assert dates.parse_date(text) == date(2026, 10, 31)


def test_parse_date_without_a_year_takes_the_hint():
    assert dates.parse_date('Oct 31', year_hint=2027) == date(2027, 10, 31)


@pytest.mark.parametrize('text', ['9pm', '9:00 p.m.', '10 pm', 'Monday', 'soon', '2/30/2026', ''])
def test_parse_date_rejects_times_and_non_dates(text):
    assert dates.parse_date(text) is None


def test_range_across_months_is_not_a_date_list():
    assert dates.extract('Dates: September 30, 2026 - October 31, 2026') is None


# ── Informal dates and times ──────────────────────────────────────────────────

# A Friday noon UTC; 2026-05-28 is a Thursday and 2027-05-28 a Friday.
TODAY = datetime(2026, 10, 9, 12, 0)


@pytest.mark.parametrize('text, expected', [
    ('7pm', (19, 0, None, None)),
    ('Doors 7:30 PM', (19, 30, None, None)),
    ('DJ Fern live. 8–11pm.', (20, 0, 23, 0)),
    ('Time Travel House Party. 9pm–1am.', (21, 0, 1, 0)),
    ('doors at 7:30 | show at 8', (20, 0, None, None)),
    ('6:30 doors, 7 show\n18+', (19, 0, None, None)),
    ('Thursdays at 8', (20, 0, None, None)),
    ('Brunch at 10', (10, 0, None, None)),
    ('Starts 19:30', (19, 30, None, None)),
    ('Live music 7-10pm', (19, 0, 22, 0)),
])
def test_find_time(text, expected):
    assert dates.find_time(text) == expected


@pytest.mark.parametrize('text', ['Open 24/7', 'Tickets at 8 locations', 'Shows 18+', 'Our 2nd show!', 'I am here'])
def test_find_time_ignores_numbers_that_arent_times(text):
    assert dates.find_time(text) is None


@time_machine.travel(TODAY, tick=False)
@pytest.mark.parametrize('text, expected', [
    ('Thursday 5/28', date(2026, 5, 28)),
    ('Friday 5/28', date(2027, 5, 28)),
    ('Saturday, January 9', date(2027, 1, 9)),
    ('Monday 5/28', None),
])
def test_a_stated_weekday_picks_the_year(text, expected):
    assert dates.parse_date(text) == expected


@time_machine.travel(TODAY, tick=False)
@pytest.mark.parametrize('text, start', [
    ('January 9th, 2027 | 6:30 doors, 7 show', '2027-01-09T19:00:00'),
    ('POP-PUNK TRIBUTE FEST\nSaturday, January 9\n6:30 doors, 7 show\n18+', '2027-01-09T19:00:00'),
    ('we\u2019re back for a 2nd show! on Thursday 5/28!\n(1) a displayed timer\n(2) more\nit\u2019s chaotic\n'
     'once it\u2019s full\u2026 it\u2019s full.\ndoors at 7:30 | show at 8', '2026-05-28T20:00:00'),
    ('Book Club\nOctober 24th', '2026-10-24'),
])
def test_find_start(text, start):
    assert dates.find_start(text)['start_datetime'] == start


@time_machine.travel(TODAY, tick=False)
def test_find_start_takes_the_date_after_the_title():
    text = 'Next up: Karaoke, Sat 10/10 9pm\nPunk Fest\nSaturday, January 9 at 7pm\nMore shows: Fri 10/16 8pm'
    assert dates.find_start(text, title='Punk Fest')['start_datetime'] == '2027-01-09T19:00:00'


@time_machine.travel(TODAY, tick=False)
def test_find_start_needs_a_date():
    assert dates.find_start('Thursdays at 8. Open 24/7, 1/2 off wings.') is None


@time_machine.travel(TODAY, tick=False)
def test_next_occurrence():
    assert dates.next_occurrence(['TH'], (20, 0, None, None), 'UTC') == {'start_datetime': '2026-10-15T20:00:00'}
    assert dates.next_occurrence(['FR'], (20, 0, 23, 0), 'UTC') == {
        'start_datetime': '2026-10-09T20:00:00', 'end_datetime': '2026-10-09T23:00:00'}
    assert dates.next_occurrence(['FR'], (10, 0, None, None), 'UTC')['start_datetime'] == '2026-10-16T10:00:00'
    assert dates.next_occurrence(['FR'], tz='UTC') == {'start_datetime': '2026-10-09'}
    assert dates.next_occurrence(['3TU']) is None


@time_machine.travel(datetime(2026, 10, 10, 2, 0), tick=False)
def test_next_occurrence_is_in_the_events_zone():
    # 02:00 UTC Saturday is still Friday evening in New York.
    assert dates.next_occurrence(['FR'], (22, 0, None, None), 'America/New_York')['start_datetime'] == \
        '2026-10-09T22:00:00'
