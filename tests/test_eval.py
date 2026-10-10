"""Tests for scraper/eval/: case files, the replaying runner and the scorer."""
from __future__ import annotations
import json

import pytest

from scraper.eval.case import Case
from scraper.eval.compare import compare, field_equal
from scraper.eval.run import run_case
from scraper.extractors import ai

LISTING = '''<html><head><script type="application/ld+json">[
 {"@type": "Event", "name": "Jazz Night", "startDate": "2026-10-01T19:00:00-04:00"},
 {"@type": "Event", "name": "Pub Quiz", "startDate": "2026-10-02T20:00:00-04:00"}
]</script></head><body></body></html>'''

PROSE = ('Live music at The Rusty Tap every week. ' * 10 +
         'Friday June 5 — The Blue Notes, doors 7pm. ')
PROSE_HTML = f'<html><body><article><p>{PROSE}</p></article></body></html>'


def _case(**kwargs) -> Case:
    defaults = dict(id='t', url='https://venue.test/events/', html=LISTING,
                    captured_at='2026-09-01T12:00:00+00:00')
    return Case(**{**defaults, **kwargs})


# ── compare ───────────────────────────────────────────────────────────────────

def test_datetimes_compare_as_instants():
    assert field_equal('start_datetime', '2026-10-01T19:00:00-04:00', '2026-10-01T23:00:00+00:00')
    assert field_equal('start_datetime', '2026-10-01T19:00', '2026-10-01T23:00:00+00:00',
                       'America/New_York')
    assert not field_equal('start_datetime', '2026-10-01T19:00:00-04:00', '2026-10-01T19:00:00+00:00')


def test_null_label_asserts_empty():
    assert field_equal('description', None, None)
    assert field_equal('tag_names', None, [])
    assert not field_equal('description', None, 'Something')


def test_fuzzy_description_and_tolerant_numbers():
    assert field_equal('description', 'Live jazz trio in the back room.', 'Live jazz trio in the back room')
    assert not field_equal('description', 'Live jazz trio.', 'Pub quiz with prizes.')
    assert field_equal('location_lat', 40.7128, 40.7131)
    assert field_equal('ticket_price', 10, 10.0)
    assert field_equal('url', 'https://a.test/e/', 'https://a.test/e')
    assert field_equal('tag_names', ['Music', 'Jazz'], ['jazz', 'music'])


def test_pairing_reports_missing_wrong_and_extra():
    expected = [{'title': 'Jazz Night', 'start_datetime': '2026-10-01T23:00:00+00:00'},
                {'title': 'Pub Quiz'}]
    actual = [{'title': 'jazz  NIGHT', 'start_datetime': '2026-10-02T23:00:00+00:00'},
              {'title': 'Gift Cards'}]
    c = compare(expected, actual)
    assert c.failures == ['events[0].start_datetime', 'events[1]', 'extra[Gift Cards]']
    assert c.recall == 0.5 and c.precision == 0.5


def test_incomplete_labels_ignore_extras_but_not_rejected():
    c = compare([{'title': 'Jazz Night'}], [{'title': 'Jazz Night'}, {'title': 'Karaoke'}, {'title': 'Menu'}],
                not_events=['menu'], complete=False)
    assert c.failures == ['extra[Menu]']


def test_start_time_breaks_title_ties():
    expected = [{'title': 'Open Mic', 'start_datetime': '2026-10-08T23:00:00+00:00'}]
    actual = [{'title': 'Open Mic', 'start_datetime': '2026-10-01T23:00:00+00:00'},
              {'title': 'Open Mic', 'start_datetime': '2026-10-08T23:00:00+00:00'}]
    c = compare(expected, actual, complete=False)
    assert c.matches[0].actual is actual[1]


# ── run_case ──────────────────────────────────────────────────────────────────

def test_run_case_uses_the_crawl_pipelines():
    result = run_case(_case())
    assert result.strategy == 'jsonld'
    assert [e['title'] for e in result.events] == ['Jazz Night', 'Pub Quiz']
    assert result.events[0]['start_datetime'] == '2026-10-01T23:00:00+00:00'
    assert result.events[0]['confidence'] > 0


def test_run_case_freezes_the_clock_at_capture():
    later = run_case(_case(captured_at='2026-10-02T12:00:00+00:00'))
    assert [e['title'] for e in later.events] == ['Pub Quiz']
    assert later.dropped[0]['drop_reason'] == 'past_event'


def _ai_answer(*events):
    return {'events': [{**{k: None for k in ai.OUTPUT_KEYS}, **e} for e in events]}


def test_replay_feeds_back_the_recorded_answer():
    user = ai.build_user_message(ai.page_text(PROSE_HTML), url='https://venue.test/events/',
                                 today=_case().captured.date())
    answer = _ai_answer({'title': 'The Blue Notes', 'start_datetime': '2026-10-05T19:00',
                         'evidence': 'Friday June 5 — The Blue Notes, doors 7pm'})
    case = _case(html=PROSE_HTML, ai_response={'prompt_hash': ai.prompt_hash(user), 'response': answer})
    result = run_case(case)
    assert result.ai_used and not result.ai_stale
    assert [e['title'] for e in result.events] == ['The Blue Notes']


def test_replay_flags_a_missing_or_stale_recording():
    assert run_case(_case(html=PROSE_HTML)).ai_missing
    stale = _case(html=PROSE_HTML, ai_response={'prompt_hash': 'old', 'response': _ai_answer()})
    assert run_case(stale).ai_stale


def test_no_ai_mode_never_asks():
    result = run_case(_case(html=PROSE_HTML), ai=None)
    assert not result.ai_missing and result.events == []


def test_live_mode_records_the_answer(monkeypatch):
    answer = _ai_answer()
    monkeypatch.setattr(ai, '_call', lambda *a, **kw: answer)
    result = run_case(_case(html=PROSE_HTML), ai='live', api_key='k')
    assert result.ai_response['response'] == answer
    assert result.ai_response['model'] == ai.MODEL


# ── Case files ────────────────────────────────────────────────────────────────

def test_case_round_trips(tmp_path):
    case = _case(events=[{'title': 'Jazz Night', 'description': None}], not_events=['Menu'],
                 parsed={'events': []}, known_failures=[{'path': 'events[0]', 'reason': 'x'}])
    case.save(tmp_path / 'c')
    loaded = Case.load(tmp_path / 'c')
    assert loaded.events == case.events and loaded.not_events == ['Menu']
    assert loaded.parsed == {'events': []} and loaded.html == LISTING
    assert loaded.known_failures == case.known_failures
    assert not (tmp_path / 'c' / 'ai_response.json').exists()


def test_validate_catches_bad_labels():
    problems = _case(kind='page', events=[{'start_datetime': 'x', 'colour': 'red'}]).validate()
    assert any('kind' in p for p in problems)
    assert any('needs a title' in p for p in problems)
    assert any('unknown fields' in p for p in problems)


# ── Scorecard ─────────────────────────────────────────────────────────────────

def test_scorecard_counts_fields_and_methods():
    from scraper.eval.scorecard import regressions, scorecard
    good = _case(id='good', events=[{'title': 'Jazz Night'}, {'title': 'Pub Quiz'}])
    bad = _case(id='bad', events=[{'title': 'Jazz Night', 'start_datetime': '2026-10-01T20:00:00-04:00'},
                                  {'title': 'Brunch'}], not_events=['Pub Quiz'])
    card = scorecard([good, bad])
    assert card['events'] == 4 and card['recall'] == 0.75
    assert card['fields']['start_datetime'] == {'right': 0, 'asserted': 1, 'accuracy': 0.0}
    assert card['methods']['missed']['events'] == 1
    assert card['per_case']['bad']['failures'] == ['events[0].start_datetime', 'events[1]', 'extra[Pub Quiz]']
    before = scorecard([good, _case(id='bad', events=[{'title': 'Jazz Night'}])])
    assert regressions(card, before) == {
        'worse': {'bad': ['events[0].start_datetime', 'events[1]']}, 'better': {}}


# ── Training export ───────────────────────────────────────────────────────────

def test_export_matches_the_live_prompt_and_answers_in_local_time(monkeypatch):
    from scraper.eval.dataset import training_record
    sent = []
    monkeypatch.setattr(ai, '_call', lambda key, system, user, *a, **k: sent.append(user) or {'events': []})
    case = _case(html=PROSE_HTML, seed_context={'timezone': 'America/New_York', 'location_title': 'The Rusty Tap'},
                 events=[{'title': 'The Blue Notes', 'start_datetime': '2026-10-05T23:00:00+00:00'}])
    run_case(case, ai='live', api_key='k')
    record, problem = training_record(case)
    assert problem == ''
    assert record['messages'][0]['content'] == sent[0]
    [event] = json.loads(record['messages'][1]['content'])['events']
    assert event['start_datetime'] == '2026-10-05T19:00'
    assert event['evidence'] == 'Friday June 5 — The Blue Notes, doors 7pm.'
    assert set(event) == set(ai.OUTPUT_KEYS)


def test_export_gives_midnight_back_as_a_bare_date():
    from scraper.eval.dataset import _local
    assert _local('2025-10-10T04:00:00+00:00', 'America/New_York') == '2025-10-10'
    assert _local('2025-10-10T23:30:00+00:00', 'America/New_York') == '2025-10-10T19:30'
    assert _local('2025-10-10T23:30:00+00:00', None) == '2025-10-10T23:30:00+00:00'


def test_export_skips_cases_it_cannot_teach_from():
    from scraper.eval.dataset import training_record
    assert training_record(_case(html=PROSE_HTML, complete=False))[1].startswith('labels cover only some')
    assert training_record(_case())[1] == 'too little page text for the model'
    no_evidence = _case(html=PROSE_HTML, events=[{'title': 'Not On The Page', 'start_datetime': '2026-10-05'}])
    assert 'no evidence' in training_record(no_evidence)[1]
    negative, _ = training_record(_case(html=PROSE_HTML))
    assert json.loads(negative['messages'][1]['content']) == {'events': []}


def test_split_is_stable_per_site():
    from scraper.eval.dataset import split_of
    a, b = _case(url='https://www.venue.test/a'), _case(url='https://venue.test/b')
    assert split_of(a, 0.5) == split_of(b, 0.5)
    assert {split_of(a, 0.0), split_of(a, 1.0)} == {'train', 'eval'}


# ── capture ───────────────────────────────────────────────────────────────────

def test_capture_obeys_robots_unless_told_not_to(monkeypatch):
    from scraper.eval import capture

    class Robots:
        def __init__(self, url): pass
        def read(self): pass
        def can_fetch(self, agent, url): return False

    class Page:
        ok, text = True, '<html>page</html>'

    monkeypatch.setattr(capture.robotparser, 'RobotFileParser', Robots)
    monkeypatch.setattr(capture.requests, 'get', lambda *a, **k: Page())
    with pytest.raises(capture.FetchError, match='robots.txt'):
        capture.fetch('https://venue.test/?format=json', render=False, obey_robots=True)
    assert capture.fetch('https://venue.test/?format=json', render=False, obey_robots=False) == Page.text
