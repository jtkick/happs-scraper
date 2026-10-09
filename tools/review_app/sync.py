"""
Pull review cases from the backend into review/inbox/:

  crawl snapshots    pages the crawler saved (AI used, low confidence, drops,
                     empty listings, a random sample), with what it parsed then
  admin corrections  scraped events someone edited or rejected in Django admin;
                     the edit becomes the starting label for that event
  missed reports     event pages someone said the crawl missed

Snapshots and corrections are marked claimed in the backend so they're
pulled once; reports are remembered in review/synced.json.
"""
from __future__ import annotations
import json
import os
from urllib.parse import urlparse

from scraper.discovery.events_page import site_of
from scraper.eval.capture import FetchError, fetch, labels_from, new_case
from scraper.eval.case import FIXTURES_DIR, INBOX_DIR, LABEL_FIELDS, ROOT, Case, slug
from scraper.eval.compare import similarity
from scraper.sources.client import BackendClient
from tools.process_reports import report_case

LEDGER = ROOT / 'review' / 'synced.json'


def pull() -> list[str]:
    base, token = os.getenv('HAPPS_API_BASE', ''), os.getenv('HAPPS_SCRAPER_TOKEN', '')
    if not base or not token:
        raise RuntimeError('Set HAPPS_API_BASE and HAPPS_SCRAPER_TOKEN (in .env) to sync from the backend.')
    client = BackendClient(base, token)
    api_key = os.getenv('ANTHROPIC_API_KEY', '')
    seen = _ledger()
    added = []
    try:
        for snapshot in client.snapshots(status='pending'):
            html = client.snapshot_html(snapshot['id'])
            if html is None:
                continue
            case = snapshot_case(snapshot, html)
            _save(case, added)
            client.update_snapshot(snapshot['id'], {'status': 'claimed'})

        for correction in client.corrections(status='pending'):
            case = correction_case(correction, client, api_key)
            if case is not None:
                _save(case, added)
            client.update_correction(correction['id'], {'status': 'claimed'})

        for report in client.reports():
            key = f"report:{report['id']}"
            if key in seen or report.get('status') in ('fixed', 'wontfix'):
                continue
            try:
                html = fetch(report['url'])
            except FetchError:
                continue
            source = client.source_by_domain(site_of(report['url']))
            _save(report_case(report, report['url'], html, source, api_key), added)
            seen.add(key)
    finally:
        _save_ledger(seen)
        client.close()
    return added


def snapshot_case(snapshot: dict, html: str) -> Case:
    ctx = snapshot.get('context') or {}
    case = new_case(
        snapshot['url'], html, kind=ctx.get('kind') or 'listing',
        seed_context=ctx.get('seed_context') or {}, partial=ctx.get('partial'),
        recipe=ctx.get('recipe') or {}, platform=ctx.get('platform'), source='crawl',
        source_ref=snapshot['id'], notes=f"Crawl snapshot: {snapshot.get('reason', '')}",
        ai='replay', ai_response=snapshot.get('ai_response'), captured_at=snapshot['captured_at'],
        case_id=_case_id('crawl', snapshot['url'], snapshot['id']))
    if snapshot.get('parsed'):
        case.parsed = snapshot['parsed']     # what the crawl itself produced, not today's re-run
    return case


def correction_case(correction: dict, client: BackendClient, api_key: str):
    """The corrected event's page, labelled with just that event as an admin left it."""
    snapshot = correction.get('snapshot')
    html = client.snapshot_html(snapshot['id']) if snapshot else None
    if html is not None:
        case = snapshot_case(snapshot, html)
        case.id = _case_id('fix', correction['source_url'], correction['id'])
        case.source, case.source_ref = 'correction', correction['id']
    else:
        try:
            html = fetch(correction['source_url'])
        except FetchError:
            return None
        case = new_case(correction['source_url'], html, source='correction',
                        source_ref=correction['id'], ai='live', api_key=api_key,
                        seed_context=correction.get('context') or {},
                        case_id=_case_id('fix', correction['source_url'], correction['id']))

    before, after = correction.get('before') or {}, correction.get('after') or {}
    parsed = case.parsed.get('events', []) + case.parsed.get('dropped', [])
    match = max(parsed, key=lambda e: similarity(e.get('title', ''), before.get('title', '')), default=None)
    if match is not None and similarity(match.get('title', ''), before.get('title', '')) < 0.8:
        match = None

    case.complete = False
    who = correction.get('user') or 'An admin'
    if after.get('not_an_event'):
        case.events = []
        case.not_events = [before.get('title', '')]
        case.notes = f"{who} rejected “{before.get('title')}” in admin."
    else:
        label = labels_from(match) if match else {}
        label.update({k: v for k, v in after.items() if k in LABEL_FIELDS})
        label.setdefault('title', before.get('title', ''))
        case.events = [label]
        changed = ', '.join(sorted(after)) or 'nothing'
        case.notes = f"{who} corrected {changed} in admin."
    return case


def _save(case: Case, added: list[str]) -> None:
    if (INBOX_DIR / case.id).exists() or (FIXTURES_DIR / case.id).exists():
        return
    case.save(INBOX_DIR / case.id)
    added.append(case.id)


def _case_id(kind: str, url: str, ref: str) -> str:
    """crawl-venue-test-events-1f2e3d4c: backend ids are UUIDv7, so the random tail tells them apart."""
    parsed = urlparse(url)
    return f"{kind}-{slug(f'{parsed.netloc} {parsed.path}')[:40].rstrip('-')}-{str(ref)[-8:]}"


def _ledger() -> set[str]:
    try:
        return set(json.loads(LEDGER.read_text()))
    except (OSError, ValueError):
        return set()


def _save_ledger(seen: set[str]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(sorted(seen), indent=1) + '\n')
