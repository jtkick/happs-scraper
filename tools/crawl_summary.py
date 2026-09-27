#!/usr/bin/env python3
"""
Print the backend's crawl health roll-up; exit 1 if any source broke in the
window, so a scheduled CI job fails (and notifies) when sites stop working.

Usage:
    python tools/crawl_summary.py [--hours 24] [--no-fail]
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    parser = argparse.ArgumentParser(description='Crawl health summary')
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--no-fail', action='store_true', help='always exit 0')
    args = parser.parse_args()

    from scrapy.utils.project import get_project_settings
    from scraper.sources.client import BackendClient
    data = BackendClient.from_settings(get_project_settings()).summary(args.hours)
    if data is None:
        sys.exit('Could not reach the backend summary endpoint')

    print(f'Sources: {data["sources_by_status"]}')
    print(f'Runs (last {args.hours}h): {data["runs"]}')
    print(f'Review queue: {data["review_queue"]}   Open missed-event reports: {data["open_missed_reports"]}')
    broken = data['newly_broken']
    if broken:
        print(f'Newly broken ({len(broken)}):')
        for domain in broken:
            print(f'  - {domain}')
        if not args.no_fail:
            sys.exit(1)


if __name__ == '__main__':
    main()
