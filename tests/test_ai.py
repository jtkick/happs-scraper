"""Tests for scraper/extractors/ai.py with the Claude client stubbed out (no network)."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from scraper.extractors import ai

PAGE_TEXT = (
    'Live music at The Rusty Tap. ' * 10 +
    'Friday June 5 — The Blue Notes, doors 7pm. '
    'Saturday June 6 — Open Mic from 8pm, free entry. '
)
HTML = f'<html><body><article><p>{PAGE_TEXT}</p></article></body></html>'


class FakeMessages:
    def __init__(self, payload, stop_reason='end_turn'):
        self.payload, self.stop_reason, self.calls = payload, stop_reason, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type='text', text=text)])


@pytest.fixture
def stub(monkeypatch):
    def install(payload, stop_reason='end_turn'):
        messages = FakeMessages(payload, stop_reason)
        monkeypatch.setattr(ai, '_client', lambda key: SimpleNamespace(messages=messages))
        return messages
    return install


def event(title, evidence, **extra):
    base = {k: None for k in ai.OUTPUT_KEYS}
    base.update(title=title, start_datetime='2026-06-05T19:00', evidence=evidence, **extra)
    return base


def test_returns_every_event_and_drops_nulls(stub):
    stub({'events': [event('The Blue Notes', 'Friday June 5 — The Blue Notes, doors 7pm'),
                     event('Open Mic', 'Saturday June 6 — Open Mic from 8pm', ticket_price=0)]})
    result = ai.extract_many(HTML, 'https://t.test/', 'key')
    assert [e['title'] for e in result.events] == ['The Blue Notes', 'Open Mic']
    assert 'description' not in result.events[0]
    assert result.events[1]['ticket_price'] == 0
    assert not any('drop_reason' in e for e in result.events)


def test_evidence_not_on_page_is_flagged(stub):
    stub({'events': [event('Ghost Gig', 'Sunday June 7 — Ghost Gig 9pm')]})
    [e] = ai.extract_many(HTML, 'https://t.test/', 'key').events
    assert e['drop_reason'] == 'ai_unverified'


def test_evidence_match_ignores_case_and_whitespace(stub):
    stub({'events': [event('Open Mic', 'saturday   june 6 —\nopen mic')]})
    [e] = ai.extract_many(HTML, 'https://t.test/', 'key').events
    assert 'drop_reason' not in e


def test_request_uses_structured_output_and_context(stub):
    messages = stub({'events': []})
    ai.extract_many(HTML, 'https://t.test/e', 'key', venue='The Rusty Tap', today=date(2026, 6, 1))
    call = messages.calls[0]
    assert call['model'] == ai.MODEL
    assert call['output_config']['format']['schema'] is ai.EVENTS_SCHEMA
    user = call['messages'][0]['content']
    assert 'Today: 2026-06-01' in user and 'The Rusty Tap' in user and 'https://t.test/e' in user


@pytest.mark.parametrize('stop_reason', ['refusal', 'max_tokens'])
def test_incomplete_responses_yield_nothing(stub, stop_reason):
    stub({'events': [event('X', 'doors 7pm')]}, stop_reason=stop_reason)
    assert ai.extract_many(HTML, 'https://t.test/', 'key').events == []


def test_short_pages_skip_the_call(stub):
    messages = stub({'events': []})
    assert ai.extract_many('<html><p>hi</p></html>', 'https://t.test/', 'key').events == []
    assert messages.calls == []


def test_long_pages_report_truncation(stub, monkeypatch):
    stub({'events': []})
    monkeypatch.setattr(ai, 'MAX_TEXT_CHARS', 250)
    assert ai.extract_many(HTML, 'https://t.test/', 'key').truncated


def test_extract_returns_first_verified_event(stub):
    stub({'events': [event('Ghost', 'nowhere'), event('Open Mic', 'Open Mic from 8pm')]})
    assert ai.extract(HTML, 'https://t.test/', 'key')['title'] == 'Open Mic'


def test_pick_events_links_only_returns_offered_urls(stub):
    stub({'urls': ['https://t.test/events', 'https://evil.test/']})
    links = [('Events', 'https://t.test/events'), ('Menu', 'https://t.test/menu')]
    assert ai.pick_events_links(links, 'https://t.test/', 'key') == ['https://t.test/events']
