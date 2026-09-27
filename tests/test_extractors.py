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

from scraper.extractors import jsonld, opengraph
from scraper.extractors.jsonld import extract_all as jsonld_extract_all
from tests.conftest import load_fixtures

_ALL = load_fixtures()
_HTML_IDS = [f['id'] for f in _ALL if f['html'] is not None]
_HTML = [f for f in _ALL if f['html'] is not None]
_TEXT_IDS = [f['id'] for f in _ALL if f['clean_text'] is not None]
_TEXT = [f for f in _ALL if f['clean_text'] is not None]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _merge_no_overwrite(target: dict, source: dict) -> None:
    """Copy non-None values from source into target without clobbering existing keys."""
    for k, v in source.items():
        if v is not None and k not in target:
            target[k] = v


def _run_waterfall_single(html: str, url: str, seed_ctx: dict) -> dict:
    """
    Single-event waterfall: JSON-LD (first) → OpenGraph → seed_context.
    Mirrors BaseEventSpider.parse_event, minus selectors and AI.
    """
    page: dict = {}

    jl = jsonld.extract(html, url)
    if jl:
        _merge_no_overwrite(page, jl)

    if not (page.get('title') and page.get('start_datetime')):
        og = opengraph.extract(html, url)
        if og:
            _merge_no_overwrite(page, og)

    result = dict(page)
    _merge_no_overwrite(result, seed_ctx)
    return result


def _run_waterfall_multi(html: str, url: str, seed_ctx: dict) -> list[dict]:
    """
    Multi-event waterfall: all JSON-LD events, each with seed_context as fallback.
    Used when expected is a list.
    """
    results = []
    for event in jsonld_extract_all(html, url):
        r = dict(event)
        _merge_no_overwrite(r, seed_ctx)
        results.append(r)
    return results


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
    JSON-LD → OpenGraph waterfall + seed context matches fixture expected output.

    expected can be a single dict (one event) or a list of dicts (multiple events
    embedded in the page's JSON-LD).  For the list case each expected item is
    matched by title to the corresponding extracted event.
    """
    expected = fx['data'].get('expected')
    if not expected:
        pytest.skip('fixture has no expected output')

    seed_ctx = fx['data'].get('seed_context', {})

    if isinstance(expected, list):
        results = _run_waterfall_multi(fx['html'], fx['url'], seed_ctx)
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
        result = _run_waterfall_single(fx['html'], fx['url'], seed_ctx)
        _assert_fields(result, expected)


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
