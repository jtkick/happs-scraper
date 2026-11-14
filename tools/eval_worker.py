#!/usr/bin/env python3
"""
Run the eval store's queued runs with the code on disk: lease a run, parse
each case at the version it was queued with (scraper/eval/run.py), score it
(scraper/eval/scorecard.py), report back, then send the scorecard.

Usage:
    python tools/eval_worker.py [--once] [--poll SECONDS] [--name NAME]

Live runs (ai: live) ask the model with ANTHROPIC_API_KEY and record each new
answer on the case, so later replays use it.
"""
from __future__ import annotations
import argparse
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.eval.run import run_case
from scraper.eval.scorecard import aggregate, score_case
from scraper.eval.store import Store, StoreError

logger = logging.getLogger('eval_worker')
RESULT_BATCH = 20


def scraper_sha() -> str:
    try:
        return subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=Path(__file__).parent.parent,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ''


def run_one(store: Store, run: dict, worker: str, api_key: str = '') -> dict:
    """Parse and score every case in a leased run; returns the finished run."""
    ai = None if run['ai'] == 'none' else run['ai']
    if ai == 'live' and not api_key:
        return store.post(f"runs/{run['id']}/finish/", {
            'worker': worker, 'error': 'A live run needs ANTHROPIC_API_KEY on the worker.'}, idempotent=False)
    scores, batch = {}, []

    def send():
        if batch:
            store.post(f"runs/{run['id']}/results/", {'worker': worker, 'results': list(batch)}, idempotent=False)
            batch.clear()

    for item in run['items']:
        case_id = item['case_id']
        try:
            case = store.load_case(case_id, item['case_version'])
            result = run_case(case, ai=ai, api_key=api_key, model=run.get('model') or None)
            score = score_case(case, result)
            scores[case_id] = score
            if ai == 'live' and result.ai_response:
                store.post(f'cases/{case_id}/ai-responses/', {**result.ai_response, 'origin': 'live'})
            batch.append({
                'case_id': case_id, 'passed': not score['new_failures'], 'failures': score['failures'],
                'new_failures': score['new_failures'], 'stats': score,
                'result': {**result.to_parsed(), 'ai_missing': result.ai_missing, 'ai_stale': result.ai_stale},
            })
        except StoreError:
            raise
        except Exception as exc:    # one broken case mustn't stop the run
            logger.exception('Case %s failed', case_id)
            batch.append({'case_id': case_id, 'error': f'{type(exc).__name__}: {exc}'})
        if len(batch) >= RESULT_BATCH:
            send()
    send()
    card = aggregate(scores)
    return store.post(f"runs/{run['id']}/finish/", {
        'worker': worker, 'scorecard': card, 'scraper_sha': scraper_sha()}, idempotent=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--once', action='store_true', help='run what is queued, then exit')
    parser.add_argument('--poll', type=float, default=5.0)
    parser.add_argument('--name', default=f'{socket.gethostname()}-{os.getpid()}')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    store = Store.from_env()
    api_key = os.getenv('ANTHROPIC_API_KEY', '')
    while True:
        run = store.post('runs/lease/', {'worker': args.name}, idempotent=False)
        if run is None:
            if args.once:
                return
            time.sleep(args.poll)
            continue
        logger.info('Run %s: %d cases, ai=%s', run['id'], len(run['items']), run['ai'])
        try:
            done = run_one(store, run, args.name, api_key)
            card = done.get('scorecard') or {}
            logger.info('Run %s %s: recall %.2f precision %.2f', run['id'], done['status'],
                        card.get('recall', 0), card.get('precision', 0))
        except StoreError as exc:
            # 409: cancelled, or the lease ran out and someone else has it.
            logger.warning('Run %s stopped: %s', run['id'], exc)


if __name__ == '__main__':
    main()
