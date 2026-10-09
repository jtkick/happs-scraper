#!/usr/bin/env python3
"""
Diagnose open missed-event reports (filed in Django admin) and feed the
fixes back into the crawl.

Usage:
    python tools/process_reports.py [--save-cases] [--dry-run]

For each report URL:
  fetch fails / robots.txt forbids   → blocked
  nothing extractable                → extraction_failed  (+ source relearn requested)
  extractable but ValidatePipeline
    would drop it                    → validation_dropped (reason attached)
  extractable and valid              → not_discovered: the crawl never reached
                                        it, so its listing page is added to the
                                        source's recipe events_urls

--save-cases puts each fetched page in review/inbox/report-<id>/ (with the
dropped events among its starting labels, since someone says they exist) so
the miss becomes a regression test once it's reviewed in tools/review.py.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.discovery.events_page import home_url, site_of
from scraper.eval.capture import FetchError, fetch, labels_from, new_case
from scraper.eval.case import INBOX_DIR, Case


def diagnose(case: Case) -> tuple[str, str]:
    parsed = case.parsed
    if parsed['events']:
        e = parsed['events'][0]
        return ('not_discovered',
                f"Extracts cleanly ({e.get('extraction_method')}): {e.get('title')} @ {e.get('start_datetime')}")
    if parsed['dropped']:
        return 'validation_dropped', '; '.join(
            f"{e['drop_reason']}: {e.get('title')}" for e in parsed['dropped'])
    return 'extraction_failed', 'No event could be extracted from the page.'


def listing_for(url: str) -> str:
    """/events/jazz-night → /events (the page that should have linked to it)."""
    parsed = urlparse(url)
    parent = parsed.path.rstrip('/').rsplit('/', 1)[0] or '/'
    return f'{parsed.scheme}://{parsed.netloc}{parent}'


def report_case(report: dict, url: str, html: str, source: dict | None, api_key: str) -> Case:
    case = new_case(url, html, kind='detail', seed_context=(source or {}).get('context') or {},
                    source='report', source_ref=report['id'], notes=report.get('notes', ''),
                    ai='live', api_key=api_key, case_id=f"report-{report['id'][-8:]}")
    case.events += [labels_from(e) for e in case.parsed['dropped']]
    return case


def main():
    parser = argparse.ArgumentParser(description='Diagnose missed-event reports')
    parser.add_argument('--save-cases', action='store_true',
                        help='save each page to review/inbox/ for tools/review.py')
    parser.add_argument('--dry-run', action='store_true', help='print diagnoses, change nothing')
    args = parser.parse_args()

    from scrapy.utils.project import get_project_settings
    from scraper.sources.client import BackendClient
    settings = get_project_settings()
    client = BackendClient.from_settings(settings)
    user_agent = settings.get('USER_AGENT')

    reports = client.open_reports()
    print(f'{len(reports)} open reports')
    for report in reports:
        url = report['url']
        source = client.source_by_domain(site_of(url))
        if source is None and not args.dry_run:
            client.upsert_sources([{'domain': site_of(url), 'homepage_url': home_url(url)}])
            source = client.source_by_domain(site_of(url))

        case = None
        try:
            html = fetch(url, user_agent=user_agent)
        except FetchError as exc:
            diagnosis, detail = 'blocked', str(exc)
        else:
            case = report_case(report, url, html, source, settings.get('ANTHROPIC_API_KEY', ''))
            diagnosis, detail = diagnose(case)
        print(f'  {url}\n    → {diagnosis}: {detail}')
        if args.dry_run:
            continue

        if source and not source.get('recipe_locked'):
            if diagnosis == 'not_discovered':
                recipe = dict(source.get('recipe') or {})
                urls = recipe.get('events_urls') or []
                listing = listing_for(url)
                if listing.rstrip('/') not in {u.rstrip('/') for u in urls}:
                    recipe['events_urls'] = urls + [listing]
                    client.update_source(source['id'], {'recipe': recipe, 'recipe_origin': 'manual'})
                    detail += f' — added {listing} to events_urls'
            elif diagnosis == 'extraction_failed':
                client.update_source(source['id'], {'relearn_requested': True})
                detail += ' — relearn requested'

        if args.save_cases and case:
            case.save(INBOX_DIR / case.id)
            detail += f' — case {case.id}'
        client.update_report(report['id'], {
            'status': 'diagnosed', 'diagnosis': diagnosis, 'diagnosis_detail': detail,
            'source': source['id'] if source else None,
        })


if __name__ == '__main__':
    main()
