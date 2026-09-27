"""
Recurrence pattern extractor.

Two-pass approach:
  1. schema.org Schedule  — from JSON-LD eventSchedule (structured, free)
  2. Regex text matching  — on title + description text (heuristic, free)

Output is a dict whose keys match the backend's Event recurrence fields:
  recurrence_freq       'none' | 'daily' | 'weekly' | 'monthly' | 'yearly'
  recurrence_interval   int  (default 1)
  recurrence_byday      list of RRULE day codes: ["MO","TH"] or ["3TU"]
  recurrence_month_mode 'day' | 'weekday'
  recurrence_until      'YYYY-MM-DD' or None
  recurrence_count      int or None

Returns None when no recurrence pattern is detected.
"""

from __future__ import annotations
import re
from typing import Optional

# ── Day name → RRULE code ─────────────────────────────────────────────────────

_WEEKDAY_MAP: dict[str, str] = {
    'monday': 'MO', 'tuesday': 'TU', 'wednesday': 'WE',
    'thursday': 'TH', 'friday': 'FR', 'saturday': 'SA', 'sunday': 'SU',
    'mon': 'MO', 'tue': 'TU', 'wed': 'WE', 'thu': 'TH',
    'fri': 'FR', 'sat': 'SA', 'sun': 'SU',
    # Abbreviations sometimes seen in event copy
    'thur': 'TH', 'thurs': 'TH', 'weds': 'WE',
}

_ORDINAL_MAP: dict[str, str] = {
    'first': '1', '1st': '1',
    'second': '2', '2nd': '2',
    'third': '3', '3rd': '3',
    'fourth': '4', '4th': '4',
    'fifth': '5', '5th': '5',
    'last': '-1',
}

_WRITTEN_NUMBER: dict[str, int] = {
    'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8,
}

_ICAL_FREQ: dict[str, str] = {
    'P1D': 'daily',  'P7D': 'weekly',
    'P1W': 'weekly', 'P2W': 'weekly',   # handled as interval=2 below
    'P1M': 'monthly', 'P1Y': 'yearly',
}


# ── Public interface ──────────────────────────────────────────────────────────

def extract(title: str, description: str, jsonld_node: Optional[dict] = None) -> Optional[dict]:
    """
    Try schema.org first, then fall back to text heuristics.
    Returns a recurrence dict or None.
    """
    if jsonld_node:
        result = _from_jsonld(jsonld_node)
        if result:
            return result

    return _from_text(f"{title} {description}")


def signature(recurrence: dict) -> str:
    """
    Canonical string used as part of the recurring-event fingerprint.
    Stable regardless of which occurrence of the event is being scraped.
    """
    freq     = recurrence.get('recurrence_freq', 'none')
    interval = recurrence.get('recurrence_interval', 1)
    byday    = ':'.join(sorted(recurrence.get('recurrence_byday') or []))
    mode     = recurrence.get('recurrence_month_mode', 'day')
    return f"{freq}:{interval}:{byday}:{mode}"


# ── schema.org Schedule parser ────────────────────────────────────────────────

def _from_jsonld(node: dict) -> Optional[dict]:
    """
    Parse an eventSchedule node (schema.org/Schedule) from a JSON-LD Event.
    Handles both the `eventSchedule` property and an inline Schedule block.
    """
    schedule = node.get('eventSchedule') or node.get('Schedule')
    if not schedule:
        return None

    if isinstance(schedule, list):
        schedule = schedule[0] if schedule else None
    if not schedule or not isinstance(schedule, dict):
        return None

    # ── Frequency ─────────────────────────────────────────────────────────────
    raw_freq = schedule.get('repeatFrequency', '')
    freq = _ical_freq_to_str(raw_freq)
    if not freq:
        return None

    interval = 1
    # "P2W" → weekly with interval=2
    m = re.match(r'P(\d+)([DWMY])', raw_freq)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == 'W' and n > 1:
            interval = n
        elif unit == 'D' and n > 1 and n % 7 == 0:
            freq = 'weekly'
            interval = n // 7

    # ── byDay ─────────────────────────────────────────────────────────────────
    by_day_raw = schedule.get('byDay', [])
    if isinstance(by_day_raw, str):
        by_day_raw = [by_day_raw]
    byday = [_normalise_day(d) for d in by_day_raw]
    byday = [d for d in byday if d]

    # ── repeatCount / endDate ─────────────────────────────────────────────────
    count = schedule.get('repeatCount')
    until = schedule.get('endDate') or schedule.get('scheduleTimezone')

    return _build(freq, interval=interval, byday=byday, until=until, count=count)


def _ical_freq_to_str(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.upper()
    if raw in _ICAL_FREQ:
        return _ICAL_FREQ[raw]
    m = re.match(r'P(\d+)([DWMY])', raw)
    if m:
        unit = m.group(2)
        return {'D': 'daily', 'W': 'weekly', 'M': 'monthly', 'Y': 'yearly'}.get(unit)
    return None


def _normalise_day(raw: str) -> Optional[str]:
    """Map schema.org day name or RRULE code to a plain RRULE code."""
    if not raw:
        return None
    clean = raw.strip().lower().rstrip('s')   # "Thursdays" → "thursday"
    # schema.org uses "https://schema.org/Thursday" style URLs too
    clean = clean.split('/')[-1]
    return _WEEKDAY_MAP.get(clean, raw.upper()[:2] if len(raw) >= 2 else None)


# ── Text heuristics ───────────────────────────────────────────────────────────

def _from_text(text: str) -> Optional[dict]:
    text_lower = text.lower()

    # ── 1. Nightly / daily ────────────────────────────────────────────────────
    if re.search(r'\b(nightly|every\s+night|daily|every\s+day)\b', text_lower):
        return _build('daily')

    # ── 2. Every weekday ──────────────────────────────────────────────────────
    if re.search(r'\bevery\s+weekday\b', text_lower):
        return _build('weekly', byday=['MO', 'TU', 'WE', 'TH', 'FR'])

    # ── 3. Every weekend ──────────────────────────────────────────────────────
    if re.search(r'\bevery\s+weekend\b', text_lower):
        return _build('weekly', byday=['SA', 'SU'])

    # ── 4. Ordinal weekday of month: "first tuesday of the month" ─────────────
    m = re.search(
        r'\b(first|second|third|fourth|fifth|last|\d+(?:st|nd|rd|th))'
        r'\s+(' + '|'.join(_WEEKDAY_MAP.keys()) + r')'
        r'(?:\s+of\s+(?:each|every|the)\s+month|\s+monthly)?\b',
        text_lower,
    )
    if m:
        ordinal_raw = m.group(1).rstrip('stndrh')   # "3rd" → "3"
        ordinal = _ORDINAL_MAP.get(ordinal_raw, ordinal_raw)
        day_code = _WEEKDAY_MAP.get(m.group(2))
        if ordinal and day_code:
            return _build(
                'monthly',
                byday=[f"{ordinal}{day_code}"],
                month_mode='weekday',
            )

    # ── 5. "Every N weeks on DAY" ─────────────────────────────────────────────
    m = re.search(
        r'\bevery\s+(\d+|' + '|'.join(_WRITTEN_NUMBER) + r')\s+weeks?\s+'
        r'(?:on\s+)?(' + '|'.join(_WEEKDAY_MAP.keys()) + r')\b',
        text_lower,
    )
    if m:
        raw_n = m.group(1)
        interval = _WRITTEN_NUMBER.get(raw_n, int(raw_n) if raw_n.isdigit() else 2)
        day_code = _WEEKDAY_MAP.get(m.group(2))
        if day_code:
            return _build('weekly', interval=interval, byday=[day_code])

    # ── 6. "Every other DAY" ─────────────────────────────────────────────────
    m = re.search(
        r'\bevery\s+other\s+(' + '|'.join(_WEEKDAY_MAP.keys()) + r')\b',
        text_lower,
    )
    if m:
        day_code = _WEEKDAY_MAP.get(m.group(1))
        if day_code:
            return _build('weekly', interval=2, byday=[day_code])

    # ── 7. "Every DAY [and DAY]" ─────────────────────────────────────────────
    day_pattern = '|'.join(_WEEKDAY_MAP.keys())
    m = re.search(
        r'\bevery\s+(' + day_pattern + r')'
        r'(?:\s*(?:,|and|&)\s*(' + day_pattern + r'))*\b',
        text_lower,
    )
    if m:
        # Collect all days mentioned after "every"
        span_text = text_lower[m.start():]
        found_days: list[str] = []
        for word, code in _WEEKDAY_MAP.items():
            if re.search(r'\b' + word + r'\b', span_text[:60]):
                if code not in found_days:
                    found_days.append(code)
        if found_days:
            return _build('weekly', byday=found_days)

    # ── 8. Standalone "weekly" / "monthly" / "yearly" / "annually" ───────────
    if re.search(r'\bweekly\b', text_lower):
        return _build('weekly')
    if re.search(r'\bmonthly\b|\bevery\s+month\b', text_lower):
        return _build('monthly')
    if re.search(r'\b(?:yearly|annually|every\s+year)\b', text_lower):
        return _build('yearly')

    return None


# ── Builder ───────────────────────────────────────────────────────────────────

def _build(
    freq: str,
    interval: int = 1,
    byday: Optional[list[str]] = None,
    month_mode: str = 'day',
    until: Optional[str] = None,
    count: Optional[int] = None,
) -> dict:
    return {
        'recurrence_freq':       freq,
        'recurrence_interval':   interval,
        'recurrence_byday':      byday or [],
        'recurrence_month_mode': month_mode if freq == 'monthly' else 'day',
        'recurrence_until':      until,
        'recurrence_count':      int(count) if count is not None else None,
    }
