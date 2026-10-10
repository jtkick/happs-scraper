"""
Parse explicit date/time occurrences from event-page text.

Supplements structured-data extractors for patterns they cannot represent:
  Multi-day events   Friday, June 5: 6–10 pm; Saturday, June 6: noon–10 pm
  Date ranges        June 5–7, 2026
  Date lists         June 5, June 12, June 19 at 7 pm
  Exceptions         every Friday, except July 4

Returns any subset of:
  start_datetime  ISO-8601 string  (only if no date was found upstream)
  end_datetime    ISO-8601 string
  rdates          list[str]   — additional explicit occurrence starts
  exdates         list[dict]  — [{"datetime": str, "reason": str}]
"""
from __future__ import annotations
import re
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# ── Month tables ───────────────────────────────────────────────────────────────

_MONTH: dict[str, int] = {
    'january': 1,  'february': 2,  'march': 3,    'april': 4,
    'may': 5,      'june': 6,      'july': 7,      'august': 8,
    'september': 9,'october': 10,  'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6, 'jul': 7,
    'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

# Longest-first so the regex alternation doesn't short-circuit on "jun" before "june"
_MON_PAT = '|'.join(sorted(_MONTH, key=len, reverse=True))

_WD_PAT = (
    r'mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|'
    r'thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?'
)

# ── Time parsing ───────────────────────────────────────────────────────────────

# "10 a.m. to 6 p.m."  — individual am/pm on each side
_T_BOTH = re.compile(
    r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)'
    r'\s*(?:to|–|—|-)\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)',
    re.IGNORECASE,
)

# "6 to 10 p.m."  — shared trailing am/pm
_T_SHARED = re.compile(
    r'(\d{1,2})(?::(\d{2}))?'
    r'\s*(?:to|–|—|-)\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)',
    re.IGNORECASE,
)


def _to24(hour: int, period: str) -> int:
    p = re.sub(r'\.', '', period).lower()
    if p == 'pm' and hour != 12:
        return hour + 12
    if p == 'am' and hour == 12:
        return 0
    return hour


def _parse_time_range(text: str) -> Optional[tuple[int, int, int, int]]:
    """
    Return (start_h, start_m, end_h, end_m) in 24-h, or None.

    Handles:
      "6 to 10 p.m."       → (18, 0, 22, 0)
      "noon to 10 p.m."    → (12, 0, 22, 0)
      "10 a.m. to 6 p.m."  → (10, 0, 18, 0)
      "12 to 10 p.m."      → (12, 0, 22, 0)
    """
    text = re.sub(r'\bnoon\b', '12:00 p.m.', text, flags=re.IGNORECASE)
    text = re.sub(r'\bmidnight\b', '12:00 a.m.', text, flags=re.IGNORECASE)

    m = _T_BOTH.search(text)
    if m:
        sh, sm, sap, eh, em, eap = m.groups()
        return _to24(int(sh), sap), int(sm or 0), _to24(int(eh), eap), int(em or 0)

    m = _T_SHARED.search(text)
    if m:
        sh, sm, eh, em, eap = m.groups()
        sh, sm = int(sh), int(sm or 0)
        eh, em = int(eh), int(em or 0)
        eh24 = _to24(eh, eap)
        sh24 = _to24(sh, eap)
        # "10 to 2 p.m." → start was 10am, not 10pm
        if sh24 > eh24 and re.sub(r'\.', '', eap).lower() == 'pm':
            sh24 = sh  # keep as-is (already < 12, no +12)
        return sh24, sm, eh24, em

    return None


# ── Date helpers ───────────────────────────────────────────────────────────────

def _extract_year(text: str) -> Optional[int]:
    """Return the first plausible event year found in text."""
    m = re.search(r'\b(20[2-9]\d)\b', text)
    return int(m.group(1)) if m else None


def _best_year(month: int, day: int, hint: Optional[int]) -> int:
    """
    Pick the most likely year for a (month, day) with no explicit year.
    Uses hint if provided; otherwise returns the nearest future occurrence.
    """
    if hint:
        return hint
    today = date.today()
    for year in (today.year, today.year + 1):
        try:
            if date(year, month, day) >= today:
                return year
        except ValueError:
            continue
    return today.year


def _dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> Optional[str]:
    try:
        return datetime(year, month, day, hour, minute).strftime('%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return None


# ── Pattern 1: Multi-day festival segments separated by semicolons ─────────────
#
# Matches one segment like: "Friday, June 5: 6 to 10 p.m."
#                       or: "June 5: noon to 9 pm"

_SEG_RE = re.compile(
    r'(?:(?:' + _WD_PAT + r'),?\s+)?'       # optional weekday
    r'(' + _MON_PAT + r')\s+'               # month name
    r'(\d{1,2})(?:st|nd|rd|th)?'            # day
    r'(?:,?\s*(20\d{2}))?'                  # optional year
    r'\s*[,:]\s*'                            # colon or comma separator
    r'(.+)',                                 # rest (time range)
    re.IGNORECASE,
)


def _parse_multi_day(text: str, year_hint: Optional[int]) -> list[tuple[str, Optional[str]]]:
    """
    Parse semicolon-delimited date:time segments.
    Returns list of (start_iso, end_iso_or_None) tuples.
    """
    # Only try if there are semicolons between what look like date segments
    if ';' not in text:
        return []

    segments = [s.strip() for s in text.split(';')]
    results: list[tuple[str, Optional[str]]] = []

    for seg in segments:
        m = _SEG_RE.search(seg)
        if not m:
            continue
        mon_str, day_str, yr_str, time_text = m.groups()
        month = _MONTH.get(mon_str.lower())
        day = int(day_str)
        if not month or not (1 <= day <= 31):
            continue
        year = int(yr_str) if yr_str else _best_year(month, day, year_hint)

        tr = _parse_time_range(time_text)
        start_iso = _dt(year, month, day, tr[0], tr[1]) if tr else _dt(year, month, day)
        end_iso   = _dt(year, month, day, tr[2], tr[3]) if tr else None

        if start_iso:
            results.append((start_iso, end_iso))

    return results if len(results) >= 2 else []


# ── Pattern 2: Compact date range  "June 5–7" or "June 5-7, 2026" ─────────────

_RANGE_RE = re.compile(
    r'(' + _MON_PAT + r')\s+'
    r'(\d{1,2})(?:st|nd|rd|th)?'
    r'\s*(?:–|—|-)\s*'
    r'(\d{1,2})(?:st|nd|rd|th)?'
    r'(?:,?\s*(20\d{2}))?',
    re.IGNORECASE,
)

_SHARED_TIME_RE = re.compile(
    r'(?:from|at)\s+(.+)',
    re.IGNORECASE,
)


def _parse_date_range(text: str, year_hint: Optional[int]) -> list[str]:
    """
    Parse "June 5–7, 2026 [from time]" into a list of start datetimes,
    one per day. Returns [] if not found or only one day.
    """
    m = _RANGE_RE.search(text)
    if not m:
        return []

    mon_str, start_day, end_day, yr_str = m.groups()
    month = _MONTH.get(mon_str.lower())
    start_d, end_d = int(start_day), int(end_day)
    if not month or start_d >= end_d or (end_d - start_d) > 14:
        return []

    year_hint = int(yr_str) if yr_str else year_hint or _extract_year(text)
    year = _best_year(month, start_d, year_hint)

    time_text = text[m.end():]
    tm = _SHARED_TIME_RE.search(time_text)
    tr = _parse_time_range(tm.group(1)) if tm else None
    sh, sm = (tr[0], tr[1]) if tr else (0, 0)

    return [
        iso for d in range(start_d, end_d + 1)
        if (iso := _dt(year, month, d, sh, sm))
    ]


# A run across months, "September 30, 2026 - October 31, 2026": its two ends are
# the event's start and end, not a list of occurrences.
_SPAN_RE = re.compile(
    r'(?:' + _MON_PAT + r')\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*20\d{2})?'
    r'\s*(?:–|—|-|\bto\b|\bthrough\b|\bthru\b)\s*'
    r'(?:' + _MON_PAT + r')\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*20\d{2})?',
    re.IGNORECASE,
)


# ── Pattern 3: Explicit date list  "June 5, June 12, June 19 [at time]" ────────

_DATE_LIST_RE = re.compile(
    r'(' + _MON_PAT + r')\s+(\d{1,2})(?:st|nd|rd|th)?',
    re.IGNORECASE,
)


def _parse_date_list(text: str, year_hint: Optional[int]) -> list[tuple[str, Optional[str]]]:
    """
    Find two or more explicit month+day occurrences in the text.
    Returns [(start_iso, end_iso_or_None), ...].
    Only activated when there are ≥ 2 distinct dates.
    """
    # Look for a shared time range
    time_m = re.search(r'(?:from|at)\s+(.+)', text, re.IGNORECASE)
    tr = _parse_time_range(time_m.group(1)) if time_m else None

    year = year_hint or _extract_year(text)
    results: list[tuple[str, Optional[str]]] = []
    seen: set[str] = set()

    for m in _DATE_LIST_RE.finditer(text):
        month = _MONTH.get(m.group(1).lower())
        day = int(m.group(2))
        if not month or not (1 <= day <= 31):
            continue
        yr = _best_year(month, day, year)
        start = _dt(yr, month, day, tr[0], tr[1]) if tr else _dt(yr, month, day)
        end   = _dt(yr, month, day, tr[2], tr[3]) if tr else None
        if start and start not in seen:
            seen.add(start)
            results.append((start, end))

    return results if len(results) >= 2 else []


# ── Pattern 4: Exception phrases ──────────────────────────────────────────────

_EXCEPT_RE = re.compile(
    r'(?:'
    r'except(?:ing)?(?:\s+on)?'
    r'|no\s+(?:show|event|performance|class|session)\s+on'
    r'|closed?\s+on'
    r'|not\s+held\s+on'
    r')\s+'
    r'((?:' + _MON_PAT + r')\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*20\d{2})?'
    r'(?:\s*(?:and|,)\s*(?:' + _MON_PAT + r')\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s*20\d{2})?)*)',
    re.IGNORECASE,
)

# "and June 5" or ", June 5" within an exception clause
_ADDL_EXCEPT_RE = re.compile(
    r'(?:and|,)\s+(' + _MON_PAT + r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(20\d{2}))?',
    re.IGNORECASE,
)


def _parse_exceptions(text: str, year_hint: Optional[int]) -> list[dict]:
    """
    Return exdate dicts for phrases like "except July 4" or "no event on July 4 and July 11".
    """
    year = year_hint or _extract_year(text)
    results: list[dict] = []

    for em in _EXCEPT_RE.finditer(text):
        clause = em.group(1)
        # Parse the first date in the clause
        first = _DATE_LIST_RE.search(clause)
        if first:
            month = _MONTH.get(first.group(1).lower())
            day = int(first.group(2))
            if month and 1 <= day <= 31:
                yr = year or _best_year(month, day, None)
                iso = _dt(yr, month, day)
                if iso:
                    results.append({'datetime': iso, 'reason': ''})
        # Parse any additional dates in the same clause ("and July 11")
        for am in _ADDL_EXCEPT_RE.finditer(clause):
            month = _MONTH.get(am.group(1).lower())
            day = int(am.group(2))
            yr_str = am.group(3)
            if month and 1 <= day <= 31:
                yr = int(yr_str) if yr_str else (year or _best_year(month, day, None))
                iso = _dt(yr, month, day)
                if iso:
                    results.append({'datetime': iso, 'reason': ''})

    return results


# ── Single date  "October 31, 2026" / "31 Oct" / "10/31/2026" / "2026-10-31" ────

_WEEKDAY_PREFIX = r'(?:(?P<wd>' + _WD_PAT + r')\.?,?\s+)?'
_DATE_FORMS = [
    ('iso', r'(?P<y>20\d{2})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b'),
    ('month_first', _WEEKDAY_PREFIX + r'(?P<mon>' + _MON_PAT + r')\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?\b'
                    r'(?!\s*(?:[ap]\.?m\b|:))(?:,?\s*(?P<y>20\d{2}))?'),
    ('day_first', _WEEKDAY_PREFIX + r'(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>' + _MON_PAT + r')\b\.?'
                  r'(?:,?\s*(?P<y>20\d{2}))?'),
    ('mdy', _WEEKDAY_PREFIX + r'(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2}|\d{4}))?\b(?!\s*(?:[ap]\.?m\b|:))'),
]
_DATE_AT_START = [(form, re.compile('^' + body, re.IGNORECASE)) for form, body in _DATE_FORMS]
_DATE_ANYWHERE = [(form, re.compile(r'(?<![\w/])' + body, re.IGNORECASE)) for form, body in _DATE_FORMS]

_WEEKDAY_INDEX = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}
_RRULE_INDEX = {'MO': 0, 'TU': 1, 'WE': 2, 'TH': 3, 'FR': 4, 'SA': 5, 'SU': 6}


def _date_from(m: re.Match, year_hint: Optional[int]) -> Optional[date]:
    """
    A matched date. A stated weekday picks the year when none is printed
    ("Thursday 5/28" is the 5/28 that's a Thursday), and a weekday no nearby
    year agrees with means it isn't a date.
    """
    g = m.groupdict()
    month = _MONTH[g['mon'].lower()] if g.get('mon') else int(g['m'])
    day = int(g['d'])
    year = int(g['y']) + (2000 if len(g['y']) == 2 else 0) if g.get('y') else None
    try:
        if year:
            return date(year, month, day)
        if g.get('wd') and not year_hint:
            weekday = _WEEKDAY_INDEX[g['wd'][:3].lower()]
            today = date.today()
            return next((d for y in (today.year, today.year + 1, today.year - 1)
                         if (d := date(y, month, day)).weekday() == weekday), None)
        return date(_best_year(month, day, year_hint), month, day)
    except ValueError:
        return None


def parse_date(text: str, year_hint: Optional[int] = None) -> Optional[date]:
    """
    The date at the start of `text`, or None. A yearless date takes year_hint,
    else its nearest future occurrence. Numeric dates are read US-style (M/D).
    """
    text = text.strip()
    for _, pattern in _DATE_AT_START:
        if m := pattern.match(text):
            return _date_from(m, year_hint)
    return None


def find_dates(text: str) -> list[tuple[date, int, int]]:
    """
    Every date in `text` in order, with where each starts and ends. A bare
    numeric date ("3/4") counts only with a weekday or a year, so fractions
    and "24/7" don't.
    """
    found = []
    for form, pattern in _DATE_ANYWHERE:
        for m in pattern.finditer(text):
            if form == 'mdy' and not (m.group('wd') or m.group('y')):
                continue
            if day := _date_from(m, None):
                found.append((m.start(), -m.end(), day))
    dates, taken = [], -1
    for at, end, day in sorted(found):
        if at >= taken:
            dates.append((day, at, -end))
            taken = -end
    return dates


def find_date(text: str) -> Optional[tuple[date, int, int]]:
    """The first date anywhere in `text` (see find_dates)."""
    found = find_dates(text)
    return found[0] if found else None


# ── Times  "7pm" / "8–11pm" / "show at 8" / "6:30 doors, 7 show" ───────────────

_AMPM = r'(?:a\.?m\b\.?|p\.?m\b\.?|noon)'
_CLOCK = r'(\d{1,2})(?::(\d{2}))?'
_START_WORD = r'(?:show(?:time|s)?|music|starts?|begins?)'
_DOORS_WORD = r'doors?(?:[ \t]+open)?'


def _word_time(word: str) -> re.Pattern:
    """A time said with `word` before or after it: "show at 8", "7 show", "Doors: 7pm"."""
    return re.compile(
        r'(?<![\d:/])' + _CLOCK + r'[ \t]*(' + _AMPM + r')?[ \t]*' + word + r'\b'
        r'|\b' + word + r'[ \t]*(?:at|@|:|-)?[ \t]*' + _CLOCK + r'[ \t]*(' + _AMPM + r')?(?![\d/+%:])',
        re.IGNORECASE,
    )


_START_TIME = _word_time(_START_WORD)
_DOORS_TIME = _word_time(_DOORS_WORD)
_ONE_TIME = re.compile(r'(?<![\d:/])' + _CLOCK + r'[ \t]*(' + _AMPM + r')', re.IGNORECASE)
_TWENTY_FOUR = re.compile(r'(?<![\d:/])(1[3-9]|2[0-3]):([0-5]\d)(?![\d:])')
# "at 8" only at the end of a phrase: "at 8 locations" isn't a time.
_AT_TIME = re.compile(r'(?:\bat|@)[ \t]*' + _CLOCK + r"(?=[ \t]*(?:$|[|,.;!)–—-]|o'?clock))",
                      re.IGNORECASE | re.MULTILINE)
_MORNING = re.compile(r'\b(?:morning|brunch|breakfast)\b', re.IGNORECASE)


def _hour(hour: int, period: Optional[str], text: str) -> int:
    """24-hour; a bare hour is evening unless the text talks about the morning."""
    if period:
        return 12 if period.lower() == 'noon' else _to24(hour, period)
    if hour == 0 or hour >= 12:
        return hour
    return hour if _MORNING.search(text) else hour + 12


def _clock(m: re.Match, text: str, offset: int = 0) -> tuple[int, int]:
    hour, minute, period = m.group(1 + offset), m.group(2 + offset), m.group(3 + offset)
    return _hour(int(hour), period, text), int(minute or 0)


def find_time(text: str) -> Optional[tuple[int, int, Optional[int], Optional[int]]]:
    """
    (start_h, start_m, end_h, end_m) of the first time `text` gives, end None
    when it gives only a start. The show time beats the doors time.
    """
    for pattern in (_START_TIME, _DOORS_TIME):
        if m := pattern.search(text):
            h, mi = _clock(m, text, 0 if m.group(1) else 3)
            ranged = _parse_time_range(text[m.start(4) if m.group(4) else m.start():m.end() + 16])
            if ranged and ranged[:2] == (h, mi):
                return ranged
            if 0 <= h < 24 and mi < 60:
                return h, mi, None, None
    if found := _parse_time_range(text):
        return found
    for m in _ONE_TIME.finditer(text):
        h, mi = _clock(m, text)
        if 0 <= h < 24 and mi < 60:
            return h, mi, None, None
    if m := _TWENTY_FOUR.search(text):
        return int(m.group(1)), int(m.group(2)), None, None
    for m in _AT_TIME.finditer(text):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if 0 < hour <= 12 and minute < 60:
            return _hour(hour, None, text), minute, None, None
    return None


def _times(day: date, clock: Optional[tuple]) -> dict:
    if not clock:
        return {'start_datetime': day.isoformat()}
    sh, sm, eh, em = clock
    start = datetime(day.year, day.month, day.day, sh, sm)
    result = {'start_datetime': start.strftime('%Y-%m-%dT%H:%M:%S')}
    if eh is not None:
        end = datetime(day.year, day.month, day.day, eh, em)
        if end <= start:
            end += timedelta(days=1)
        result['end_datetime'] = end.strftime('%Y-%m-%dT%H:%M:%S')
    return result


# How far past the title a single event's date is looked for; further on are other events.
DATE_WINDOW = 3000


def after_title(text: str, title: Optional[str]) -> str:
    """The page text from the title's first appearance (else the top), DATE_WINDOW long."""
    at = text.lower().find(title.strip().lower()) if title and title.strip() else -1
    return text[max(at, 0):][:DATE_WINDOW]


def at_most_one_day(text: str, title: Optional[str] = None) -> bool:
    """
    Does the text after the title name one day, or none? A page naming several
    is a listing or carries other events, and one of them can't be picked blind.
    """
    return len({day for day, _, _ in find_dates(after_title(text, title))}) <= 1


def find_start(text: str, title: Optional[str] = None) -> Optional[dict]:
    """
    One event's date and time from its own text: the first date after the
    title, with the time on its own line or the next few, else further down
    ("Thursday 5/28" … "doors at 7:30 | show at 8"). Returns start/end, or None.
    """
    region = after_title(text, title)
    found = find_date(region)
    if not found:
        return None
    day, start, end = found
    line = region.rfind('\n', 0, start) + 1
    near_end = end
    for _ in range(3):
        nl = region.find('\n', near_end + 1)
        near_end = nl if nl >= 0 else len(region)
    clock = find_time(region[line:near_end]) or find_time(region[end:])
    return _times(day, clock)


def next_occurrence(byday: list[str], clock: Optional[tuple] = None, tz: Optional[str] = None) -> Optional[dict]:
    """
    The next start on one of `byday` (RRULE codes like "TH") at `clock`, at or
    after now in `tz`: "Thursdays at 8" seen on a Friday starts next Thursday.
    """
    days = {_RRULE_INDEX[code] for code in byday if code in _RRULE_INDEX}
    if not days:
        return None
    now = datetime.now(ZoneInfo(tz)).replace(tzinfo=None) if tz else datetime.now()
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        if clock and datetime(day.year, day.month, day.day, clock[0], clock[1]) < now:
            continue
        return _times(day, clock)
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def extract(text: str, year_hint: Optional[int] = None) -> Optional[dict]:
    """
    Parse date/time occurrences from event page text.

    year_hint  — explicit year to anchor dates that have no year in the text.
                 If None, the extractor first searches the text for a 4-digit year,
                 then falls back to the nearest future occurrence of each date.

    Returns a dict with any subset of:
      start_datetime  str          — ISO-8601 (only set when no upstream date exists)
      end_datetime    str          — ISO-8601
      rdates          list[str]    — ISO-8601 additional occurrence starts
      exdates         list[dict]   — [{"datetime": str, "reason": str}]
    Returns None if nothing useful was found.
    """
    year = year_hint or _extract_year(text)
    result: dict = {}

    # Exceptions — collect first, then strip their clause text so downstream
    # parsers don't re-interpret exception dates as occurrence dates.
    exdates = _parse_exceptions(text, year)
    if exdates:
        result['exdates'] = exdates
    remaining = _EXCEPT_RE.sub(' ', text)

    # Multi-day festival segments separated by semicolons (highest priority)
    multi = _parse_multi_day(remaining, year)
    if multi:
        result['start_datetime'] = multi[0][0]
        if multi[0][1]:
            result['end_datetime'] = multi[0][1]
        result['rdates'] = [occ[0] for occ in multi[1:]]
        return result or None

    # Compact date range: "June 5–7"
    range_dates = _parse_date_range(remaining, year)
    if range_dates:
        result['start_datetime'] = range_dates[0]
        if len(range_dates) > 1:
            result['rdates'] = range_dates[1:]
        return result or None

    # Explicit date list: "June 5, June 12, June 19 at 7 pm"
    date_list = _parse_date_list(_SPAN_RE.sub(' ', remaining), year)
    if date_list:
        result['start_datetime'] = date_list[0][0]
        if date_list[0][1]:
            result['end_datetime'] = date_list[0][1]
        if len(date_list) > 1:
            result['rdates'] = [d[0] for d in date_list[1:]]
        return result or None

    return result or None
