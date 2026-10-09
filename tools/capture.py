#!/usr/bin/env python3
"""
Capture a web page as a case for review (scraper/eval/case.py).

Usage:
    python tools/capture.py <url> [options]

Options:
    --kind listing|detail  What the page is (default: listing)
    --venue NAME           Venue name (seed context, as a source's context supplies)
    --address TEXT         Venue address (seed context)
    --lat FLOAT / --lon FLOAT  Venue coordinates (seed context)
    --timezone ZONE        IANA zone for times the page prints without an offset
    --from-file PATH       Use a local HTML file instead of fetching the URL
    --captured-at ISO      When the page was saved (default: now); dates are read as of then
    --no-render            Fetch the raw HTML instead of rendering with Playwright (as the crawl does)
    --no-ai                Don't call the model even when ANTHROPIC_API_KEY is set
    --id SLUG              Case id (default: from the first event's title)
    --notes TEXT           What this case is for
    --edit                 Edit case.json in $EDITOR and save straight to tests/fixtures/

The page is parsed exactly as a crawl would parse it, with the model's answer
recorded so tests replay it offline. Without --edit the case goes to
review/inbox/ for tools/review.py.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.eval.capture import FetchError, fetch, new_case, promote
from scraper.eval.case import CASE_FILE, INBOX_DIR, Case


def _edit(case: Case) -> Case:
    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'nano'
    cmd = [editor] + (['--wait'] if os.path.basename(editor) in ('code', 'code-insiders') else [])
    with tempfile.TemporaryDirectory() as tmp:
        case.save(Path(tmp) / case.id)
        path = Path(tmp) / case.id / CASE_FILE
        subprocess.run(cmd + [str(path)], check=True)
        json.loads(path.read_text())
        edited = Case.load(path.parent)
    return edited


def main():
    parser = argparse.ArgumentParser(description='Capture a web page as a review case',
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('url')
    parser.add_argument('--kind', choices=('listing', 'detail'), default='listing')
    parser.add_argument('--venue', default='')
    parser.add_argument('--address', default='')
    parser.add_argument('--lat', type=float)
    parser.add_argument('--lon', type=float)
    parser.add_argument('--timezone', default='')
    parser.add_argument('--from-file', metavar='PATH')
    parser.add_argument('--captured-at', metavar='ISO')
    parser.add_argument('--no-render', action='store_true')
    parser.add_argument('--no-ai', action='store_true')
    parser.add_argument('--id', dest='case_id')
    parser.add_argument('--notes', default='')
    parser.add_argument('--edit', action='store_true')
    args = parser.parse_args()

    if args.from_file:
        html = Path(args.from_file).read_text()
    else:
        print(f'Fetching {args.url} …')
        try:
            html = fetch(args.url, render=not args.no_render)
        except FetchError as exc:
            sys.exit(f'Fetch failed: {exc}')

    context = {k: v for k, v in {
        'location_title': args.venue, 'location_address': args.address,
        'location_lat': args.lat, 'location_lon': args.lon, 'timezone': args.timezone,
    }.items() if v not in (None, '')}
    case = new_case(args.url, html, kind=args.kind, seed_context=context, notes=args.notes,
                    ai=None if args.no_ai else 'live', api_key=os.getenv('ANTHROPIC_API_KEY', ''),
                    captured_at=args.captured_at, case_id=args.case_id)

    parsed = case.parsed
    print(f"Strategy {parsed['strategy'] or '-'}: {len(parsed['events'])} events, "
          f"{len(parsed['dropped'])} dropped{' (AI used)' if parsed['ai_used'] else ''}")
    for e in parsed['events']:
        print(f"  ✓ {e.get('title')!r} @ {e.get('start_datetime')}")
    for e in parsed['dropped']:
        print(f"  ✗ {e.get('title')!r} — {e.get('drop_reason')}")

    if args.edit:
        target = promote(_edit(case))
        print(f'Saved {target}')
    else:
        case.save(INBOX_DIR / case.id)
        print(f'Saved {INBOX_DIR / case.id} — review it with: python tools/review.py')


if __name__ == '__main__':
    main()
