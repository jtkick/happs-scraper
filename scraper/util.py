"""
Small helpers shared across the scraper, with no dependencies on it.

Times
  parse_iso(value)  → datetime | None   an ISO date/datetime as written: a naive one stays naive (event times)
  parse_utc(value)  → datetime | None   the same, a naive one read as UTC (crawl timestamps, comparisons with now)
  now_iso()         → str               the current UTC time, ISO
Text
  strip_tags(value) → str | None        an HTML fragment (a feed's description) as plain text
  fold(text)        → str               whitespace collapsed and case folded, for comparing text
"""
from __future__ import annotations
import html
import re
from datetime import datetime, timezone
from typing import Optional

_TAG = re.compile(r'<[^>]+>')
_SPACE = re.compile(r'\s+')


def parse_iso(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def parse_utc(value) -> Optional[datetime]:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_tags(value) -> Optional[str]:
    if not value:
        return None
    return _SPACE.sub(' ', html.unescape(_TAG.sub(' ', str(value)))).strip() or None


def fold(text) -> str:
    return _SPACE.sub(' ', str(text or '')).strip().casefold()
