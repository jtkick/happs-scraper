"""
Run an eval-store run with the code on disk: parse each case at the version it
was queued with (run.py), score it (scorecard.py), report back to the store and
send the scorecard. Used by tools/eval_worker.py and by the MCP server's
in-process runs (tools/eval_mcp.py run_suite(local=True)).

A run picked up again after a crash skips the cases it already reported and
puts their stored scores back into the scorecard.
"""
from __future__ import annotations
import logging
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Iterator, Optional

from scraper.eval.case import LABEL_FIELDS, Case
from scraper.eval.run import run_case
from scraper.eval.scorecard import aggregate, score_case
from scraper.eval.store import Store, StoreError

logger = logging.getLogger(__name__)
RESULT_BATCH = 20


class SchemaMismatch(Exception):
    pass


def check_schema(store: Store):
    """Refuse to score when the store's label fields differ from this code's: one repo moved without the other."""
    theirs = store.get('schema/')['label_fields']
    if list(theirs) != list(LABEL_FIELDS):
        raise SchemaMismatch(
            f'The store\'s label fields differ from the scraper\'s: only in the store '
            f'{sorted(set(theirs) - set(LABEL_FIELDS))}, only here {sorted(set(LABEL_FIELDS) - set(theirs))}. '
            'Update evaluation/labels.py LABEL_FIELDS and scraper/eval/case.py together.')


def scraper_sha() -> str:
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).parents[2],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def score_one(case: Case, ai: Optional[str], api_key: str, model: Optional[str]) -> dict:
    """Parse and score one case: {score, result, ai_response, duration_ms}. Runs in a worker process."""
    started = time.monotonic()
    result = run_case(case, ai=ai, api_key=api_key, model=model)
    score = score_case(case, result)
    duration = round((time.monotonic() - started) * 1000)
    score['duration_ms'] = duration
    return {
        'score': score, 'duration_ms': duration, 'ai_response': result.ai_response,
        'result': {**result.to_parsed(), 'ai_missing': result.ai_missing, 'ai_stale': result.ai_stale},
    }


def _report(case_id: str, out: dict) -> dict:
    score = out['score']
    return {'case_id': case_id, 'passed': not score['new_failures'], 'failures': score['failures'],
            'new_failures': score['new_failures'], 'stats': score, 'result': out['result']}


def _stored_scores(store: Store, run_id: str) -> dict:
    """Scores the store already has for this run's finished cases, keyed by case id."""
    scores = {}
    for item in store.get(f'runs/{run_id}/cases/'):
        if item['status'] == 'done' and item.get('stats') and 'counts' in item['stats']:
            scores[item['case_id']] = {**item['stats'], 'failures': item['failures'],
                                       'new_failures': item['new_failures']}
    return scores


def _loaded(store: Store, items: list) -> Iterator[tuple[str, Optional[Case], Optional[str]]]:
    """(case id, case, error) for each item; a case that can't be loaded comes with its error."""
    for item in items:
        try:
            yield item['case_id'], store.load_case(item['case_id'], item['case_version']), None
        except StoreError as exc:
            if exc.status not in (404, 410):
                raise
            yield item['case_id'], None, f'Could not load the case: {exc}'


def run_one(store: Store, run: dict, worker: str, api_key: str = '', jobs: int = 1) -> dict:
    """Score every unfinished case of a leased run and finish it; returns the finished run."""
    run_id = run['id']
    ai = None if run['ai'] == 'none' else run['ai']
    if ai == 'live' and not api_key:
        return store.post(f'runs/{run_id}/finish/', {
            'worker': worker, 'error': 'A live run needs ANTHROPIC_API_KEY on the worker.'}, idempotent=False)
    model = run.get('model') or None
    todo = [i for i in run['items'] if i.get('status') != 'done']
    scores = _stored_scores(store, run_id) if len(todo) < len(run['items']) else {}
    batch: list[dict] = []

    def send(force=False):
        if batch and (force or len(batch) >= RESULT_BATCH):
            store.post(f'runs/{run_id}/results/', {'worker': worker, 'results': list(batch)}, idempotent=False)
            batch.clear()

    def finished(case_id: str, out: Optional[dict], error: Optional[str]):
        if error:
            batch.append({'case_id': case_id, 'error': error})
        else:
            scores[case_id] = out['score']
            if ai == 'live' and out['ai_response']:
                store.post(f'cases/{case_id}/ai-responses/', {**out['ai_response'], 'origin': 'live'})
            batch.append(_report(case_id, out))
        send()

    if jobs <= 1:
        for case_id, case, error in _loaded(store, todo):
            if case is None:
                finished(case_id, None, error)
                continue
            try:
                finished(case_id, score_one(case, ai, api_key, model), None)
            except Exception as exc:    # one broken case mustn't stop the run
                logger.exception('Case %s failed', case_id)
                finished(case_id, None, f'{type(exc).__name__}: {exc}')
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            pending = {}
            for case_id, case, error in _loaded(store, todo):
                if case is None:
                    finished(case_id, None, error)
                else:
                    pending[case_id] = pool.submit(score_one, case, ai, api_key, model)
            for case_id, future in pending.items():
                try:
                    finished(case_id, future.result(), None)
                except Exception as exc:
                    logger.error('Case %s failed: %s', case_id, exc)
                    finished(case_id, None, f'{type(exc).__name__}: {exc}')
    send(force=True)
    return store.post(f'runs/{run_id}/finish/', {
        'worker': worker, 'scorecard': aggregate(scores), 'scraper_sha': scraper_sha()}, idempotent=False)
