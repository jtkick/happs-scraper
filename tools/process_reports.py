#!/usr/bin/env python3
"""
Diagnose open missed-event reports (filed in Django admin) and feed the
fixes back into the crawl.

Usage:
    python tools/process_reports.py [--save-fixtures] [--dry-run]

For each report URL:
  fetch fails / robots.txt forbids   → blocked
  nothing extractable                → extraction_failed  (+ source relearn requested)
  extractable but ValidatePipeline
    would drop it                    → validation_dropped (reason attached)
  extractable and valid              → not_discovered: the crawl never reached
                                        it, so its listing page is added to the
                                        source's recipe events_urls

--save-fixtures writes tests/fixtures/report-<id>/ so the miss becomes a
regression test once its expected output is reviewed.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from scrapy.exceptions import DropItem

from scraper import extraction
from scraper.discovery.events_page import home_url, site_of
from scraper.items import EventItem
from scraper.pipelines import NormalizePipeline, ValidatePipeline

FIXTURES_DIR = Path(__file__).parent.parent / 'tests' / 'fixtures'


def fetch(url: str, user_agent: str) -> tuple[str | None, str]:
    robots = robotparser.RobotFileParser(home_url(url) + 'robots.txt')
    try:
        robots.read()
        if not robots.can_fetch(user_agent, url):
            return None, 'robots.txt disallows this URL'
    except Exception:
        pass
    try:
        resp = requests.get(url, headers={'User-Agent': user_agent}, timeout=20)
    except requests.RequestException as exc:
        return None, str(exc)
    if not resp.ok:
        return None, f'HTTP {resp.status_code}'
    return resp.text, ''


def diagnose(url: str, html: str, source: dict | None, settings) -> tuple[str, str, list[dict]]:
    ai = None
    if settings.get('ANTHROPIC_API_KEY'):
        from scraper.extractors import ai as ai_module
        key = settings.get('ANTHROPIC_API_KEY')
        ai = lambda h, u: ai_module.extract_many(h, u, key)  # noqa: E731
    context = (source or {}).get('context') or {}
    result = extraction.extract_page(html, url, ai=ai)
    text = extraction.page_text(html) if result.single else None
    events = [e for e in (extraction.finalize(d, context=context, jsonld_node=n, page_text=text)
                          for d, n in result.events) if e]
    if not events:
        return 'extraction_failed', 'No event could be extracted from the page.', []

    normalize, validate = NormalizePipeline(), ValidatePipeline()
    reasons = []
    for data in events:
        item = EventItem({k: v for k, v in data.items() if k in EventItem.fields})
        try:
            validate.process_item(normalize.process_item(item, None), settings_spider(settings))
            return ('not_discovered',
                    f"Extracts cleanly ({data.get('extraction_method')}): {data.get('title')} "
                    f"@ {item.get('start_datetime')}", events)
        except DropItem as exc:
            reasons.append(str(exc))
    return 'validation_dropped', '; '.join(reasons), events


def settings_spider(settings):
    return type('ToolSpider', (), {'settings': settings})()


def listing_for(url: str) -> str:
    """/events/jazz-night → /events (the page that should have linked to it)."""
    parsed = urlparse(url)
    parent = parsed.path.rstrip('/').rsplit('/', 1)[0] or '/'
    return f'{parsed.scheme}://{parsed.netloc}{parent}'


def save_fixture(report: dict, url: str, html: str, events: list[dict]):
    target = FIXTURES_DIR / f"report-{report['id'][:8]}"
    target.mkdir(parents=True, exist_ok=True)
    (target / 'page.html').write_text(html)
    (target / 'clean_text.txt').write_text(extraction.page_text(html) or '')
    fields = ('title', 'start_datetime', 'end_datetime', 'location_title', 'url')
    (target / 'fixture.json').write_text(json.dumps({
        'url': url,
        'notes': f"Missed-event report {report['id']}: {report.get('notes', '')} "
                 '— REVIEW expected values before relying on this fixture.',
        'reviewed': False,
        'expected_events': [{k: e.get(k) for k in fields} for e in events],
    }, indent=2, default=str))
    return target


def main():
    parser = argparse.ArgumentParser(description='Diagnose missed-event reports')
    parser.add_argument('--save-fixtures', action='store_true')
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

        html, error = fetch(url, user_agent)
        if html is None:
            diagnosis, detail, events = 'blocked', error, []
        else:
            diagnosis, detail, events = diagnose(url, html, source, settings)
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

        if args.save_fixtures and html:
            detail += f' — fixture {save_fixture(report, url, html, events).name}'
        client.update_report(report['id'], {
            'status': 'diagnosed', 'diagnosis': diagnosis, 'diagnosis_detail': detail,
            'source': source['id'] if source else None,
        })


if __name__ == '__main__':
    main()
