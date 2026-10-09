"""
Which of a listing's events are worth following to their own page.

detail_reason(data)   → 'required' (the listing lacks a start or description),
                        'soft' (the page probably says more), or None
may_follow(reason, …) → within the run's detail budget? Soft follows leave a
                        reserve for required ones, and only sample a site
                        whose detail pages haven't helped
judge_useful(…)       → after enough soft follows, did they help? (the recipe learns `detail_useful`)
detail_url(data, url) → the event's own page on this site, or None
"""
from __future__ import annotations
from typing import Optional
from urllib.parse import urljoin

from scraper.discovery.events_page import same_site
from scraper.util import parse_iso

# A listing description shorter than this that doesn't end a sentence is probably a teaser.
TEASER_CHARS = 300
# After this many soft follows in a run, judge whether they help: at least this share must add something.
DETAIL_SAMPLE_MIN = 5
DETAIL_USEFUL_SHARE = 0.3


def detail_reason(data: dict) -> Optional[str]:
    if not (data.get('start_datetime') and data.get('description')):
        return 'required'
    if (looks_cut_off(data['description'])
            or not (data.get('location_address') or data.get('location_lat') is not None)
            or not data.get('end_datetime')
            or unexplained_span(data)):
        return 'soft'
    return None


def may_follow(reason: str, *, budget: int, reserve: int, detail_useful: Optional[bool],
               soft_scheduled: int) -> bool:
    if reason == 'required':
        return budget > 0
    if budget <= reserve:
        return False
    # Detail pages haven't helped this site: keep sampling a few so that can change.
    return detail_useful is not False or soft_scheduled < DETAIL_SAMPLE_MIN


def judge_useful(followed: int, useful: int) -> Optional[bool]:
    """None until enough soft follows to tell."""
    if followed < DETAIL_SAMPLE_MIN:
        return None
    return useful >= DETAIL_USEFUL_SHARE * followed


def detail_url(data: dict, page_url: str) -> Optional[str]:
    if not data.get('url'):
        return None
    url = urljoin(page_url, data['url'])
    if url.rstrip('/') == page_url.rstrip('/') or not same_site(url, page_url):
        return None
    return url


def looks_cut_off(description: str) -> bool:
    text = description.rstrip()
    if text.endswith(('…', '...')):
        return True
    return len(text) < TEASER_CHARS and not text.endswith(('.', '!', '?', '"', '”', ')'))


def unexplained_span(data: dict) -> bool:
    """Days between start and end, and nothing saying which of them it happens on."""
    if data.get('recurrence_freq') not in (None, 'none') or data.get('rdates'):
        return False
    start, end = parse_iso(data.get('start_datetime')), parse_iso(data.get('end_datetime'))
    return bool(start and end and (end.date() - start.date()).days > 1)
