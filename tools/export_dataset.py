#!/usr/bin/env python3
"""
Export the reviewed cases in tests/fixtures/ as AI training data.

Usage:
    python tools/export_dataset.py [--out-dir dataset] [--eval-share 0.2]

Writes:
    train.jsonl / eval.jsonl   {id, system, messages: [user, assistant]} — the exact prompt the
                               crawler sends for the page (scraper/extractors/ai.py) and the
                               answer it should get. Split by site, so no site is in both.
    corrections.jsonl          what the scraper parsed at capture time next to the labels,
                               for error analysis or preference data

Cases that can't be used (partial labels, too little text, an event with no
evidence on the page) are listed with the reason.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.eval.case import load_all
from scraper.eval.dataset import correction_record, split_of, training_record


def main():
    parser = argparse.ArgumentParser(description='Export reviewed cases as training data',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out-dir', default='dataset')
    parser.add_argument('--eval-share', type=float, default=0.2)
    args = parser.parse_args()

    cases = load_all()
    if not cases:
        sys.exit('No reviewed cases in tests/fixtures/. Review some with tools/review.py.')
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {name: (out / f'{name}.jsonl').open('w') for name in ('train', 'eval', 'corrections')}
    counts = {'train': 0, 'eval': 0, 'corrections': 0}
    skipped = []
    try:
        for case in cases:
            correction = correction_record(case)
            if correction:
                files['corrections'].write(json.dumps(correction, ensure_ascii=False) + '\n')
                counts['corrections'] += 1
            record, problem = training_record(case)
            if record is None:
                skipped.append((case.id, problem))
                continue
            split = split_of(case, args.eval_share)
            files[split].write(json.dumps(record, ensure_ascii=False) + '\n')
            counts[split] += 1
    finally:
        for f in files.values():
            f.close()

    print(f"{counts['train']} train, {counts['eval']} eval, {counts['corrections']} corrections → {out}/")
    for case_id, problem in skipped:
        print(f'  skipped {case_id}: {problem}')


if __name__ == '__main__':
    main()
