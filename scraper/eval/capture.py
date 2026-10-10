"""
Turn a page into a case, and a reviewed case into a fixture. Used by
tools/capture.py, tools/review.py and tools/process_reports.py.
"""
from __future__ import annotations
import shutil
from pathlib import Path
from typing import Optional
from urllib import robotparser
from urllib.parse import urlparse

import requests

from scraper import settings as defaults
from scraper.eval.case import (
    FIXTURES_DIR, INBOX_DIR, LABEL_FIELDS, Case, now_iso, unique_id,
)
from scraper.eval.compare import compare
from scraper.eval.run import run_case
from scraper.items import RECURRENCE_DEFAULTS

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

_RECURRENCE = tuple(RECURRENCE_DEFAULTS)


class FetchError(Exception):
    pass


def fetch(url: str, *, render: bool = True, user_agent: str = USER_AGENT,
          obey_robots: bool = defaults.ROBOTSTXT_OBEY) -> str:
    """
    The page's HTML, honouring robots.txt like the crawler (ROBOTSTXT_OBEY). Rendered
    with Playwright by default, as the crawl renders every page; render=False fetches it raw.
    """
    if obey_robots:
        _check_robots(url, user_agent)
    if render:
        return _render(url, user_agent)
    try:
        resp = requests.get(url, headers={'User-Agent': user_agent}, timeout=20)
    except requests.RequestException as exc:
        raise FetchError(str(exc)) from exc
    if not resp.ok:
        raise FetchError(f'HTTP {resp.status_code}')
    return resp.text


def _check_robots(url: str, user_agent: str):
    origin = '{0.scheme}://{0.netloc}/'.format(urlparse(url))
    robots = robotparser.RobotFileParser(origin + 'robots.txt')
    try:
        robots.read()
        if not robots.can_fetch(user_agent, url):
            raise FetchError('robots.txt disallows this URL')
    except FetchError:
        raise
    except Exception:
        pass


def _render(url: str, user_agent: str) -> str:
    try:
        from playwright.sync_api import Error as PlaywrightError, sync_playwright
    except ImportError as exc:
        raise FetchError('Playwright is not installed: pip install playwright && '
                         'playwright install chromium') from exc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=user_agent)
            page.goto(url, wait_until='networkidle', timeout=30_000)
            return page.content()
        except PlaywrightError as exc:
            raise FetchError(str(exc).splitlines()[0]) from exc
        finally:
            browser.close()


def new_case(url: str, html: str, *, kind: str = 'listing', seed_context: Optional[dict] = None,
             partial: Optional[dict] = None, recipe: Optional[dict] = None,
             platform: Optional[str] = None, source: str = 'manual',
             source_ref: Optional[str] = None, notes: str = '', ai: Optional[str] = 'live',
             api_key: str = '', captured_at: Optional[str] = None,
             ai_response: Optional[dict] = None, case_id: Optional[str] = None) -> Case:
    """Parse the page and start its labels from what the scraper found."""
    case = Case(id='', url=url, html=html, kind=kind, captured_at=captured_at or now_iso(),
                source=source, source_ref=source_ref, seed_context=seed_context or {},
                partial=partial, recipe=recipe or {}, platform=platform, notes=notes,
                ai_response=ai_response)
    if ai == 'live' and not api_key:
        ai = None
    reparse(case, ai=ai, api_key=api_key)
    case.events = [labels_from(e) for e in case.parsed['events']]
    parts = urlparse(url)
    first = case.events[0]['title'] if case.events else f'{parts.netloc} {parts.path}'.replace('www.', '', 1)
    case.id = case_id or unique_id(first)
    return case


def reparse(case: Case, *, ai: Optional[str] = 'replay', api_key: str = '') -> None:
    """Re-run the scraper on the case and store its output (and a fresh AI answer if live)."""
    result = run_case(case, ai=ai, api_key=api_key)
    case.parsed = {**result.to_parsed(), 'ai_missing': result.ai_missing, 'ai_stale': result.ai_stale}
    if ai == 'live' and result.ai_response is not None:
        case.ai_response = result.ai_response


def labels_from(event: dict) -> dict:
    """A parsed event as a starting label: its non-empty fields, recurrence only if it recurs."""
    label = {}
    for key in LABEL_FIELDS:
        value = event.get(key)
        if value in (None, '', [], {}):
            continue
        if key in _RECURRENCE and event.get('recurrence_freq', 'none') == 'none':
            continue
        label[key] = value
    return label


def known_failures_for(case: Case) -> list[dict]:
    """Where the scraper is still wrong about this case, keeping any reasons already written."""
    result = run_case(case, ai='replay')
    comparison = compare(case.events, result.events, not_events=case.not_events,
                         complete=case.complete, timezone_name=case.timezone)
    reasons = {k['path']: k.get('reason', '') for k in case.known_failures}
    return [{'path': p, 'reason': reasons.get(p, '')} for p in comparison.failures]


def promote(case: Case, reviewer: Optional[str] = None) -> Path:
    """Save a reviewed case as a fixture and take it out of the inbox."""
    problems = case.validate()
    if problems:
        raise ValueError('; '.join(problems))
    case.reviewer = reviewer or case.reviewer
    case.reviewed_at = now_iso()
    case.known_failures = known_failures_for(case)
    target = FIXTURES_DIR / case.id
    case.save(target)
    inbox = INBOX_DIR / case.id
    if inbox.exists():
        shutil.rmtree(inbox)
    return target
