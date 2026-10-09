#!/usr/bin/env python3
"""
Score the scraper on the reviewed cases in tests/fixtures/.

Usage:
    python tools/evaluate.py [--inbox] [--ai replay|live|none] [--model ID]
                             [--json OUT] [--baseline FILE]

    --inbox           Score review/inbox/ instead (unreviewed: labels are the scraper's own guesses)
    --ai live         Ask the model afresh (compare prompts or models; costs credits)
    --model ID        Model for --ai live (default: the crawler's)
    --json OUT        Save the scorecard, e.g. as a baseline for later runs
    --baseline FILE   Show what got worse or better since a saved scorecard; exit 1 if worse
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.eval.case import FIXTURES_DIR, INBOX_DIR, load_all
from scraper.eval.scorecard import regressions, scorecard


def _pct(value) -> str:
    return '   –' if value is None else f'{value * 100:4.0f}%'


def main():
    parser = argparse.ArgumentParser(description='Score the scraper on reviewed cases',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--inbox', action='store_true')
    parser.add_argument('--ai', choices=('replay', 'live', 'none'), default='replay')
    parser.add_argument('--model')
    parser.add_argument('--json', metavar='OUT')
    parser.add_argument('--baseline', metavar='FILE')
    args = parser.parse_args()

    if args.ai == 'live':
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent.parent / '.env')
    cases = load_all(INBOX_DIR if args.inbox else FIXTURES_DIR)
    if not cases:
        sys.exit('No cases to score. Capture some with tools/review.py.')
    card = scorecard(cases, ai=None if args.ai == 'none' else args.ai,
                     api_key=os.getenv('ANTHROPIC_API_KEY', ''), model=args.model)

    print(f"{card['cases']} cases, {card['events']} labelled events")
    print(f"  recall    {_pct(card['recall'])}   (labelled events the scraper found)")
    print(f"  precision {_pct(card['precision'])}   (parsed events that are real)\n")
    print('Field accuracy (where labels assert the field):')
    for name, f in sorted(card['fields'].items(), key=lambda kv: kv[1]['accuracy']):
        print(f"  {name:<22} {_pct(f['accuracy'])}  {f['right']:>4}/{f['asserted']}")
    print('\nBy extraction method:')
    for name, m in card['methods'].items():
        print(f"  {name:<22} {m['events']:>4} events   fields {_pct(m['accuracy'])}")

    failing = {k: v for k, v in card['per_case'].items() if v['failures']}
    if failing:
        print(f'\nCases with mismatches ({len(failing)}):')
        for case_id, c in failing.items():
            new = set(c['new_failures'])
            shown = ', '.join(f"{p}{' (new)' if p in new else ''}" for p in c['failures'][:6])
            more = f' … +{len(c["failures"]) - 6}' if len(c['failures']) > 6 else ''
            print(f'  {case_id}: {shown}{more}')
    missing_ai = [k for k, v in card['per_case'].items() if v['ai_missing']]
    if missing_ai:
        print(f"\nNo recorded AI answer (re-run with live AI in the review app): {', '.join(missing_ai)}")

    if args.json:
        Path(args.json).write_text(json.dumps(card, indent=1) + '\n')
        print(f'\nSaved {args.json}')

    if args.baseline:
        diff = regressions(card, json.loads(Path(args.baseline).read_text()))
        for label, cases_ in (('Worse', diff['worse']), ('Better', diff['better'])):
            for case_id, paths in cases_.items():
                print(f"{label}: {case_id}: {', '.join(paths)}")
        if not diff['worse'] and not diff['better']:
            print('\nNo change from the baseline.')
        if diff['worse']:
            sys.exit(1)


if __name__ == '__main__':
    main()
