"""
Golden tests over the reviewed case corpus in tests/fixtures/ (see
scraper/eval/case.py for the format; add cases with tools/review.py).

Each case is parsed the way a crawl would parse it (scraper/eval/run.py),
with the model's recorded answer replayed, and scored by scraper/eval/compare.py.
Mismatches listed in the case's known_failures are expected; one that starts
passing fails the test, so the entry is removed in the same change (the same
rule as the xfail(strict=True) markers elsewhere).

    pytest                # replay recorded AI answers (offline)
    pytest --run-ai       # also call the model live (costs credits)
"""
from __future__ import annotations

import pytest

from scraper.eval.case import load_all
from scraper.eval.compare import compare
from scraper.eval.run import run_case

_CASES = load_all()
_IDS = [c.id for c in _CASES]


def _check(case, result) -> None:
    assert not result.ai_missing, (
        'the parser now asks the model but this case has no recorded answer; '
        're-capture it in tools/review.py')
    comparison = compare(case.events, result.events, not_events=case.not_events,
                         complete=case.complete, timezone_name=case.timezone)
    failures = set(comparison.failures)
    known = {k['path'] for k in case.known_failures}

    unexpected = sorted(failures - known)
    details = []
    for m in comparison.matches:
        for key, r in m.fields.items():
            if f'events[{m.index}].{key}' in unexpected:
                details.append(f'  events[{m.index}].{key}: expected {r.expected!r}, got {r.actual!r}')
    other = [p for p in unexpected if not any(d.startswith(f'  {p}:') for d in details)]
    assert not unexpected, 'new mismatches:\n' + '\n'.join(details + [f'  {p}' for p in other])

    fixed = sorted(known - failures)
    assert not fixed, f'known failures now pass — remove them from known_failures: {fixed}'


@pytest.mark.parametrize('case', _CASES, ids=_IDS)
def test_case(case):
    assert not case.validate(), case.validate()
    _check(case, run_case(case, ai='replay'))


@pytest.mark.ai
@pytest.mark.parametrize('case', [c for c in _CASES if c.ai_response], ids=[c.id for c in _CASES if c.ai_response])
def test_case_live_ai(case, anthropic_api_key):
    _check(case, run_case(case, ai='live', api_key=anthropic_api_key))


def test_cases_survive_the_clock():
    """Frozen time: a case parses the same long after its events have passed."""
    import time_machine
    from datetime import datetime, timedelta, timezone
    for case in _CASES:
        before = run_case(case).to_parsed()
        with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=800)):
            after = run_case(case).to_parsed()
        assert before == after, case.id
