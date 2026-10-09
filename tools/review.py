#!/usr/bin/env python3
"""
Review scraped pages: see what the scraper parsed next to the page, correct
it, and save the case as a fixture (tests/fixtures/) for tests and training.

Usage:
    python tools/review.py [--port 8765] [--no-reload]
    python tools/review.py sync        # pull new cases from the backend, no UI

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
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')

    if args.command == 'sync':
        from tools.review_app.sync import pull
        try:
            added = pull()
        except RuntimeError as exc:
            sys.exit(str(exc))
        print(f'{len(added)} new cases in review/inbox/' + ''.join(f'\n  {a}' for a in added))
        return

    import uvicorn
    print(f'Review app on http://{args.host}:{args.port}/')
    uvicorn.run('tools.review_app.app:app', host=args.host, port=args.port, app_dir=str(ROOT),
                reload=not args.no_reload,
                reload_dirs=[str(ROOT / 'scraper'), str(ROOT / 'tools' / 'review_app')],
                log_level='warning')


if __name__ == '__main__':
    main()
