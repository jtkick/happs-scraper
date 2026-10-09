"""
Score parsed events against a case's labels. Shared by the golden tests, the
review app and tools/evaluate.py, so "correct" means the same everywhere.

Each mismatch has a stable path, which is what a case's known_failures lists:
  events[2]                 a labelled event was not parsed
  events[2].end_datetime    it was, but this field is wrong
  extra[Gift Cards]         a parsed event that isn't labelled (complete cases only)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Optional
from zoneinfo import ZoneInfo

from scraper.util import fold

_TITLE_MATCH = 0.8
_DESCRIPTION_MATCH = 0.9
_COORD_TOLERANCE = 0.0005        # ≈ 50 m
_PRICE_TOLERANCE = 0.01

_DATETIME_FIELDS = {'start_datetime', 'end_datetime'}
_SET_FIELDS = {'tag_names', 'recurrence_byday'}
_URL_FIELDS = {'url', 'ticket_url', 'image_url'}
# Free text whose exact wording isn't the point (evidence is any span that states the date).
_UNCHECKED = {'evidence'}


@dataclass
class FieldResult:
    ok: bool
    expected: Any
    actual: Any


@dataclass
class EventMatch:
    index: int
    expected: dict
    actual: Optional[dict]
    fields: dict[str, FieldResult] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.actual is not None and all(f.ok for f in self.fields.values())


@dataclass
class Comparison:
    matches: list[EventMatch]
    extras: list[dict]              # parsed but unlabelled (only counted when complete)
    rejected: list[dict]            # parsed but listed in not_events
    complete: bool

    @property
    def found(self) -> int:
        return sum(1 for m in self.matches if m.actual is not None)

    @property
    def recall(self) -> float:
        return self.found / len(self.matches) if self.matches else 1.0

    @property
    def precision(self) -> float:
        wrong = len(self.rejected) + (len(self.extras) if self.complete else 0)
        total = self.found + wrong
        return self.found / total if total else 1.0

    @property
    def failures(self) -> list[str]:
        paths = []
        for m in self.matches:
            if m.actual is None:
                paths.append(f'events[{m.index}]')
                continue
            paths += [f'events[{m.index}].{k}' for k, r in m.fields.items() if not r.ok]
        if self.complete:
            paths += [f'extra[{e.get("title", "")}]' for e in self.extras]
        paths += [f'extra[{e.get("title", "")}]' for e in self.rejected]
        return paths


def compare(expected: list[dict], actual: list[dict], *, not_events: list[str] = (),
            complete: bool = True, timezone_name: Optional[str] = None) -> Comparison:
    pairs = _pair(expected, actual, timezone_name)
    matches = []
    for i, exp in enumerate(expected):
        got = pairs.get(i)
        match = EventMatch(index=i, expected=exp, actual=got)
        if got is not None:
            for key, value in exp.items():
                if key in _UNCHECKED:
                    continue
                match.fields[key] = FieldResult(
                    field_equal(key, value, got.get(key), timezone_name), value, got.get(key))
        matches.append(match)

    used = {id(a) for a in pairs.values()}
    rejected_titles = {fold(t) for t in not_events}
    extras, rejected = [], []
    for event in actual:
        if id(event) in used:
            continue
        (rejected if fold(event.get('title', '')) in rejected_titles else extras).append(event)
    return Comparison(matches=matches, extras=extras, rejected=rejected, complete=complete)


def field_equal(key: str, expected: Any, actual: Any, timezone_name: Optional[str] = None) -> bool:
    if _empty(expected):
        return _empty(actual)
    if _empty(actual):
        return False
    if key in _DATETIME_FIELDS:
        return _instant(expected, timezone_name) == _instant(actual, timezone_name)
    if key == 'rdates':
        return ({_instant(v, timezone_name) for v in expected}
                == {_instant(v, timezone_name) for v in actual})
    if key == 'exdates':
        return ({_instant(_exdate(v), timezone_name) for v in expected}
                == {_instant(_exdate(v), timezone_name) for v in actual})
    if key in _SET_FIELDS:
        return {fold(v) for v in expected} == {fold(v) for v in actual}
    if key in ('location_lat', 'location_lon'):
        return abs(float(expected) - float(actual)) <= _COORD_TOLERANCE
    if key == 'ticket_price':
        return abs(float(expected) - float(actual)) <= _PRICE_TOLERANCE
    if key in _URL_FIELDS:
        return str(expected).strip().rstrip('/') == str(actual).strip().rstrip('/')
    if key == 'description':
        return similarity(expected, actual) >= _DESCRIPTION_MATCH
    if isinstance(expected, str):
        return fold(expected) == fold(str(actual))
    return expected == actual


def _pair(expected: list[dict], actual: list[dict], timezone_name: Optional[str]) -> dict[int, dict]:
    """Greedy best-first pairing on title similarity, with matching start time as a tie-break."""
    scored = []
    for i, exp in enumerate(expected):
        for j, got in enumerate(actual):
            score = similarity(exp.get('title', ''), got.get('title', ''))
            if score < _TITLE_MATCH:
                continue
            if exp.get('start_datetime') and field_equal(
                    'start_datetime', exp['start_datetime'], got.get('start_datetime'), timezone_name):
                score += 1
            scored.append((score, i, j))
    pairs, used = {}, set()
    for _, i, j in sorted(scored, key=lambda s: (-s[0], s[1], s[2])):
        if i in pairs or j in used:
            continue
        pairs[i] = actual[j]
        used.add(j)
    return pairs


def _instant(value, timezone_name: Optional[str]) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone_name) if timezone_name else timezone.utc)
    return dt.astimezone(timezone.utc)


def _exdate(value) -> Any:
    return value.get('datetime') if isinstance(value, dict) else value


def _empty(value) -> bool:
    return value is None or value == '' or value == [] or value == {}



def similarity(a, b) -> float:
    a, b = fold(a), fold(b)
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()
