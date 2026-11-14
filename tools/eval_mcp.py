#!/usr/bin/env python3
"""
MCP server (stdio) for building test data that measures the scraper, over the
backend's eval store (api/eval/). Pages are captured and previewed with the
code on disk, so what an agent sees is what the crawl would do.

Run by an MCP client:  .venv/bin/python tools/eval_mcp.py
Needs HAPPS_API_BASE and HAPPS_EVAL_TOKEN (from `manage.py eval_agent`) in .env;
ANTHROPIC_API_KEY only for ai="live".
"""
from __future__ import annotations
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from scraper.eval.compare import compare
from scraper.eval.run import run_case
from scraper.eval.store import Store, StoreError, case_from_api

INSTRUCTIONS = """\
Build and score test cases for the Abuzz event scraper.

A case is a saved page, the context it's parsed in (venue name, address, timezone), and labels:
the events the page should give. In a label a key asserts its value, a missing key isn't checked,
and null asserts the field is empty. Times without an offset are read in the case's timezone.
Call eval_schema for the label fields and tags.

Typical loop: capture_page (or create_case for HTML you write, under a .test URL) → read_page_text
→ fix labels (labels start as the scraper's own parse, so check each against the page) → preview →
mark_reviewed. Every write takes the case's current `version`; on a version conflict, re-read the
case and apply your change again. Measure the scraper with run_suite (needs a worker running:
tools/eval_worker.py) and compare_runs.
"""

mcp = MCPServer('happs-eval', instructions=INSTRUCTIONS)
_store: Optional[Store] = None


def store() -> Store:
    global _store
    if _store is None:
        try:
            _store = Store.from_env()
        except StoreError as exc:
            raise ToolError(str(exc))
    return _store


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except StoreError as exc:
        raise ToolError(str(exc))


def _api_key() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return os.getenv('ANTHROPIC_API_KEY', '')


def _brief_case(data: dict) -> dict:
    """A case without its page HTML or capture parse, for an agent to read."""
    keep = ('id', 'external_id', 'version', 'status', 'kind', 'url', 'context', 'partial', 'platform',
            'complete', 'not_events', 'known_failures', 'notes', 'tags', 'source', 'reviewer', 'warnings')
    out = {k: data[k] for k in keep if k in data}
    out['page_id'] = data['page']['id'] if isinstance(data.get('page'), dict) else data.get('page_id')
    out['captured_at'] = data.get('captured_at')
    out['labels'] = [{'id': lb['id'], 'index': lb['index'], **({'note': lb['note']} if lb.get('note') else {}),
                      'fields': lb['fields']} for lb in data.get('labels', [])]
    out['has_ai_response'] = bool(data.get('ai_response'))
    return out


def _brief_event(e: dict) -> dict:
    keys = ('title', 'start_datetime', 'end_datetime', 'location_title', 'recurrence_freq', 'url',
            'extraction_method', 'drop_reason')
    return {k: e[k] for k in keys if e.get(k) not in (None, '', 'none')}


# ── Reference ─────────────────────────────────────────────────────────────────

@mcp.tool()
def eval_schema() -> dict:
    """Label fields, allowed tags and recurrence values, case kinds and statuses, and size limits."""
    return call(store().get, 'schema/')


# ── Pages and cases ───────────────────────────────────────────────────────────

def _create(body: dict, base_id: Optional[str]) -> dict:
    """POST the case, adding -2, -3 … to a generated external_id already taken."""
    if base_id is None:
        return call(store().post, 'cases/', body)
    for n in range(1, 50):
        ext = base_id if n == 1 else f'{base_id}-{n}'
        try:
            return store().post('cases/', {**body, 'external_id': ext})
        except StoreError as exc:
            if exc.status != 409:
                raise ToolError(str(exc))
    raise ToolError('Could not find a free external_id.')


def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')[:80] or 'case'


@mcp.tool()
def capture_page(url: str, kind: str = 'listing', venue_name: Optional[str] = None,
                 venue_address: Optional[str] = None, timezone: str = 'America/New_York',
                 render: bool = True, ai: str = 'live', external_id: Optional[str] = None,
                 tags: Optional[list[str]] = None, notes: str = '') -> dict:
    """Fetch a live page as the crawl would (robots.txt obeyed, rendered by default), parse it, and save
    it as a draft case whose labels are the scraper's parse. Check every label against read_page_text.
    kind: 'listing' (a page of events) or 'detail' (one event's page). ai: 'live', 'none'."""
    from scraper.eval.capture import FetchError, fetch, new_case
    try:
        html = fetch(url, render=render)
    except FetchError as exc:
        raise ToolError(f'Could not fetch {url}: {exc}')
    context = {k: v for k, v in (('location_title', venue_name), ('location_address', venue_address),
                                 ('timezone', timezone)) if v}
    case = new_case(url, html, kind=kind, seed_context=context, ai=None if ai == 'none' else 'live',
                    api_key=_api_key(), notes=notes)
    body = {'page': {'url': url, 'html': html, 'captured_at': case.captured_at, 'origin': 'captured'},
            'kind': kind, 'context': context, 'parsed': case.parsed, 'labels': case.events,
            'tags': tags or [], 'notes': notes}
    if case.ai_response:
        body['ai_response'] = {**case.ai_response, 'origin': 'capture'}
    data = _create(body, external_id) if external_id else _create(body, _slug(case.id))
    return {**_brief_case(data), 'parsed': [_brief_event(e) for e in case.parsed['events']],
            'dropped': [_brief_event(e) for e in case.parsed['dropped']]}


@mcp.tool()
def create_case(url: str, labels: list[dict], html: Optional[str] = None, page_id: Optional[str] = None,
                kind: str = 'listing', context: Optional[dict] = None, captured_at: Optional[str] = None,
                complete: bool = True, not_events: Optional[list[str]] = None, external_id: Optional[str] = None,
                tags: Optional[list[str]] = None, notes: str = '') -> dict:
    """Save a case from HTML you supply (a synthetic page must use a URL under .test, e.g.
    https://brewpub.test/events) or an existing page_id, with the labels you expect. The page is parsed
    without AI so the case has a capture parse. captured_at fixes "today" for the page (ISO datetime).
    context: {location_title, location_address, location_lat, location_lon, venue_type, timezone}."""
    from scraper.eval.case import Case
    from scraper.eval.capture import reparse
    context = context or {}
    if page_id:
        page = call(store().get, f'pages/{page_id}/')
        html, url, captured_at = store().html(page_id), page['url'], captured_at or page['captured_at']
        page_body = {'page_id': page_id}
    elif html:
        host = urlparse(url).hostname or ''
        page_body = {'page': {'url': url, 'html': html, 'captured_at': captured_at,
                              'origin': 'synthetic' if host.endswith('.test') else 'captured'}}
    else:
        raise ToolError('Send html or page_id.')
    from scraper.util import now_iso
    case = Case(id='', url=url, html=html, kind=kind, captured_at=captured_at or now_iso(), seed_context=context)
    reparse(case, ai=None)
    page_body.get('page', {}).setdefault('captured_at', case.captured_at)
    body = {**page_body, 'kind': kind, 'context': context, 'labels': labels, 'complete': complete,
            'not_events': not_events or [], 'tags': tags or [], 'notes': notes, 'parsed': case.parsed}
    data = _create(body, external_id) if external_id else _create(
        body, _slug((labels[0].get('title') if labels else '') or urlparse(url).hostname or 'case'))
    return _brief_case(data)


@mcp.tool()
def derive_case(case_id: str, external_id: Optional[str] = None, replace: Optional[list[dict]] = None,
                remove_selectors: Optional[list[str]] = None, html: Optional[str] = None,
                overrides: Optional[dict] = None) -> dict:
    """A variant of a case on edited HTML: replace [{find, replace}] text, remove elements matching CSS
    selectors, or give whole new html. Labels are copied; edit them to match the change.
    overrides: case fields to change too (context, kind, complete, labels, …)."""
    source = call(store().get, f'cases/{case_id}/')
    page_id = source['page']['id']
    if remove_selectors or html is not None:
        text = html if html is not None else call(store().html, page_id)
        if remove_selectors:
            text = _remove(text, remove_selectors)
        for op in replace or []:
            if op['find'] not in text:
                raise ToolError(f"Not in the page: {op['find']!r}")
            text = text.replace(op['find'], op['replace'])
        page = call(store().post, f'pages/{page_id}/derive/', {'html': text})
    elif replace:
        page = call(store().post, f'pages/{page_id}/derive/', {'replace': replace})
    else:
        raise ToolError('Send replace, remove_selectors or html.')
    data = call(store().post, f'cases/{case_id}/clone/', {
        'external_id': external_id, 'overrides': {**(overrides or {}), 'page_id': page['id']}})
    return _brief_case(data)


def _remove(html: str, selectors: list[str]) -> str:
    from parsel import Selector
    from lxml import html as lxml_html
    tree = lxml_html.fromstring(html)
    doc = Selector(root=tree)
    removed = 0
    for css in selectors:
        for node in doc.css(css):
            el = node.root
            if el.getparent() is not None:
                el.getparent().remove(el)
                removed += 1
    if not removed:
        raise ToolError(f'No elements matched {selectors}.')
    return lxml_html.tostring(tree, encoding='unicode', doctype='<!DOCTYPE html>')


@mcp.tool()
def read_page_text(case_id: Optional[str] = None, page_id: Optional[str] = None, offset: int = 0,
                   limit: int = 6000) -> dict:
    """The page's text as the scraper's model reads it, in pieces (offset/limit in characters).
    Use it to check labels against what the page actually says."""
    from scraper.extractors import ai
    if case_id:
        page_id = call(store().get, f'cases/{case_id}/')['page']['id']
    if not page_id:
        raise ToolError('Send case_id or page_id.')
    html = call(store().html, page_id)
    text = ai.page_text(html) or ''
    return {'page_id': page_id, 'length': len(text), 'offset': offset,
            'text': text[offset:offset + max(1, min(limit, 20000))],
            'more': offset + limit < len(text)}


@mcp.tool()
def find_cases(tag: Optional[str] = None, status: Optional[str] = None, kind: Optional[str] = None,
               domain: Optional[str] = None, q: Optional[str] = None, suite: Optional[str] = None,
               failing_in: Optional[str] = None, limit: int = 50) -> dict:
    """Search cases. q matches the URL, external_id, notes and label titles; failing_in is a run id.
    Retired cases are left out unless status='retired'."""
    params = {k: v for k, v in dict(tag=tag, status=status, kind=kind, domain=domain, q=q, suite=suite,
                                    failing_in=failing_in).items() if v}
    data = call(store().get, 'cases/', limit=min(limit, 200), **params)
    return {'cases': data['results'], 'more': bool(data.get('next'))}


@mcp.tool()
def get_case(case_id: str, version: Optional[int] = None, include_capture_parse: bool = False,
             labels_from: int = 0, labels_limit: int = 100) -> dict:
    """A case with its labels (each with id and index). version gives it as it was then. A big listing's
    labels come labels_limit at a time from labels_from (labels_total says how many there are)."""
    params = {'include': 'parsed'} if include_capture_parse else {}
    if version is not None:
        params['version'] = version
    data = call(store().case, case_id, **params)
    out = _brief_case(data)
    out['labels_total'] = len(out['labels'])
    out['labels'] = out['labels'][labels_from:labels_from + max(1, labels_limit)]
    if include_capture_parse and data.get('parsed'):
        out['capture_parse'] = [_brief_event(e) for e in data['parsed'].get('events', [])]
    return out


@mcp.tool()
def update_case(case_id: str, version: int, changes: dict) -> dict:
    """Change case fields: context, kind, complete, not_events, known_failures, notes, tags (replaces the
    list), status ('draft' or 'retired'), external_id. Use the label tools for labels."""
    if 'labels' in changes:
        raise ToolError('Use set_labels for labels.')
    return _brief_case(call(store().patch, f'cases/{case_id}/', {**changes, 'version': version}))


@mcp.tool()
def clone_case(case_id: str, external_id: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    """A draft copy of a case on the same page, e.g. with another timezone or captured context."""
    return _brief_case(call(store().post, f'cases/{case_id}/clone/',
                            {'external_id': external_id, 'overrides': overrides or {}}))


@mcp.tool()
def mark_reviewed(case_id: str, version: int) -> dict:
    """Mark the case reviewed. Refused with every problem listed until labels and context check out."""
    return _brief_case(call(store().post, f'cases/{case_id}/review/', {'version': version}, idempotent=False))


@mcp.tool()
def case_history(case_id: str) -> dict:
    """Every version of a case: who changed it, when, and which fields."""
    return call(store().get, f'cases/{case_id}/history/')


@mcp.tool()
def accept_failures(case_id: str, version: int, paths: list[str], reason: str) -> dict:
    """Record mismatch paths (from preview or a run, e.g. 'events[2].end_datetime') as known failures of the
    case: scraper bugs it's expected to show until they're fixed, so runs report them apart from new ones."""
    data = call(store().case, case_id)
    known = {k['path']: k for k in data.get('known_failures') or []}
    for path in paths:
        known[path] = {'path': path, 'reason': reason}
    return _brief_case(call(store().patch_case, case_id, version, {'known_failures': list(known.values())}))


@mcp.tool()
def coverage() -> dict:
    """What the test data covers: cases by tag, status, kind, platform and source; how many labels assert each
    field (reviewed and all); the newest finished run by extraction strategy; and which workers are up."""
    stats = call(store().get, 'stats/')
    stats['workers'] = call(store().schema).get('workers', [])
    return stats


@mcp.tool()
def list_snapshots(status: Optional[str] = 'pending', domain: Optional[str] = None, limit: int = 50) -> dict:
    """Pages crawls saved (where the model ran, an event was held or dropped, or an events page came back empty).
    case_made says whether a case was already made from one; turn one into a case with case_from_snapshot."""
    params = {k: v for k, v in dict(status=status, domain=domain).items() if v}
    data = call(store().get, 'snapshots/', limit=min(limit, 200), **params)
    return {'snapshots': data['results'], 'more': bool(data.get('next'))}


@mcp.tool()
def case_from_snapshot(snapshot_id: str, external_id: Optional[str] = None, tags: Optional[list[str]] = None) -> dict:
    """A draft case from a crawl's saved page, its labels started from what the crawl parsed (marked incomplete
    until you check them against read_page_text)."""
    return _brief_case(call(store().post, f'cases/from-snapshot/{snapshot_id}/',
                            {'external_id': external_id, 'tags': tags or []}))


# ── Labels ────────────────────────────────────────────────────────────────────

def _labels(data: dict) -> dict:
    out = {'version': data['version'],
           'labels': [{'id': lb['id'], 'index': lb['index'], 'fields': lb['fields']} for lb in data['labels']]}
    if data.get('warnings'):
        out['warnings'] = data['warnings']
    return out


@mcp.tool()
def set_labels(case_id: str, version: int, labels: list[dict]) -> dict:
    """Replace all of a case's labels. Each is a dict of label fields."""
    return _labels(call(store().put, f'cases/{case_id}/labels/', {'version': version, 'labels': labels}))


@mcp.tool()
def add_label(case_id: str, version: int, fields: dict, index: Optional[int] = None, note: str = '') -> dict:
    """Add one label (at index, else last)."""
    body: dict[str, Any] = {'version': version, 'fields': fields, 'note': note}
    if index is not None:
        body['index'] = index
    return _labels(call(store().post, f'cases/{case_id}/labels/', body, idempotent=False))


@mcp.tool()
def edit_label(case_id: str, version: int, label_id: str, fields: Optional[dict] = None,
               unset: Optional[list[str]] = None, index: Optional[int] = None, note: Optional[str] = None) -> dict:
    """Change one label: fields merge key by key (a null value asserts the field is empty), unset removes
    keys (no longer checked), index moves it."""
    body: dict[str, Any] = {'version': version, 'fields': fields or {}, 'unset': unset or []}
    if index is not None:
        body['index'] = index
    if note is not None:
        body['note'] = note
    return _labels(call(store().patch, f'cases/{case_id}/labels/{label_id}/', body))


@mcp.tool()
def remove_label(case_id: str, version: int, label_id: str) -> dict:
    """Remove one label."""
    return _labels(call(store().delete, f'cases/{case_id}/labels/{label_id}/', version=version))


@mcp.tool()
def labels_from_parse(case_id: str, version: int, source: str = 'captured',
                      indexes: Optional[list[int]] = None, append: bool = False) -> dict:
    """Start labels from a parse's events: source 'captured' (the capture parse) or a run id. indexes picks
    events; append keeps the labels already there."""
    body = {'version': version, 'source': source, 'append': append}
    if indexes is not None:
        body['indexes'] = indexes
    return _labels(call(store().post, f'cases/{case_id}/labels/from-parse/', body, idempotent=False))


# ── Scoring ───────────────────────────────────────────────────────────────────

@mcp.tool()
def preview(case_id: str, ai: str = 'replay', record_answer: bool = False) -> dict:
    """Parse the case with the scraper code on disk and score it against its labels: what's missed or
    wrong (expected vs actual), extra events, and drops. ai: 'replay' (the recorded answer), 'live'
    (asks the model; record_answer saves its answer on the case), 'none'."""
    data = call(store().get, f'cases/{case_id}/', include='ai')
    case = case_from_api(data, call(store().html, data['page']['id']))
    mode = None if ai == 'none' else ai
    api_key = _api_key() if mode == 'live' else ''
    if mode == 'live' and not api_key:
        raise ToolError('ai="live" needs ANTHROPIC_API_KEY in .env.')
    result = run_case(case, ai=mode, api_key=api_key)
    c = compare(case.events, result.events, not_events=case.not_events, complete=case.complete,
                timezone_name=case.timezone)
    if record_answer and result.ai_response:
        call(store().post, f'cases/{case_id}/ai-responses/', {**result.ai_response, 'origin': 'live'})
    known = {k['path'] for k in case.known_failures}
    return {
        'version': data['version'], 'strategy': result.strategy, 'ai_used': result.ai_used,
        'ai_missing': result.ai_missing, 'ai_stale': result.ai_stale,
        'recall': c.recall, 'precision': c.precision, 'failures': c.failures,
        'new_failures': [p for p in c.failures if p not in known],
        'labels': [{'index': m.index, 'title': m.expected.get('title'), 'found': m.actual is not None,
                    'wrong': {k: {'expected': r.expected, 'actual': r.actual} for k, r in m.fields.items() if not r.ok}}
                   for m in c.matches],
        'extra': [_brief_event(e) for e in c.extras + c.rejected],
        'parsed': [_brief_event(e) for e in result.events],
        'dropped': [_brief_event(e) for e in result.dropped],
    }


@mcp.tool()
def run_suite(suite: Optional[str] = None, case_ids: Optional[list[str]] = None, ai: str = 'replay',
              name: str = '', baseline: Optional[str] = None, local: bool = False, wait: int = 60) -> dict:
    """Score the scraper over a suite or a list of case ids. local=True runs it here and returns the
    finished run (good for a few dozen cases); otherwise it's queued for the worker (tools/eval_worker.py)
    and this waits up to `wait` seconds. ai: 'replay', 'live' (costs credits), 'none'. baseline: a run id,
    or 'latest' for the suite's newest finished run."""
    from scraper.eval.runner import SchemaMismatch, check_schema, run_one
    body: dict[str, Any] = {'ai': ai, 'name': name, 'wait': 0 if local else max(0, min(wait, 60))}
    if suite:
        body['suite'] = suite
    elif case_ids:
        body['cases'] = case_ids
    else:
        raise ToolError('Send suite or case_ids.')
    if baseline:
        body['baseline'] = baseline
    run = call(store().queue_run, **body)
    if local:
        try:
            check_schema(store())
        except SchemaMismatch as exc:
            raise ToolError(str(exc))
        worker = f'mcp-{os.getpid()}'
        leased = call(store().lease, worker, run['id'])
        api_key = _api_key() if ai == 'live' else ''
        return call(run_one, store(), leased, worker, api_key)
    if run['status'] == 'queued' and not any(w['active'] for w in call(store().schema).get('workers', [])):
        run['note'] = ('No eval worker has asked for work in the last two minutes, so this run will wait. '
                       'Start one (python tools/eval_worker.py) or call run_suite with local=True.')
    return run


@mcp.tool()
def get_run(run_id: str, failed_only: bool = True, limit: int = 50) -> dict:
    """A run's status and scorecard, and its failing cases with their mismatch paths."""
    run = call(store().get, f'runs/{run_id}/')
    params = {'failed': 1} if failed_only else {}
    items = call(store().get, f'runs/{run_id}/cases/', **params)
    run['cases'] = [{k: i[k] for k in ('case_id', 'external_id', 'case_version', 'status', 'passed',
                                       'new_failures', 'error') if i.get(k) not in (None, '', [])}
                    for i in items[:limit]]
    run['more_cases'] = len(items) > limit
    return run


@mcp.tool()
def compare_runs(run_id: str, other_run_id: str) -> dict:
    """What got worse and better in one run than another: per case (mismatch paths) and overall."""
    return call(store().get, f'runs/{run_id}/compare/{other_run_id}/')


@mcp.tool()
def save_suite(name: str, description: Optional[str] = None, filter: Optional[dict] = None,
               add_case_ids: Optional[list[str]] = None, remove_case_ids: Optional[list[str]] = None,
               freeze: bool = False) -> dict:
    """Create or change a suite (name: a slug). filter: {tag, status, kind, platform, domain, source}, each a
    value or list; listed cases are added to what it matches. freeze pins its cases at their current versions."""
    body: dict[str, Any] = {k: v for k, v in dict(description=description, filter=filter,
                                                  add_case_ids=add_case_ids, remove_case_ids=remove_case_ids).items()
                            if v is not None}
    try:
        suite = store().patch(f'suites/{name}/', body) if body else store().get(f'suites/{name}/')
    except StoreError as exc:
        if exc.status != 404:
            raise ToolError(str(exc))
        suite = call(store().post, 'suites/', {**body, 'name': name})
    if freeze:
        suite = call(store().post, f'suites/{name}/freeze/', idempotent=False)
    return {k: suite[k] for k in ('name', 'description', 'filter', 'frozen_at', 'size')}


@mcp.tool()
def list_suites() -> list[dict]:
    """Every suite with its size."""
    return [{k: s[k] for k in ('name', 'description', 'filter', 'frozen_at', 'size')}
            for s in call(store().get, 'suites/')]


if __name__ == '__main__':
    mcp.run('stdio')
