"""Shared pytest helpers and fixture corpus loader."""
from __future__ import annotations
import json
import os
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / 'fixtures'


def load_fixtures() -> list[dict]:
    """
    Walk tests/fixtures/ and return one dict per fixture directory.

    Each dict has:
      id          — directory name (used as the test ID)
      url         — source URL from fixture.json
      data        — parsed fixture.json contents
      html        — raw HTML string, or None if page.html is absent
      clean_text  — trafilatura-extracted text, or None if clean_text.txt is absent
    """
    fixtures = []
    if not FIXTURES_DIR.exists():
        return fixtures
    for fixture_dir in sorted(FIXTURES_DIR.iterdir()):
        if not fixture_dir.is_dir():
            continue
        meta_path = fixture_dir / 'fixture.json'
        if not meta_path.exists():
            continue
        data = json.loads(meta_path.read_text())
        html_path = fixture_dir / 'page.html'
        text_path = fixture_dir / 'clean_text.txt'
        fixtures.append({
            'id':         fixture_dir.name,
            'url':        data.get('url', 'https://example.com/'),
            'data':       data,
            'html':       html_path.read_text()  if html_path.exists()  else None,
            'clean_text': text_path.read_text()  if text_path.exists()  else None,
        })
    return fixtures


# ── Pytest hooks ──────────────────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        '--run-ai', action='store_true', default=False,
        help='Run AI extractor tests (makes real Claude API calls — costs credits)',
    )


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'ai: marks tests that call the Claude API (skipped unless --run-ai is passed)',
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption('--run-ai'):
        skip = pytest.mark.skip(reason='pass --run-ai to run AI extractor tests')
        for item in items:
            if 'ai' in item.keywords:
                item.add_marker(skip)


@pytest.fixture
def anthropic_api_key():
    key = os.getenv('ANTHROPIC_API_KEY', '')
    if not key:
        pytest.skip('ANTHROPIC_API_KEY not set')
    return key


# ── Pipeline helpers ──────────────────────────────────────────────────────────

class _FakeSettings(dict):
    """Stands in for Scrapy's Settings object, which pipelines access via .get()."""
    def get(self, key, default=None):
        return super().get(key, default)


class FakeSpider:
    """Minimal spider double — pipelines only ever touch `.settings`."""
    def __init__(self, **settings):
        self.settings = _FakeSettings(settings)


@pytest.fixture
def dedup_pipeline():
    """An opened in-run FingerprintDedupPipeline."""
    from scraper.pipelines import FingerprintDedupPipeline
    pipeline = FingerprintDedupPipeline()
    pipeline.open_spider(FakeSpider())
    return pipeline
