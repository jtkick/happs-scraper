"""
Parametrized extractor tests driven by the fixture corpus in tests/fixtures/.

Each fixture directory yields one test case.  Fixtures without page.html are
skipped for the waterfall test; fixtures without clean_text.txt are skipped
for the AI test.

Run all non-AI tests:
    pytest

Run including AI tests (costs Claude API credits):
    pytest --run-ai
"""
from __future__ import annotations

import pytest

from scraper import extraction
from tests.conftest import load_fixtures

_ALL = load_fixtures()
_HTML_IDS = [f['id'] for f in _ALL if f['html'] is not None]
_HTML = [f for f in _ALL if f['html'] is not None]
_TEXT_IDS = [f['id'] for f in _ALL if f['clean_text'] is not None]
_TEXT = [f for f in _ALL if f['clean_text'] is not None]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(html: str, url: str, seed_ctx: dict) -> list[dict]:
    """The production path (scraper/extraction.py) minus AI: every event on the page."""
    result = extraction.extract_page(html, url)
    text = extraction.page_text(html) if result.single else None
    events = (extraction.finalize(d, context=seed_ctx, jsonld_node=n, page_text=text)
              for d, n in result.events)
    return [e for e in events if e]


def _assert_fields(result: dict, expected: dict) -> None:
    """
    Check every key present in `expected` against `result`.
    A key explicitly set to null in expected asserts the field is absent/null.
    Keys absent from expected are not checked.
    """
    for key, exp_val in expected.items():
        got = result.get(key)
        assert got == exp_val, f"[{key}] expected {exp_val!r}, got {got!r}"


def _find_matching_result(results: list[dict], expected_item: dict) -> dict | None:
    """Return the first result whose title matches the expected item, or None."""
    exp_title = expected_item.get('title')
    for r in results:
        if r.get('title') == exp_title:
            return r
    return None


# ── Waterfall tests ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('fx', _HTML, ids=_HTML_IDS)
def test_extractor_waterfall(fx):
    """
    Production extraction (no AI) + seed context matches fixture expected output.

    expected can be a single dict (one event) or a list of dicts (multiple events
    embedded in the page's JSON-LD).  For the list case each expected item is
    matched by title to the corresponding extracted event.
    """
    expected = fx['data'].get('expected')
    if not expected:
        pytest.skip('fixture has no expected output')

    seed_ctx = fx['data'].get('seed_context', {})

    results = _run(fx['html'], fx['url'], seed_ctx)
    if isinstance(expected, list):
        assert len(results) >= len(expected), (
            f"Expected {len(expected)} events but only extracted {len(results)}"
        )
        for exp_item in expected:
            match = _find_matching_result(results, exp_item)
            assert match is not None, (
                f"No extracted event matched title {exp_item.get('title')!r}"
            )
            _assert_fields(match, exp_item)
    else:
        assert results, 'no event extracted'
        _assert_fields(results[0], expected)


_LISTINGS = [f for f in _HTML if f['data'].get('expected_events')]


@pytest.mark.parametrize('fx', _LISTINGS, ids=[f['id'] for f in _LISTINGS])
def test_listing_recall(fx):
    """
    Listing fixtures: at least `min_recall` (default 100%) of expected_events
    are extracted, and every matched event has the expected field values.
    Fixtures written by tools/process_reports.py start with reviewed=false
    and are skipped until a human confirms their expected values.
    """
    if fx['data'].get('reviewed') is False:
        pytest.skip('fixture expected values not reviewed yet')
    expected = fx['data']['expected_events']
    results = _run(fx['html'], fx['url'], fx['data'].get('seed_context', {}))
    matched = [(exp, _find_matching_result(results, exp)) for exp in expected]
    found = [(exp, got) for exp, got in matched if got is not None]
    recall = len(found) / len(expected)
    assert recall >= fx['data'].get('min_recall', 1.0), (
        f"recall {recall:.0%}; missing {[e['title'] for e, g in matched if g is None]}")
    for exp, got in found:
        _assert_fields(got, exp)


# ── AI extractor tests ────────────────────────────────────────────────────────

@pytest.mark.ai
@pytest.mark.parametrize('fx', _TEXT, ids=_TEXT_IDS)
def test_ai_extractor(fx, anthropic_api_key):
    """AI extractor produces the expected fields for clean-text fixtures."""
    from scraper.extractors.ai import extract

    # Wrap in minimal HTML so trafilatura has something to strip
    html = f"<html><body><pre>{fx['clean_text']}</pre></body></html>"
    result = extract(html, fx['url'], anthropic_api_key)

    assert result is not None, 'AI extractor returned None'

    expected = fx['data'].get('expected', {})
    if expected:
        _assert_fields(result, expected)
