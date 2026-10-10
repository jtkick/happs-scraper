"""
Extract event fields from inline JavaScript data embedded in <script> tags.

Handles common patterns:
  <script id="__NEXT_DATA__">           Next.js pure-JSON page data
  <script type="application/json">      any pure-JSON block
  var data = {...}                       simpleview CMS, generic assignments
  window.__INITIAL_STATE__ = {...}      Redux SSR state dumps
  window.__PRELOADED_STATE__ = {...}
  window.__NUXT__ = {...}               Nuxt.js
  any  var X = {  or  window.X = {  in a script tag

The extractor recursively walks every parsed JSON structure for the sub-object
that best matches known event field names, then maps it to EventItem fields.
Runs after JSON-LD/OpenGraph as a free supplemental pass — no network calls.
"""
from __future__ import annotations
import json
import logging
import re
from typing import Any, Iterator, Optional

from scraper.util import strip_tags

logger = logging.getLogger(__name__)

# ── Field aliases ─────────────────────────────────────────────────────────────

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    'title':            ('title', 'name', 'eventName', 'event_name', 'eventTitle'),
    'description':      ('description', 'summary', 'details', 'eventDescription'),
    'start_datetime':   ('startDate', 'start_date', 'start_datetime', 'startTime',
                         'start_time', 'dateStart', 'date_start', 'begins', 'startAt'),
    'end_datetime':     ('endDate', 'end_date', 'end_datetime', 'endTime',
                         'end_time', 'dateEnd', 'date_end', 'ends', 'endAt'),
    'location_title':   ('location', 'venue', 'venueName', 'venue_name', 'place',
                         'locationName', 'location_name'),
    'location_address': ('address', 'streetAddress', 'street_address',
                         'fullAddress', 'full_address', 'formattedAddress'),
    'location_lat':     ('latitude', 'lat'),
    'location_lon':     ('longitude', 'lon', 'lng'),
    'ticket_price':     ('price', 'admission', 'ticketPrice', 'ticket_price',
                         'cost', 'fee'),
    'ticket_url':       ('ticketUrl', 'ticket_url', 'linkUrl', 'link_url',
                         'buyUrl', 'buy_url', 'registrationUrl'),
    'url':              ('eventUrl', 'event_url', 'permalink', 'canonical'),
    'image_url':        ('imageUrl', 'image_url', 'featuredImage', 'featured_image',
                         'coverImage', 'cover_image', 'heroImage', 'thumbnail'),
}

# Field names whose string values may contain multi-day schedule text.
# Captured as _schedule_text for the dates extractor — not an EventItem field.
_SCHEDULE_ALIASES: tuple[str, ...] = (
    'times', 'time', 'eventTime', 'event_time', 'eventTimes', 'event_times',
    'schedule', 'scheduleText', 'schedule_text',
    'hours', 'operatingHours', 'operating_hours',
    'when', 'occurrence', 'occurrences',
    'recurrence', 'recurrenceText', 'recurrence_text',
)

# Aliases that count toward the event-likelihood score.
# url/image excluded — too common in generic JS objects.
_SCORED_FIELDS = frozenset(_FIELD_ALIASES) - {'url', 'image_url'}
_SCORE_ALIASES: frozenset[str] = frozenset(
    alias
    for field, aliases in _FIELD_ALIASES.items()
    if field in _SCORED_FIELDS
    for alias in aliases
)

_MIN_SCORE = 3

# Keys under which some platforms nest an event's dates, e.g. Wix Events'
# scheduling.config.startDate. Searched up to _DATES_DEPTH levels down.
_DATE_CONTAINERS: tuple[str, ...] = ('scheduling', 'schedule', 'dates', 'timing', 'config')
_DATES_DEPTH = 2
_DATE_FIELDS = ('start_datetime', 'end_datetime')


# ── HTML stripping ────────────────────────────────────────────────────────────

# ── Script tag extraction ─────────────────────────────────────────────────────

_SCRIPT_RE = re.compile(r'<script([^>]*)>(.*?)</script>', re.DOTALL | re.IGNORECASE)
_ASSIGN_RE = re.compile(r'(?:var\s+\w+|window\.\w+)\s*=\s*\{')


def _iter_scripts(html: str) -> Iterator[tuple[str, str]]:
    for m in _SCRIPT_RE.finditer(html):
        yield m.group(1), m.group(2)


def _extract_balanced(text: str, start: int) -> Optional[str]:
    """Return the balanced {...} substring starting at text[start], or None."""
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if not in_str:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _json_from_script(attrs: str, content: str) -> Iterator[Any]:
    """Yield parsed JSON objects from a single script block."""
    stripped = content.strip()
    if not stripped:
        return

    # Pure-JSON scripts (Next.js, Nuxt, application/json blocks)
    if ('application/json' in attrs or '__NEXT_DATA__' in attrs
            or '__NUXT_DATA__' in attrs or 'nuxt-data' in attrs.lower()):
        try:
            yield json.loads(stripped)
            return
        except json.JSONDecodeError:
            pass

    # Content that starts with { — might be pure JSON
    if stripped.startswith('{'):
        try:
            yield json.loads(stripped)
            return
        except json.JSONDecodeError:
            pass

    # Assignment patterns: var X = {...} and window.X = {...}
    for m in _ASSIGN_RE.finditer(content):
        brace_pos = m.end() - 1  # match ends with the opening {
        raw = _extract_balanced(content, brace_pos)
        if raw and len(raw) > 20:
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                pass


# ── Event-object discovery ────────────────────────────────────────────────────

def _score(obj: dict) -> int:
    """Count distinct scored event fields present in obj."""
    matched: set[str] = set()
    for alias in _SCORE_ALIASES:
        if obj.get(alias) is not None:
            for field, aliases in _FIELD_ALIASES.items():
                if field in _SCORED_FIELDS and alias in aliases:
                    matched.add(field)
                    break
    return len(matched)


def _nested_dates(obj: dict, depth: int = _DATES_DEPTH) -> dict:
    """Start/end aliases found under a date container, when obj has no start of its own."""
    if depth == 0 or any(obj.get(a) is not None for a in _FIELD_ALIASES['start_datetime']):
        return {}
    for key in _DATE_CONTAINERS:
        holder = obj.get(key)
        if not isinstance(holder, dict):
            continue
        if any(holder.get(a) is not None for a in _FIELD_ALIASES['start_datetime']):
            return {a: holder[a] for f in _DATE_FIELDS for a in _FIELD_ALIASES[f] if holder.get(a) is not None}
        found = _nested_dates(holder, depth - 1)
        if found:
            return found
    return {}


def _find_candidates(data: Any, depth: int = 0) -> Iterator[tuple[int, dict]]:
    """Recursively yield (score, obj) for event-like dicts, with any nested dates lifted onto obj."""
    if depth > 12:
        return
    if isinstance(data, dict):
        dates = _nested_dates(data)
        obj = {**dates, **data} if dates else data
        s = _score(obj)
        if s >= _MIN_SCORE:
            yield s, obj
        for v in data.values():
            yield from _find_candidates(v, depth + 1)
    elif isinstance(data, list):
        for item in data[:200]:
            yield from _find_candidates(item, depth + 1)


# ── Field mapping ─────────────────────────────────────────────────────────────

def _coerce(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == 'description':
        if not isinstance(value, str):
            return None
        text = strip_tags(value) if '<' in value else value.strip()
        return text or None
    if field in ('location_lat', 'location_lon'):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field == 'ticket_price':
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            clean = re.sub(r'[^\d.]', '', value)
            try:
                return float(clean) if clean else None
            except ValueError:
                return None
        return None
    if field == 'location_title':
        if isinstance(value, dict):
            return value.get('name') or value.get('title')
        return str(value).strip() or None
    if field == 'location_address':
        if isinstance(value, dict):
            parts = filter(None, [
                value.get('streetAddress') or value.get('street'),
                value.get('addressLocality') or value.get('city'),
                value.get('addressRegion') or value.get('state'),
                value.get('postalCode') or value.get('zip'),
            ])
            return ', '.join(parts) or None
        return str(value).strip() or None
    if field == 'image_url':
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, dict):
            value = value.get('url') or value.get('src') or value.get('@id')
        return str(value).strip() if value else None
    if isinstance(value, str):
        return value.strip() or None
    return str(value) if value is not None else None


def _map_fields(obj: dict) -> dict:
    result: dict = {}
    for target_field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if obj.get(alias) is not None:
                coerced = _coerce(target_field, obj[alias])
                if coerced is not None:
                    result[target_field] = coerced
                    break
    # Collect schedule-hint strings for the dates extractor (not an EventItem field).
    schedule_parts: list[str] = []
    for alias in _SCHEDULE_ALIASES:
        val = obj.get(alias)
        if isinstance(val, str) and val.strip():
            schedule_parts.append(val.strip())
    if schedule_parts:
        result['_schedule_text'] = ' '.join(schedule_parts)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def extract(html: str, base_url: str) -> Optional[dict]:
    """
    Scan all <script> tags for inline JSON data and return extracted event fields.
    Returns None if no viable event object is found.
    """
    best_score = 0
    best_obj: Optional[dict] = None

    for attrs, content in _iter_scripts(html):
        for parsed in _json_from_script(attrs, content):
            for score, obj in _find_candidates(parsed):
                if score > best_score:
                    best_score = score
                    best_obj = obj

    if best_obj is None:
        return None

    result = _map_fields(best_obj)
    logger.debug("inline_json: score=%d fields=%s from %s", best_score, list(result.keys()), base_url)
    return result or None


def extract_all(html: str, base_url: str) -> list[dict]:
    """
    Return every inline event object that has at least a title and a start
    date — the shape of a listing page's embedded data (calendar widgets,
    Next.js page props). Venue/organizer objects are excluded by that rule.
    """
    results: list[dict] = []
    seen: set[tuple] = set()
    for attrs, content in _iter_scripts(html):
        for parsed in _json_from_script(attrs, content):
            for _, obj in _find_candidates(parsed):
                mapped = _map_fields(obj)
                key = (mapped.get('title'), mapped.get('start_datetime'))
                if not all(key) or key in seen:
                    continue
                seen.add(key)
                results.append(mapped)
    logger.debug("inline_json: %d complete events from %s", len(results), base_url)
    return results
