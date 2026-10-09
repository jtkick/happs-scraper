"""
Reviewed cases as training examples for the AI extractor: the exact prompt
the crawler sends for the page, and the answer it should have got.

Labels are stored as the pipeline normalizes them (UTC), but the prompt asks
the model for local time as the page prints it, so datetimes are converted
back to the venue's zone. Every event needs `evidence` (a verbatim span with
its date) because the crawler drops answers whose evidence isn't on the page;
when a label has none, the sentence naming the event is used.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from scraper.eval.case import Case
from scraper.extractors import ai


def training_record(case: Case) -> tuple[Optional[dict], str]:
    """(record, '') or (None, why the case can't be used)."""
    if not case.complete:
        return None, 'labels cover only some of the page'
    text = ai.page_text(case.html) or ''
    if len(text) < ai.MIN_TEXT_CHARS:
        return None, 'too little page text for the model'
    venue = (case.seed_context or {}).get('location_title')
    user = ai.build_user_message(text, url=case.url, venue=venue, today=case.captured.date())

    events = []
    for i, label in enumerate(case.events):
        if not label.get('start_datetime'):
            return None, f'events[{i}] has no start_datetime'
        evidence = _evidence(label, text)
        if evidence is None:
            return None, f'events[{i}] has no evidence on the page (set one in the review app)'
        event = {k: label.get(k) for k in ai.OUTPUT_KEYS}
        for key in ('start_datetime', 'end_datetime'):
            event[key] = _local(event[key], case.timezone)
        event['evidence'] = evidence
        events.append(event)

    return {
        'id': case.id,
        'system': ai._SYSTEM,
        'messages': [{'role': 'user', 'content': user},
                     {'role': 'assistant', 'content': json.dumps({'events': events}, ensure_ascii=False)}],
    }, ''


def correction_record(case: Case) -> Optional[dict]:
    """What the scraper produced when the page was captured, next to what it should have."""
    if not case.parsed:
        return None
    return {'id': case.id, 'url': case.url, 'parsed': case.parsed.get('events', []),
            'dropped': case.parsed.get('dropped', []), 'labels': case.events,
            'not_events': case.not_events}


def split_of(case: Case, eval_share: float) -> str:
    """'train' or 'eval', fixed per site so one site's pages never land on both sides."""
    domain = urlparse(case.url).netloc.removeprefix('www.')
    bucket = int(hashlib.sha256(domain.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return 'eval' if bucket < eval_share else 'train'


def _evidence(label: dict, text: str) -> Optional[str]:
    haystack = _norm(text)
    if label.get('evidence') and _norm(label['evidence']) in haystack:
        return label['evidence']
    title = _norm(label.get('title', ''))
    if not title:
        return None
    for line in text.splitlines():
        for sentence in re.split(r'(?<=[.!?])\s+', line):
            if title in _norm(sentence):
                return sentence.strip()
    return None


def _local(value, zone: Optional[str]):
    """'2026-10-01T23:00:00+00:00' → '2026-10-01T19:00' in America/New_York; anything else as is."""
    if not value or not zone or 'T' not in str(value):
        return value
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return value
    if dt.tzinfo is None:
        return value
    local = dt.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    # The pipeline reads a bare date as local midnight; the prompt wants the bare date back.
    if (local.hour, local.minute) == (0, 0):
        return local.date().isoformat()
    return local.isoformat(timespec='minutes')


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text)).strip().lower()
