#!/usr/bin/env python3
"""
Run the eval store's queued runs with the code on disk (scraper/eval/runner.py):
lease a run, parse each case at the version it was queued with, score it,
report back, then send the scorecard. A run whose worker died is picked up
again once its lease runs out, and only its unfinished cases are redone.

Usage:
    python tools/eval_worker.py [--once] [--jobs N] [--poll SECONDS] [--name NAME]

Live runs (ai: live) ask the model with ANTHROPIC_API_KEY and record each new
answer on the case, so later replays use it.
"""
from __future__ import annotations
import argparse
import logging
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.eval.runner import SchemaMismatch, check_schema, run_one
from scraper.eval.store import Store, StoreError

logger = logging.getLogger('eval_worker')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--once', action='store_true', help='run what is queued, then exit')
    parser.add_argument('--jobs', type=int, default=1, help='cases scored at once, in separate processes')
    parser.add_argument('--poll', type=float, default=5.0)
    parser.add_argument('--name', default=f'{socket.gethostname()}-{os.getpid()}')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    store = Store.from_env()
    try:
        check_schema(store)
    except SchemaMismatch as exc:
        sys.exit(str(exc))
    api_key = os.getenv('ANTHROPIC_API_KEY', '')
    while True:
        run = store.lease(args.name)
        if run is None:
            if args.once:
                return
            time.sleep(args.poll)
            continue
        todo = sum(1 for i in run['items'] if i.get('status') != 'done')
        logger.info('Run %s: %d cases (%d to do), ai=%s', run['id'], len(run['items']), todo, run['ai'])
        try:
            done = run_one(store, run, args.name, api_key, jobs=args.jobs)
            card = done.get('scorecard') or {}
            logger.info('Run %s %s: recall %.2f precision %.2f', run['id'], done['status'],
                        card.get('recall', 0), card.get('precision', 0))
        except StoreError as exc:
            # 409: cancelled, or the lease ran out and someone else has it.
            logger.warning('Run %s stopped: %s', run['id'], exc)


if __name__ == '__main__':
    main()
