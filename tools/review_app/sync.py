"""
Pull review cases from the backend into review/inbox/:

  crawl snapshots    pages the crawler saved (AI used, low confidence, drops,
                     empty listings, a random sample), with what it parsed then
  corrections        scraped events a curator (the backend's /staff/ console)
                     or an admin fixed, confirmed or rejected; the event as
                     they left it becomes the label, on the page as it was
                     when they did (pinned by the backend, captured on request).
                     A page a curator said lists several events becomes a case
                     whose events still need labelling
  page reviews       pages a curator checked as a whole: every event the page
                     produced as they left it, plus any the scraper missed,
                     as one case (complete when they said that's all of them)
  missed reports     event pages someone said the crawl missed

Snapshots, corrections and page reviews are marked claimed in the backend so
they're pulled once; reports are remembered in review/synced.json.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from scraper.discovery.events_page import site_of
from scraper.eval.capture import FetchError, fetch, labels_from, new_case
from scraper.eval.case import FIXTURES_DIR, INBOX_DIR, LABEL_FIELDS, ROOT, Case, slug
from scraper.eval.compare import similarity
from scraper.items import RECURRENCE_DEFAULTS
from scraper.pipelines import source_fingerprint
from scraper.sources.client import BackendClient
from tools.process_reports import report_case

LEDGER = ROOT / 'review' / 'synced.json'
# How long a correction waits for the crawler pool to capture its page before
# sync settles for the live page.
CAPTURE_WAIT = timedelta(hours=12)


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
            if waiting_for_capture(correction):
                continue
            case = correction_case(correction, client, api_key)
            if case is not None:
                _save(case, added)
            client.update_correction(correction['id'], {'status': 'claimed'})

        for page_review in client.page_reviews(status='pending'):
            if waiting_for_capture(page_review):
                continue
            case = page_review_case(page_review, client, api_key)
            if case is not None:
                _save(case, added)
            client.update_page_review(page_review['id'], {'status': 'claimed'})

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


def waiting_for_capture(correction: dict, now: datetime | None = None) -> bool:
    """The page this correction (or page review) pins hasn't been captured yet, and may still be: leave it till next sync."""
    snapshot = correction.get('snapshot') or {}
    if snapshot.get('status') != 'requested':
        return False
    created = datetime.fromisoformat(correction['created_at'].replace('Z', '+00:00'))
    return (now or datetime.now(timezone.utc)) - created < CAPTURE_WAIT


def _pinned_case(record: dict, url: str, prefix: str, source: str, client: BackendClient, api_key: str):
    """A case for the page a correction or review pins, else the live page; None if it can't be fetched."""
    snapshot = record.get('snapshot')
    html = client.snapshot_html(snapshot['id']) if snapshot else None
    case_id = _case_id(prefix, url, record['id'])
    if html is not None:
        case = snapshot_case(snapshot, html)
        case.id, case.source, case.source_ref = case_id, source, record['id']
        return case
    try:
        html = fetch(url)
    except FetchError:
        return None
    return new_case(url, html, source=source, source_ref=record['id'], ai='live', api_key=api_key,
                    seed_context=record.get('context') or {}, case_id=case_id)


def correction_case(correction: dict, client: BackendClient, api_key: str):
    """The corrected event's page, labelled with just that event as a curator or admin left it."""
    case = _pinned_case(correction, correction['source_url'], 'fix', 'correction', client, api_key)
    if case is None:
        return None

    before, after = correction.get('before') or {}, correction.get('after') or {}
    match = _corrected_event(case.parsed.get('events', []) + case.parsed.get('dropped', []), correction)

    case.complete = False
    who = correction.get('user') or 'An admin'
    if after.get('not_an_event'):
        case.events = []
        case.not_events = [before.get('title', '')]
        notes = f"{who} rejected “{before.get('title')}”."
    elif after.get('listing'):
        # The one event often has a real event's title (the first on the page),
        # so it can't go in not_events without a look.
        notes = (f"{who} said this page lists several events, which the scraper took for one, "
                 f"“{before.get('title')}”. Label every event on it (these are only what the scraper "
                 f"finds now), and add that title to not_events if it isn't one of them.")
    else:
        case.events = [correction_label(match, correction)]
        changed = correction.get('changed')
        if changed is None:                       # from before corrections kept the whole event
            changed = sorted(after)
        if correction.get('kind') == 'confirm' or not changed:
            notes = f"{who} confirmed “{after.get('title') or before.get('title')}” is right."
        else:
            notes = f"{who} corrected {', '.join(changed)}."
    case.notes = '\n'.join(n for n in (notes, correction.get('notes') or '') if n)
    return case


def page_review_case(page_review: dict, client: BackendClient, api_key: str):
    """
    A reviewed page, labelled with every event a curator kept and every one
    they added as missing. Events read from their own pages are labelled by
    title only: the listing names them, their details are on pages of their own.
    """
    url = page_review['url']
    case = _pinned_case(page_review, url, 'page', 'page_review', client, api_key)
    if case is None:
        return None
    parsed = case.parsed.get('events', []) + case.parsed.get('dropped', [])
    entries = page_review.get('events') or []
    kept = [e for e in entries if e['verdict'] in ('confirm', 'edit', 'checked')]
    labels = []
    for entry in kept:
        if entry.get('read_from') == url:
            labels.append(correction_label(_corrected_event(parsed, entry), {**entry, 'source_url': url}))
        else:
            labels.append({'title': entry['after'].get('title') or entry.get('title', '')})
    added = [labels_from(label) for label in page_review.get('missing') or []]
    rejected = [e for e in entries if e['verdict'] == 'reject']
    unchecked = [e for e in entries if e['verdict'] == 'unchecked']

    case.events = labels + added
    case.not_events = [e['title'] for e in rejected]
    case.complete = bool(page_review.get('complete')) and not unchecked
    who = page_review.get('user') or 'A curator'
    counts = [f'{len(kept)} kept', f"{sum(e['verdict'] == 'edit' for e in entries)} fixed",
              f'{len(rejected)} not events', f'{len(added)} missing']
    taken = [e['title'] for e in entries if e['verdict'] == 'listing']
    notes = [f"{who} reviewed the whole page: {', '.join(counts)}."
             + (' They said that’s every event on it.' if case.complete else '')]
    if any(e.get('read_from') != url for e in kept):
        notes.append('Events read from their own pages are labelled by title only.')
    if taken:
        notes.append(f"The scraper took the page’s list for one event, {', '.join(f'“{t}”' for t in taken)}; "
                     'that title is often the first real event’s, so it isn’t in not_events.')
    case.notes = '\n'.join([*notes, page_review.get('notes') or '']).strip()
    return case


def correction_label(match: dict | None, correction: dict) -> dict:
    """
    The label for a corrected event: what the crawl parsed, overridden by the
    corrected values. Left out unless the correction changed them: recurrence
    on an event that doesn't recur (as labels_from has it), and a url that's
    only the page's own address, which the scraper sends when it found none.
    """
    after, changed = correction.get('after') or {}, correction.get('changed') or []
    label = labels_from(match) if match else {}
    label.update({k: v for k, v in after.items() if k in LABEL_FIELDS})
    if not label.get('title'):
        label['title'] = (correction.get('before') or {}).get('title', '')
    if label.get('recurrence_freq', 'none') == 'none':
        for key in RECURRENCE_DEFAULTS:
            label.pop(key, None)
        if 'recurrence_freq' in changed:
            label['recurrence_freq'] = 'none'
    if 'url' not in changed and not (match or {}).get('url') and label.get('url') == correction.get('source_url'):
        label.pop('url')
    return label


def _corrected_event(parsed: list[dict], correction: dict) -> dict | None:
    """The parsed event the correction is about: same fingerprint, else the closest title."""
    fingerprint = correction.get('fingerprint') or ''
    prefix = fingerprint.rpartition(':')[0]
    if prefix:
        for event in parsed:
            if source_fingerprint(event, prefix) == fingerprint:
                return event
    title = (correction.get('before') or {}).get('title', '')
    match = max(parsed, key=lambda e: similarity(e.get('title', ''), title), default=None)
    if match is not None and similarity(match.get('title', ''), title) < 0.8:
        return None
    return match


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
