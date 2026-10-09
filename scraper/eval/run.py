"""
Parse a saved case exactly as a crawl would: extraction (scraper/extraction.py),
then the Normalize and Validate pipelines, with the clock frozen at the
moment the page was captured so "next Friday" and "past event" never drift.

AI modes:
  'replay'  feed back the recorded ai_response.json (offline, deterministic)
  'live'    call the model and record its answer on the result
  None      no AI, like a crawl without ANTHROPIC_API_KEY
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

import time_machine
from scrapy.http import HtmlResponse

from scraper import extraction, platforms
from scraper.eval.case import Case
from scraper.extractors import ai as ai_extractor
from scraper.items import event_item
from scraper.pipelines import dry_run



@dataclass
class RunResult:
    events: list[dict] = field(default_factory=list)    # what would be sent to the backend
    dropped: list[dict] = field(default_factory=list)   # each with 'drop_reason'
    strategy: str = ''
    ai_used: bool = False
    # Replay wanted the model but nothing was recorded, or the prompt changed since.
    ai_missing: bool = False
    ai_stale: bool = False
    ai_response: Optional[dict] = None                  # set by a live run

    def to_parsed(self) -> dict:
        return {'strategy': self.strategy, 'ai_used': self.ai_used,
                'events': self.events, 'dropped': self.dropped}


def run_case(case: Case, ai: Optional[str] = 'replay', *, api_key: str = '',
             model: Optional[str] = None) -> RunResult:
    result = RunResult()
    with time_machine.travel(case.captured, tick=False):
        response = HtmlResponse(url=case.url, body=case.html.encode('utf-8'), encoding='utf-8')
        finalized = _extract(case, response, _ai_callable(case, ai, api_key, model, result), result)
        result.events, result.dropped = dry_run(
            [event_item(data, source_url=case.url) for data in finalized])
    return result


def _extract(case: Case, response, ai, result: RunResult) -> list[dict]:
    context = case.seed_context or {}
    adapter = platforms.get(case.platform)
    if adapter:
        events = extraction.parse_feed(adapter, response, context=context)
        if events is not None:
            result.strategy = f'platform:{adapter.name}'
            return events
    page, events = extraction.parse_page(response, kind=case.kind, recipe=case.recipe, context=context,
                                         partial=case.partial, ai=ai)
    result.strategy, result.ai_used = page.strategy, page.ai_used
    return events


def _ai_callable(case: Case, mode: Optional[str], api_key: str, model: Optional[str],
                 result: RunResult):
    if mode is None:
        return None
    if mode not in ('replay', 'live'):
        raise ValueError(f'unknown AI mode {mode!r}')

    if mode == 'live':
        record = lambda answer: setattr(result, 'ai_response', answer)  # noqa: E731
        respond = ai_extractor.recorder(api_key, record, model)
    else:
        def respond(system, user, schema, label):
            recorded = case.ai_response
            if not recorded or recorded.get('response') is None:
                result.ai_missing = True
                return None
            if recorded.get('prompt_hash') != ai_extractor.prompt_hash(user):
                result.ai_stale = True
            return recorded['response']

    venue = (case.seed_context or {}).get('location_title')
    today = case.captured.date()
    return ai_extractor.extractor(api_key, venue=venue, today=today, respond=respond)
