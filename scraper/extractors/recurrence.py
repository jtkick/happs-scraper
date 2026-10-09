"""
Recurrence pattern extractor.

Sources, most trustworthy first:
  1. schema.org Schedule  — from JSON-LD eventSchedule (structured, free)
  2. Schedule text        — a page's own "recurrence"/"times" data (inline JSON)
  3. Title + description  — regex heuristics
  4. Page text            — strong phrases only, starting where the title appears
"until <date>" and "for N weeks" after a match fill until / count.

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

from scraper.extractors import dates as dates_extractor

# ── Day name → RRULE code ─────────────────────────────────────────────────────

_WEEKDAY_MAP: dict[str, str] = {
    'monday': 'MO', 'tuesday': 'TU', 'wednesday': 'WE',
    'thursday': 'TH', 'friday': 'FR', 'saturday': 'SA', 'sunday': 'SU',
    'mon': 'MO', 'tue': 'TU', 'wed': 'WE', 'thu': 'TH',
    'fri': 'FR', 'sat': 'SA', 'sun': 'SU',
    # Abbreviations sometimes seen in event copy
    'thur': 'TH', 'thurs': 'TH', 'weds': 'WE', 'tues': 'TU',
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

# How far past the title page text is searched; further on are sidebars and other events.
PAGE_WINDOW = 3000


def extract(title: str, description: str, jsonld_node: Optional[dict] = None,
            schedule_text: Optional[str] = None, page_text: Optional[str] = None) -> Optional[dict]:
    """
    Try schema.org first, then fall back to text heuristics.
    Returns a recurrence dict or None.
    """
    if jsonld_node:
        result = _from_jsonld(jsonld_node)
        if result:
            return result

    for text in (schedule_text, f"{title} {description}"):
        result = _from_text(text) if text and text.strip() else None
        if result:
            return result

    if page_text:
        return _from_text(_near_title(page_text, title), strong_only=True)
    return None


def _near_title(text: str, title: str, occurrences: int = 3) -> str:
    """The text after each of the title's first few appearances (page heading, embedded data)."""
    lower, needle = text.lower(), title.strip().lower()
    starts = [m.start() for m in re.finditer(re.escape(needle), lower)][:occurrences] if needle else []
    return '\n'.join(text[at:at + PAGE_WINDOW] for at in starts or [0])


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
    until = str(schedule.get('endDate') or '')[:10] or None

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
#
# Strong patterns name a schedule outright ("Recurring weekly on Monday",
# "every Thursday"); weak ones are a bare frequency word ("a weekly showcase").
# Whole-page text gets strong patterns only: a bare "daily" there is as likely
# to be the bar's daily specials as the event.

_DAY_WORDS = sorted(_WEEKDAY_MAP, key=len, reverse=True)
_DAY = r'\b(?:' + '|'.join(_DAY_WORDS) + r')s?\b\.?'
_DAYS = _DAY + r'(?:\s*(?:,|&|/|\band\b)\s*(?:and\s+)?' + _DAY + r')*'
_DAY_TOKEN = re.compile(r'\b(' + '|'.join(_DAY_WORDS) + r')s?\b', re.IGNORECASE)

_LEAD = r'(?:(?:recurring|recurs|repeats|repeating|occurs|held|happens)\s+)'
_NUMBER = r'(\d+|' + '|'.join(_WRITTEN_NUMBER) + r')'

_UNTIL = re.compile(r'\b(?:until|through|thru|till|til|ending(?:\s+on)?|ends(?:\s+on)?)\s+', re.IGNORECASE)
_NEW_SENTENCE = re.compile(r'[.!?]\s+[A-Z]|\n')
_COUNT = re.compile(r'\bfor\s+' + _NUMBER + r'\s+(day|night|week|month|year)s?\b', re.IGNORECASE)
_COUNT_UNIT = {'daily': ('day', 'night'), 'weekly': ('week',), 'monthly': ('month',), 'yearly': ('year',)}

_ORDINAL = r'(first|second|third|fourth|fifth|last|\d+(?:st|nd|rd|th))'
_MONTH_SUFFIX = r'(\s+(?:of\s+(?:each|every|the)\s+month|(?:of\s+)?(?:each|every)\s+month|monthly))'


def _days(span: str) -> list[str]:
    codes: list[str] = []
    for word in _DAY_TOKEN.findall(span):
        code = _WEEKDAY_MAP.get(word.lower())
        if code and code not in codes:
            codes.append(code)
    return codes


def _number(raw: str) -> int:
    return _WRITTEN_NUMBER.get(raw.lower()) or (int(raw) if raw.isdigit() else 2)


# (pattern, strong, build(match) -> (freq, kwargs)); first match wins.
_PATTERNS: list[tuple[re.Pattern, object, callable]] = [
    (re.compile(r'\bevery\s+weekday\b'), True,
     lambda m: ('weekly', {'byday': ['MO', 'TU', 'WE', 'TH', 'FR']})),
    (re.compile(r'\bevery\s+weekend\b'), True,
     lambda m: ('weekly', {'byday': ['SA', 'SU']})),
    (re.compile(r'\b' + _ORDINAL + r'\s+(' + '|'.join(_WEEKDAY_MAP) + r')' + _MONTH_SUFFIX + r'?\b'),
     lambda m: bool(m.group(3)),
     lambda m: ('monthly', {'byday': [f"{_ORDINAL_MAP[m.group(1)]}{_WEEKDAY_MAP[m.group(2)]}"],
                            'month_mode': 'weekday'})
     if m.group(1) in _ORDINAL_MAP else None),
    (re.compile(r'\bevery\s+' + _NUMBER + r'\s+weeks?\s+(?:on\s+)?(' + _DAYS + r')'), True,
     lambda m: ('weekly', {'interval': _number(m.group(1)), 'byday': _days(m.group(2))})),
    (re.compile(r'\bevery\s+other\s+(' + _DAYS + r')'), True,
     lambda m: ('weekly', {'interval': 2, 'byday': _days(m.group(1))})),
    (re.compile(r'\b' + _LEAD + r'?(?:weekly|every\s+week)\s+on\s+(' + _DAYS + r')'), True,
     lambda m: ('weekly', {'byday': _days(m.group(1))})),
    (re.compile(r'\bevery\s+(' + _DAYS + r')'), True,
     lambda m: ('weekly', {'byday': _days(m.group(1))})),
    (re.compile(r'\b(' + _LEAD + r')?(?:nightly|daily|every\s+(day|night))\b'),
     lambda m: bool(m.group(1) or m.group(2)),
     lambda m: ('daily', {})),
    (re.compile(r'\bevery\s+other\s+(week|month|year)\b'), lambda m: m.group(1) == 'week',
     lambda m: ({'week': 'weekly', 'month': 'monthly', 'year': 'yearly'}[m.group(1)], {'interval': 2})),
    (re.compile(r'\bevery\s+' + _NUMBER + r'\s+(week|month|year)s\b'), True,
     lambda m: ({'week': 'weekly', 'month': 'monthly', 'year': 'yearly'}[m.group(2)],
                {'interval': _number(m.group(1))})),
    (re.compile(r'\b(' + _LEAD + r')?weekly\b'), lambda m: bool(m.group(1)),
     lambda m: ('weekly', {})),
    (re.compile(r'\b(' + _LEAD + r')?(?:monthly|every\s+month)\b'), lambda m: bool(m.group(1)),
     lambda m: ('monthly', {})),
    (re.compile(r'\b(' + _LEAD + r')?(?:yearly|annually|every\s+year)\b'), lambda m: bool(m.group(1)),
     lambda m: ('yearly', {})),
]


def _from_text(text: str, strong_only: bool = False) -> Optional[dict]:
    text_lower = text.lower()
    for pattern, strong, build in _PATTERNS:
        for m in pattern.finditer(text_lower):
            is_strong = strong(m) if callable(strong) else strong
            if strong_only and not is_strong:
                continue
            built = build(m)
            if not built:
                continue
            freq, kwargs = built
            tail = text[m.end():m.end() + 100]
            return _build(freq, until=_until(tail), count=_count(tail, freq), **kwargs)
    return None


def _until(tail: str) -> Optional[str]:
    """The first "until <date>" in the rest of the sentence ("until 9pm" isn't a date)."""
    for m in _UNTIL.finditer(tail):
        if _NEW_SENTENCE.search(tail[:m.start()]):
            return None
        found = dates_extractor.parse_date(tail[m.end():])
        if found:
            return found.isoformat()
    return None


def _count(tail: str, freq: str) -> Optional[int]:
    m = _COUNT.search(tail)
    if m and m.group(2).lower() in _COUNT_UNIT.get(freq, ()):
        return _number(m.group(1))
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
