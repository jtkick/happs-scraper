"""
Tests for the explicit-date extractor.

Every test passes an explicit `year_hint` or an in-text year so results do not
drift as the real calendar advances — `_best_year` otherwise resolves bare
month/day pairs against today's date.
"""
import pytest

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
