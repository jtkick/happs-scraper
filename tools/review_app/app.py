"""
Review app: see what the scraper parsed from a page next to the page itself,
correct it, and save the case as a fixture. Started by tools/review.py.

Cases live in two places ('where' in every URL):
  inbox     review/inbox/<id>/   waiting for review (gitignored)
  fixtures  tests/fixtures/<id>/ reviewed: golden tests and training data
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from scraper.eval.capture import FetchError, fetch, known_failures_for, labels_from, new_case, promote
from scraper.eval.case import FIXTURES_DIR, INBOX_DIR, LABEL_FIELDS, Case, load_all
from scraper.eval.compare import compare
from scraper.eval.run import run_case
from scraper.extractors import ai as ai_extractor
from scraper.extractors import inline_json, jsonld, opengraph

HERE = Path(__file__).parent
ROOTS = {'inbox': INBOX_DIR, 'fixtures': FIXTURES_DIR}

app = FastAPI(title='Scraper review')
app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
templates = Jinja2Templates(directory=HERE / 'templates')


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
def inbox(request: Request, where: str = 'inbox'):
    _root(where)
    rows = [_summary(c, where) for c in load_all(ROOTS[where])]
    counts = {w: _count(r) for w, r in ROOTS.items()}
    return templates.TemplateResponse(request, 'inbox.html', {
        'rows': rows, 'where': where, 'counts': counts})


@app.get('/new', response_class=HTMLResponse)
def new_form(request: Request):
    return templates.TemplateResponse(request, 'new.html', {
        'error': None, 'form': {'render': 'on'}, 'has_ai': bool(_api_key())})


@app.post('/new')
async def new_submit(request: Request):
    form = dict(await request.form())
    url = form.get('url', '').strip()
    try:
        # Off the event loop: Playwright's sync API refuses to run inside one.
        html = form.get('html', '').strip() or await run_in_threadpool(
            fetch, url, render=bool(form.get('render')))
    except FetchError as exc:
        return templates.TemplateResponse(request, 'new.html', {
            'error': f'Fetch failed: {exc}', 'form': form, 'has_ai': bool(_api_key())}, status_code=400)
    context = {}
    for key in ('location_title', 'location_address', 'timezone'):
        if form.get(key, '').strip():
            context[key] = form[key].strip()
    for key in ('location_lat', 'location_lon'):
        if form.get(key, '').strip():
            context[key] = float(form[key])
    case = await run_in_threadpool(
        new_case, url, html, kind=form.get('kind', 'listing'), seed_context=context,
        notes=form.get('notes', ''), ai='live' if form.get('ai') else None, api_key=_api_key())
    case.save(INBOX_DIR / case.id)
    return RedirectResponse(f'/case/inbox/{case.id}', status_code=303)


@app.get('/case/{where}/{case_id}', response_class=HTMLResponse)
def case_page(request: Request, where: str, case_id: str):
    case = _load(where, case_id)
    return templates.TemplateResponse(request, 'case.html', {
        'case': case, 'where': where, 'fields': LABEL_FIELDS, 'has_ai': bool(_api_key())})


@app.get('/case/{where}/{case_id}/page')
def case_html(where: str, case_id: str):
    """The saved page for the preview iframe: scripts can't run (sandbox), links resolve to the site."""
    case = _load(where, case_id)
    html = re.sub(r'<script\b.*?</script\s*>', '', case.html, flags=re.S | re.I)
    base = f'<base href="{case.url}" target="_blank">'
    html, n = re.subn(r'<head\b[^>]*>', lambda m: m.group(0) + base, html, count=1, flags=re.I)
    if not n:
        html = base + html
    return Response(html, media_type='text/html', headers={
        'Content-Security-Policy': "sandbox; script-src 'none'"})


# ── API ───────────────────────────────────────────────────────────────────────

@app.get('/api/case/{where}/{case_id}')
def get_case(where: str, case_id: str):
    case = _load(where, case_id)
    return _state(case, run_case(case, ai='replay'))


@app.get('/api/case/{where}/{case_id}/sources')
def get_sources(where: str, case_id: str):
    """What the page offers before any judgement: the AI's text and the structured data found."""
    case = _load(where, case_id)
    return {
        'text': ai_extractor.page_text(case.html) or '',
        'jsonld': [node for _, node in jsonld.extract_all_with_nodes(case.html, case.url)],
        'inline_json': inline_json.extract_all(case.html, case.url),
        'opengraph': opengraph.extract(case.html, case.url),
    }


@app.put('/api/case/{where}/{case_id}')
async def save_case(where: str, case_id: str, request: Request):
    case = _load(where, case_id)
    _apply(case, await request.json())
    case.known_failures = known_failures_for(case)
    case.save(ROOTS[where] / case.id)
    return _state(case, run_case(case, ai='replay'))


@app.post('/api/case/{where}/{case_id}/rerun')
async def rerun(where: str, case_id: str, request: Request):
    """Parse again with the code as it is now; live=true also asks the model afresh."""
    case = _load(where, case_id)
    body = await request.json()
    _apply(case, body.get('labels') or {})
    live = bool(body.get('live')) and bool(_api_key())
    result = run_case(case, ai='live' if live else 'replay', api_key=_api_key())
    if live and result.ai_response is not None:
        # Keep the new answer (replay needs it) without saving unsaved label edits.
        on_disk = _load(where, case_id)
        on_disk.ai_response = case.ai_response = result.ai_response
        on_disk.save(ROOTS[where] / case.id)
    return _state(case, result)


@app.post('/api/case/{where}/{case_id}/promote')
async def promote_case(where: str, case_id: str, request: Request):
    if where != 'inbox':
        raise HTTPException(400, 'only inbox cases are promoted')
    case = _load(where, case_id)
    _apply(case, await request.json())
    if (FIXTURES_DIR / case.id).exists():
        raise HTTPException(409, f'tests/fixtures/{case.id} already exists')
    try:
        promote(case, reviewer=_reviewer())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {'url': f'/case/fixtures/{case.id}'}


@app.post('/api/case/{where}/{case_id}/discard')
def discard_case(where: str, case_id: str):
    if where != 'inbox':
        raise HTTPException(400, 'delete fixtures with git, not from here')
    _load(where, case_id)
    shutil.rmtree(INBOX_DIR / case_id)
    return {'url': '/'}


@app.post('/sync')
def sync():
    from tools.review_app.sync import pull
    try:
        added = pull()
    except RuntimeError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    return {'added': added}


# ── Helpers ───────────────────────────────────────────────────────────────────

_EDITABLE = ('events', 'not_events', 'complete', 'notes', 'kind', 'seed_context', 'partial',
             'known_failures')


def _apply(case: Case, body: dict) -> None:
    for key in _EDITABLE:
        if key in body:
            setattr(case, key, body[key])
    if 'known_failures' in body:
        case.known_failures = [k for k in body['known_failures'] if k.get('path')]


def _state(case: Case, result) -> dict:
    comparison = compare(case.events, result.events, not_events=case.not_events,
                         complete=case.complete, timezone_name=case.timezone)
    index = {id(e): i for i, e in enumerate(result.events)}
    known = {k['path'] for k in case.known_failures}
    return {
        'case': {
            'id': case.id, 'url': case.url, 'kind': case.kind, 'captured_at': case.captured_at,
            'source': case.source, 'source_ref': case.source_ref, 'seed_context': case.seed_context,
            'partial': case.partial, 'events': case.events, 'not_events': case.not_events,
            'complete': case.complete, 'notes': case.notes, 'known_failures': case.known_failures,
            'reviewer': case.reviewer, 'reviewed_at': case.reviewed_at,
            'has_ai_response': case.ai_response is not None,
            'parsed_at_capture': case.parsed,
        },
        'current': {
            **result.to_parsed(), 'ai_missing': result.ai_missing, 'ai_stale': result.ai_stale,
            # Each parsed event as a starting label, for "add as event".
            'labels': [labels_from(e) for e in result.events],
            'dropped_labels': [labels_from(e) for e in result.dropped],
        },
        'comparison': {
            'matches': [{
                'actual': index.get(id(m.actual)) if m.actual is not None else None,
                'fields': {k: r.ok for k, r in m.fields.items()},
            } for m in comparison.matches],
            'extras': [index[id(e)] for e in comparison.extras],
            'rejected': [index[id(e)] for e in comparison.rejected],
            'recall': comparison.recall,
            'precision': comparison.precision,
            'failures': comparison.failures,
            'new_failures': [p for p in comparison.failures if p not in known],
            'fixed': sorted(known - set(comparison.failures)),
        },
    }


def _summary(case: Case, where: str) -> dict:
    result = run_case(case, ai='replay')
    comparison = compare(case.events, result.events, not_events=case.not_events,
                         complete=case.complete, timezone_name=case.timezone)
    methods = sorted({e.get('extraction_method', '?') for e in result.events})
    return {
        'id': case.id, 'where': where, 'domain': urlparse(case.url).netloc, 'url': case.url,
        'kind': case.kind, 'source': case.source, 'captured_at': case.captured_at[:10],
        'strategy': result.strategy, 'methods': ', '.join(methods), 'ai': result.ai_used,
        'parsed': len(result.events), 'dropped': len(result.dropped), 'labelled': len(case.events),
        'failures': len(comparison.failures), 'recall': comparison.recall,
        'precision': comparison.precision,
        'confidence': min((e.get('confidence', 1) for e in result.events), default=None),
    }


def _count(root: Path) -> int:
    return sum(1 for d in root.iterdir() if d.is_dir()) if root.exists() else 0


def _root(where: str) -> Path:
    if where not in ROOTS:
        raise HTTPException(404)
    return ROOTS[where]


def _load(where: str, case_id: str) -> Case:
    path = _root(where) / case_id
    if '/' in case_id or not (path / 'case.json').exists():
        raise HTTPException(404, f'no case {where}/{case_id}')
    return Case.load(path)


def _api_key() -> str:
    return os.getenv('ANTHROPIC_API_KEY', '')


def _reviewer() -> Optional[str]:
    try:
        return subprocess.run(['git', 'config', 'user.name'], capture_output=True, text=True,
                              check=False).stdout.strip() or None
    except OSError:
        return None
