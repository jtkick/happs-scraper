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
from scraper.extractors import recipe as recipe_extractor
from scraper.pipelines import dry_run
from scraper.spiders.base import BaseEventSpider


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
        spider = BaseEventSpider(name='eval')
        result.events, result.dropped = dry_run(
            [spider.build_item(data, response) for data in finalized], spider)
    return result


def _extract(case: Case, response, ai, result: RunResult) -> list[dict]:
    context = case.seed_context or {}
    if case.platform:
        adapter = platforms.get(case.platform)
        events = adapter.parse(response) if adapter else None
        if events is not None:
            result.strategy = f'platform:{adapter.name}'
            finalized = []
            for data in events:
                data.setdefault('extraction_method', result.strategy)
                final = extraction.finalize(data, context=context)
                if final:
                    finalized.append(final)
            return finalized

    recipe = case.recipe or {}
    recipe_events = recipe_extractor.extract(response, recipe) if recipe.get('item_css') else None
    partial = case.partial if case.kind == 'detail' else None
    if partial and extraction.sufficient(partial):
        ai = None
    page = extraction.extract_page(case.html, case.url, recipe_events=recipe_events, ai=ai)
    result.strategy = page.strategy
    result.ai_used = page.ai_used
    return extraction.finalize_page(page, case.html, context=context, partial=partial)


def _ai_callable(case: Case, mode: Optional[str], api_key: str, model: Optional[str],
                 result: RunResult):
    if mode is None:
        return None
    if mode not in ('replay', 'live'):
        raise ValueError(f'unknown AI mode {mode!r}')

    def respond(system, user, schema, label):
        if mode == 'live':
            used = model or ai_extractor.MODEL
            data = ai_extractor._call(api_key, system, user, schema, label, model=used)
            result.ai_response = {'model': used, 'prompt_hash': ai_extractor.prompt_hash(user),
                                  'response': data}
            return data
        recorded = case.ai_response
        if not recorded or recorded.get('response') is None:
            result.ai_missing = True
            return None
        if recorded.get('prompt_hash') != ai_extractor.prompt_hash(user):
            result.ai_stale = True
        return recorded['response']

    venue = (case.seed_context or {}).get('location_title')
    today = case.captured.date()
    return lambda html, url: ai_extractor.extract_many(
        html, url, api_key, venue=venue, today=today, respond=respond)
