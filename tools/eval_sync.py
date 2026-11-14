#!/usr/bin/env python3
"""
Move cases between disk (tests/fixtures/, review/inbox/) and the backend's eval store.

Usage:
    python tools/eval_sync.py push [DIR …] [--tag T …] [--reviewed]
    python tools/eval_sync.py pull --to DIR [--suite NAME | --tag T | --status reviewed]

push   Upserts each case by its folder name (external_id). Defaults to tests/fixtures
       (marked reviewed, tagged `fixture`) and review/inbox (drafts, tagged `inbox`).
       Sending a case that hasn't changed leaves it alone.
pull   Writes cases from the store as case folders, e.g. reviewed ones for the golden tests.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.eval.case import FIXTURES_DIR, INBOX_DIR, Case
from scraper.eval.store import Store, StoreError, case_to_api, site_to_api

BATCH_BYTES = 8 * 1024 * 1024
BATCH_ITEMS = 50


def push(store: Store, root: Path, tags: list[str], reviewed: bool) -> dict:
    totals = {'created': 0, 'updated': 0, 'refused': 0}
    batch, size = [], 0

    def flush():
        nonlocal batch, size
        if not batch:
            return
        results = store.post('cases/bulk/', {'items': [item for _, item in batch]})['results']
        for (case_id, _), r in zip(batch, results):
            if r['status'] == 201:
                totals['created'] += 1
            elif r['status'] == 200:
                totals['updated'] += 1
            else:
                totals['refused'] += 1
                print(f'  {case_id}: ' + '; '.join(f"{e['path'] or '-'} {e['message']}" for e in r['errors']))
        batch, size = [], 0

    for path in sorted(p for p in root.iterdir() if (p / 'case.json').exists()):
        case = Case.load(path)
        if case.kind == 'site':
            # Sites go one at a time, pages and all.
            try:
                store.put(f'sites/by-key/{case.id}/', site_to_api(case, tags=tags, reviewed=reviewed))
                totals['updated'] += 1
            except StoreError as exc:
                totals['refused'] += 1
                print(f'  {case.id}: {exc}')
            continue
        item = {**case_to_api(case, tags=tags, reviewed=reviewed), 'external_id': case.id}
        weight = len(case.html.encode('utf-8'))
        if batch and (size + weight > BATCH_BYTES or len(batch) >= BATCH_ITEMS):
            flush()
        batch.append((case.id, item))
        size += weight
    flush()
    return totals


def pull(store: Store, out: Path, params: dict) -> int:
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for summary in store.pages('cases/', **params):
        case = store.load_case(summary['id'])
        case.save(out / case.id)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('push')
    p.add_argument('dirs', nargs='*', type=Path)
    p.add_argument('--tag', action='append', default=[])
    p.add_argument('--reviewed', action='store_true')
    q = sub.add_parser('pull')
    q.add_argument('--to', type=Path, required=True)
    q.add_argument('--suite')
    q.add_argument('--tag')
    q.add_argument('--status')
    args = parser.parse_args()

    store = Store.from_env()
    try:
        if args.cmd == 'push':
            targets = [(d, args.tag, args.reviewed) for d in args.dirs] or [
                (FIXTURES_DIR, ['fixture'], True), (INBOX_DIR, ['inbox'], False)]
            for root, tags, reviewed in targets:
                print(f'{root}: {push(store, root, tags, reviewed)}')
        else:
            params = {k: v for k, v in (('suite', args.suite), ('tag', args.tag), ('status', args.status)) if v}
            print(f'Wrote {pull(store, args.to, params)} cases to {args.to}')
    except StoreError as exc:
        sys.exit(f'Store refused: {exc}')


if __name__ == '__main__':
    main()
