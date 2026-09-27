"""Tests for the recurrence extractor — schema.org Schedule first, then text heuristics."""
import pytest

from scraper.extractors import recurrence


def _text(description: str, title: str = 'Event') -> dict:
    return recurrence.extract(title, description)


# ── Text heuristics ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('description, freq, byday, interval', [
    ('Nightly karaoke',              'daily',   [],                             1),
    ('Every night at 9',             'daily',   [],                             1),
    ('Happens daily',                'daily',   [],                             1),
    ('Every Thursday at 8pm',        'weekly',  ['TH'],                         1),
    ('Every Monday and Wednesday',   'weekly',  ['MO', 'WE'],                   1),
    ('Every weekday',                'weekly',  ['MO', 'TU', 'WE', 'TH', 'FR'], 1),
    ('Every weekend',                'weekly',  ['SA', 'SU'],                   1),
    ('Every other Saturday',         'weekly',  ['SA'],                         2),
    ('Every 3 weeks on Sunday',      'weekly',  ['SU'],                         3),
    ('Every three weeks on Sunday',  'weekly',  ['SU'],                         3),
    ('A weekly showcase',            'weekly',  [],                             1),
    ('Held monthly',                 'monthly', [],                             1),
    ('Held annually',                'yearly',  [],                             1),
    ('Every year in June',           'yearly',  [],                             1),
])
def test_text_patterns(description, freq, byday, interval):
    result = _text(description)
    assert result['recurrence_freq'] == freq
    assert sorted(result['recurrence_byday']) == sorted(byday)
    assert result['recurrence_interval'] == interval


def test_numeric_ordinal_weekday_of_month():
    result = _text('3rd Tuesday of the month')
    assert result['recurrence_freq'] == 'monthly'
    assert result['recurrence_byday'] == ['3TU']
    assert result['recurrence_month_mode'] == 'weekday'


def test_title_is_searched_too():
    assert _text('', title='Nightly Karaoke')['recurrence_freq'] == 'daily'


@pytest.mark.parametrize('description', [
    'A single night of music',
    'One-time benefit concert',
    '',
])
def test_no_pattern_returns_none(description):
    assert _text(description) is None


def test_month_mode_only_set_for_monthly():
    assert _text('Every Thursday')['recurrence_month_mode'] == 'day'


# ── schema.org Schedule ───────────────────────────────────────────────────────

def test_jsonld_schedule_wins_over_text():
    node = {'eventSchedule': {'repeatFrequency': 'P1M', 'byDay': 'Monday'}}
    result = recurrence.extract('Event', 'every thursday', jsonld_node=node)
    assert result['recurrence_freq'] == 'monthly'
    assert result['recurrence_byday'] == ['MO']


@pytest.mark.parametrize('repeat, freq, interval', [
    ('P1D',  'daily',   1),
    ('P7D',  'weekly',  1),
    ('P14D', 'weekly',  2),
    ('P1W',  'weekly',  1),
    ('P2W',  'weekly',  2),
    ('P1M',  'monthly', 1),
    ('P1Y',  'yearly',  1),
])
def test_ical_repeat_frequencies(repeat, freq, interval):
    node = {'eventSchedule': {'repeatFrequency': repeat}}
    result = recurrence.extract('', '', jsonld_node=node)
    assert (result['recurrence_freq'], result['recurrence_interval']) == (freq, interval)


@pytest.mark.parametrize('by_day, expected', [
    ('Thursday',                      ['TH']),
    ('Thursdays',                     ['TH']),
    (['Monday', 'Friday'],            ['MO', 'FR']),
    (['https://schema.org/Saturday'], ['SA']),
])
def test_byday_normalisation(by_day, expected):
    node = {'eventSchedule': {'repeatFrequency': 'P1W', 'byDay': by_day}}
    assert recurrence.extract('', '', jsonld_node=node)['recurrence_byday'] == expected


def test_end_date_and_repeat_count():
    node = {'eventSchedule': {
        'repeatFrequency': 'P1W', 'endDate': '2026-12-31', 'repeatCount': 10,
    }}
    result = recurrence.extract('', '', jsonld_node=node)
    assert result['recurrence_until'] == '2026-12-31'
    assert result['recurrence_count'] == 10


def test_schedule_without_frequency_falls_through_to_text():
    node = {'eventSchedule': {'byDay': 'Monday'}}
    result = recurrence.extract('Event', 'every thursday', jsonld_node=node)
    assert result['recurrence_byday'] == ['TH']


def test_node_without_schedule_falls_through_to_text():
    result = recurrence.extract('Event', 'nightly', jsonld_node={'@type': 'Event'})
    assert result['recurrence_freq'] == 'daily'


# ── Fingerprint signature ─────────────────────────────────────────────────────

def test_signature_is_order_independent():
    a = {'recurrence_freq': 'weekly', 'recurrence_interval': 1,
         'recurrence_byday': ['TH', 'MO'], 'recurrence_month_mode': 'day'}
    b = dict(a, recurrence_byday=['MO', 'TH'])
    assert recurrence.signature(a) == recurrence.signature(b)


def test_signature_defaults_for_empty_dict():
    assert recurrence.signature({}) == 'none:1::day'


@pytest.mark.parametrize('override', [
    {'recurrence_freq': 'daily'},
    {'recurrence_interval': 2},
    {'recurrence_byday': ['FR']},
    {'recurrence_month_mode': 'weekday'},
])
def test_signature_changes_with_each_component(override):
    base = {'recurrence_freq': 'weekly', 'recurrence_interval': 1,
            'recurrence_byday': ['TH'], 'recurrence_month_mode': 'day'}
    assert recurrence.signature(base) != recurrence.signature(dict(base, **override))


def test_signature_ignores_the_occurrence_date():
    """The whole point: the same pattern fingerprints identically week to week."""
    base = {'recurrence_freq': 'weekly', 'recurrence_interval': 1,
            'recurrence_byday': ['TH'], 'recurrence_month_mode': 'day'}
    assert recurrence.signature(dict(base, start_datetime='2026-06-04')) == \
           recurrence.signature(dict(base, start_datetime='2026-06-11'))


# ── Written ordinals ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('phrase, expected', [
    ('First Tuesday of the month',  '1TU'),
    ('Second Tuesday of the month', '2TU'),
    ('Third Tuesday of the month',  '3TU'),
    ('Fourth Tuesday of the month', '4TU'),
    ('Last Tuesday of the month',   '-1TU'),
])
def test_written_ordinal_weekday_of_month(phrase, expected):
    assert recurrence.extract('Event', phrase)['recurrence_byday'] == [expected]
