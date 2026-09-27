#!/usr/bin/env python3
"""
Export the fixture corpus as a JSONL fine-tuning dataset.

One record is emitted per fixture that has clean_text.txt and enough non-null
expected fields.  Output format matches the Anthropic messages API fine-tuning
schema (user + assistant turn).

Usage:
    python tools/export_finetune.py [--output dataset.jsonl] [--min-fields N]

Options:
    --output FILE     Output path  (default: dataset.jsonl)
    --min-fields N    Skip fixtures with fewer than N non-null expected fields
                      (default: 2 — must have at least title + start_datetime)
    --stats           Print per-fixture stats before writing

The system prompt, user turn and output shape are imported from
scraper/extractors/ai.py, so fine-tuned models are a drop-in replacement for
the live Haiku calls. `evidence` comes from the fixture when recorded.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

FIXTURES_DIR = Path(__file__).parent.parent / 'tests' / 'fixtures'

from scraper.extractors.ai import OUTPUT_KEYS, _SYSTEM, build_user_message


def _load_fixtures() -> list[dict]:
    items = []
    if not FIXTURES_DIR.exists():
        return items
    for d in sorted(FIXTURES_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = d / 'fixture.json'
        text = d / 'clean_text.txt'
        if not meta.exists() or not text.exists():
            continue
        data = json.loads(meta.read_text())
        items.append({
            'id':         d.name,
            'url':        data.get('url', ''),
            'clean_text': text.read_text().strip(),
            'expected':   data.get('expected', {}),
            'expected_events': data.get('expected_events', []),
            'notes':      data.get('notes', ''),
        })
    return items


def _non_null_count(d: dict) -> int:
    return sum(1 for v in d.values() if v is not None)


def main():
    parser = argparse.ArgumentParser(
        description='Export fixture corpus as fine-tuning JSONL',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--output',     default='dataset.jsonl', metavar='FILE')
    parser.add_argument('--min-fields', default=2, type=int,
                        help='Minimum non-null expected fields to include a fixture')
    parser.add_argument('--stats',      action='store_true',
                        help='Print per-fixture stats')
    args = parser.parse_args()

    fixtures = _load_fixtures()
    if not fixtures:
        print("No fixtures with clean_text.txt found in tests/fixtures/")
        print("Run tools/capture.py to create some.")
        sys.exit(0)

    out_path = Path(args.output)
    written = skipped = 0

    with out_path.open('w') as f:
        for fx in fixtures:
            expected = fx['expected']
            n_fields = min((_non_null_count(e) for e in fx['expected_events']),
                           default=_non_null_count(expected))

            if args.stats:
                status = 'ok' if n_fields >= args.min_fields else f'skip (<{args.min_fields} fields)'
                print(f"  {fx['id']:<40} {n_fields:2} fields  {status}")

            if n_fields < args.min_fields:
                skipped += 1
                continue

            # Listing fixtures carry expected_events; detail fixtures a single `expected`.
            events = fx['expected_events'] or [expected]
            assistant_output = {'events': [{k: e.get(k) for k in OUTPUT_KEYS} for e in events]}

            record = {
                'system': _SYSTEM,
                'messages': [
                    {
                        'role':    'user',
                        'content': build_user_message(fx['clean_text'], url=fx['url']),
                    },
                    {
                        'role':    'assistant',
                        'content': json.dumps(assistant_output, ensure_ascii=False),
                    },
                ],
            }
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
            written += 1

    print(f"Wrote {written} records to {out_path}  ({skipped} skipped — fewer than {args.min_fields} non-null fields)")
    if written:
        size_kb = out_path.stat().st_size / 1024
        print(f"Dataset size: {size_kb:.1f} KB")


if __name__ == '__main__':
    main()
