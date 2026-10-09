"""Shared pytest helpers."""
from __future__ import annotations
import os

import pytest


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
