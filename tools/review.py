#!/usr/bin/env python3
"""
Review scraped pages: see what the scraper parsed next to the page, correct
it, and save the case as a fixture (tests/fixtures/) for tests and training.

Usage:
    python tools/review.py [--port 8765] [--no-reload]
    python tools/review.py sync        # pull new cases from the backend, no UI
    python tools/review.py sync --store   # … into the backend's eval store instead of review/inbox/

The server restarts whenever scraper/ changes, so "Re-run" always parses with
the code as it is on disk. Needs requirements-dev.txt.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description='Scraper review app')
    parser.add_argument('command', nargs='?', choices=('serve', 'sync'), default='serve')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-reload', action='store_true')
    parser.add_argument('--store', action='store_true', help='sync into the eval store (api/eval/)')
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')

    if args.command == 'sync':
        from scraper.eval.store import Store, StoreError
        from tools.review_app.sync import pull
        try:
            added = pull(Store.from_env() if args.store else None)
        except (RuntimeError, StoreError) as exc:
            sys.exit(str(exc))
        where = 'the eval store' if args.store else 'review/inbox/'
        print(f'{len(added)} new cases in {where}' + ''.join(f'\n  {a}' for a in added))
        return

    import uvicorn
    print(f'Review app on http://{args.host}:{args.port}/')
    uvicorn.run('tools.review_app.app:app', host=args.host, port=args.port, app_dir=str(ROOT),
                reload=not args.no_reload,
                reload_dirs=[str(ROOT / 'scraper'), str(ROOT / 'tools' / 'review_app')],
                log_level='warning')


if __name__ == '__main__':
    main()
