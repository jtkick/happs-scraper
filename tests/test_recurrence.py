"""Tests for the recurrence extractor — schema.org Schedule first, then text heuristics."""
from datetime import datetime

import pytest
import time_machine

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


@pytest.mark.parametrize('description, byday', [
    ('Thursdays at 8', ['TH']),
    ('Live jazz Thursdays, 8–11pm', ['TH']),
    ('Tuesdays & Thursdays from 7pm', ['TU', 'TH']),
    ('EVERY THU — DJ Fern live', ['TH']),
])
def test_plural_days_with_a_time_are_weekly(description, byday):
    result = _text(description)
    assert result['recurrence_freq'] == 'weekly' and result['recurrence_byday'] == byday


@pytest.mark.parametrize('description', ['Open Thursdays', 'Closed Mondays and Tuesdays', 'Taco Tuesdays are back'])
def test_plural_days_alone_are_not_a_schedule(description):
    assert _text(description) is None


def test_plural_days_with_a_time_count_in_page_text():
    page = 'Social Groove\nThursdays at 8 with DJ Fern'
    assert recurrence.extract('Social Groove', '', page_text=page)['recurrence_byday'] == ['TH']


def test_title_days():
    assert recurrence.from_title_days('Tasting Tuesdays')['recurrence_byday'] == ['TU']
    assert recurrence.from_title_days('Jazz Night') is None


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


# ── Until / count / day lists ─────────────────────────────────────────────────

@pytest.mark.parametrize('text, freq, byday, until', [
    ('Recurring daily until October 31, 2026', 'daily', [], '2026-10-31'),
    ('Recurring weekly on Monday', 'weekly', ['MO'], None),
    ('Recurring weekly on Monday, Wednesday, Thursday, Friday until October 30, 2026',
     'weekly', ['MO', 'WE', 'TH', 'FR'], '2026-10-30'),
    ('Recurring weekly on Sunday, Monday, Tuesday, Wednesday, Thursday until November 22, 2026',
     'weekly', ['SU', 'MO', 'TU', 'WE', 'TH'], '2026-11-22'),
    ('Every Thursday through Dec. 18, 2026', 'weekly', ['TH'], '2026-12-18'),
    ('Every Friday, 5-9pm, until 10/31/2026', 'weekly', ['FR'], '2026-10-31'),
    ('Tuesdays & Thursdays every week', None, None, None),
    ('Every Tues and Thurs', 'weekly', ['TU', 'TH'], None),
    ('Fridays Sep 18th – Oct 23rd 10:15AM-11AM', 'weekly', ['FR'], '2026-10-23'),
])
@time_machine.travel(datetime(2026, 10, 9, 12, 0), tick=False)
def test_recurrence_phrases_with_until(text, freq, byday, until):
    result = _text(text)
    if freq is None:
        assert result is None
        return
    assert result['recurrence_freq'] == freq
    assert result['recurrence_byday'] == byday
    assert result['recurrence_until'] == until


@pytest.mark.parametrize('text', ['Open daily until 9pm', 'Every Thursday at 8. Tickets on sale until Nov 1, 2026'])
def test_until_must_be_a_date_in_the_same_sentence(text):
    assert _text(text)['recurrence_until'] is None


def test_for_n_weeks_is_a_count():
    result = _text('Every Tuesday for 6 weeks')
    assert result['recurrence_count'] == 6 and result['recurrence_until'] is None


def test_every_other_year():
    result = _text('Lights up the city for 4 days every other year')
    assert (result['recurrence_freq'], result['recurrence_interval']) == ('yearly', 2)


def test_schedule_text_beats_description():
    result = recurrence.extract('Show', 'A weekly favourite', schedule_text='Recurring daily')
    assert result['recurrence_freq'] == 'daily'


# ── Page text ─────────────────────────────────────────────────────────────────

def test_page_text_takes_only_strong_phrases():
    page = 'Trivia Night\nDaily specials at the bar. Our weekly newsletter.'
    assert recurrence.extract('Trivia Night', '', page_text=page) is None
    page = 'Trivia Night\nRecurring weekly on Tuesday until December 1, 2026'
    result = recurrence.extract('Trivia Night', '', page_text=page)
    assert result['recurrence_byday'] == ['TU'] and result['recurrence_until'] == '2026-12-01'


def test_page_text_starts_at_the_title():
    page = 'Sidebar: Brunch, recurring daily\nTrivia Night\nOctober 6\n' + 'x' * recurrence.PAGE_WINDOW + \
           '\nRelated: Karaoke every Friday'
    assert recurrence.extract('Trivia Night', '', page_text=page) is None
