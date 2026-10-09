"""
A case: one saved page, what the scraper parsed from it, and what it should
have parsed. Reviewed cases live in tests/fixtures/<id>/ (golden tests and
training data); unreviewed ones wait in review/inbox/<id>/.

    case.json         url, kind, captured_at, context, the true `events`, …
    page.html         the page as fetched
    parsed.json       the scraper's output when captured (never edited)
    ai_response.json  the model's raw answer when captured, replayed offline

Labels in `events`: a key asserts its value, a missing key is not checked,
null asserts the field is empty. `complete: false` means the labels are a
subset of the page's events, so extra parsed events are not errors.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scraper.items import EVENT_FIELDS

ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = ROOT / 'tests' / 'fixtures'
INBOX_DIR = ROOT / 'review' / 'inbox'

CASE_FILE = 'case.json'
PAGE_FILE = 'page.html'
PARSED_FILE = 'parsed.json'
AI_FILE = 'ai_response.json'

KINDS = ('listing', 'detail')
SOURCES = ('manual', 'crawl', 'correction', 'report')

# Fields a label may assert, in the order the review app shows them.
LABEL_FIELDS = [f for f in EVENT_FIELDS if f not in ('timezone', 'drop_reason')]


@dataclass
class Case:
    id: str
    url: str
    html: str = ''
    kind: str = 'listing'
    captured_at: str = ''
    source: str = 'manual'
    source_ref: Optional[str] = None
    seed_context: dict = field(default_factory=dict)
    partial: Optional[dict] = None
    recipe: dict = field(default_factory=dict)
    platform: Optional[str] = None
    events: list[dict] = field(default_factory=list)
    not_events: list[str] = field(default_factory=list)
    complete: bool = True
    known_failures: list[dict] = field(default_factory=list)
    notes: str = ''
    reviewer: Optional[str] = None
    reviewed_at: Optional[str] = None
    parsed: Optional[dict] = None
    ai_response: Optional[dict] = None

    @property
    def captured(self) -> datetime:
        dt = datetime.fromisoformat(self.captured_at)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @property
    def timezone(self) -> Optional[str]:
        return (self.seed_context or {}).get('timezone')

    def validate(self) -> list[str]:
        problems = []
        if not self.url:
            problems.append('url is required')
        if self.kind not in KINDS:
            problems.append(f'kind must be one of {KINDS}')
        if self.source not in SOURCES:
            problems.append(f'source must be one of {SOURCES}')
        try:
            self.captured
        except (TypeError, ValueError):
            problems.append('captured_at must be an ISO datetime')
        for i, event in enumerate(self.events):
            if not event.get('title'):
                problems.append(f'events[{i}] needs a title')
            unknown = set(event) - set(LABEL_FIELDS)
            if unknown:
                problems.append(f'events[{i}] has unknown fields {sorted(unknown)}')
        if not self.html:
            problems.append('page.html is empty')
        return problems

    # ── Disk ──────────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> 'Case':
        data = json.loads((path / CASE_FILE).read_text())
        data.pop('version', None)
        case = cls(id=path.name, **data)
        case.html = _read(path / PAGE_FILE) or ''
        parsed, ai_response = _read(path / PARSED_FILE), _read(path / AI_FILE)
        case.parsed = json.loads(parsed) if parsed else None
        case.ai_response = json.loads(ai_response) if ai_response else None
        return case

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        data = {'version': 2}
        for key in ('url', 'kind', 'captured_at', 'source', 'source_ref', 'seed_context', 'partial',
                    'recipe', 'platform', 'events', 'not_events', 'complete', 'known_failures',
                    'notes', 'reviewer', 'reviewed_at'):
            value = getattr(self, key)
            if value not in (None, '', [], {}) or key in ('events', 'complete'):
                data[key] = value
        _write_json(path / CASE_FILE, data)
        (path / PAGE_FILE).write_text(self.html)
        for name, value in ((PARSED_FILE, self.parsed), (AI_FILE, self.ai_response)):
            if value is not None:
                _write_json(path / name, value)
            else:
                (path / name).unlink(missing_ok=True)


def load_all(root: Path = FIXTURES_DIR) -> list[Case]:
    if not root.exists():
        return []
    return [Case.load(d) for d in sorted(root.iterdir()) if (d / CASE_FILE).exists()]


def slug(text: str) -> str:
    text = re.sub(r"['’]", '', text.lower())
    text = re.sub(r'[^\w-]+', '-', text)
    return re.sub(r'[_-]+', '-', text).strip('-')[:60].rstrip('-') or 'case'


def unique_id(base: str, *roots: Path) -> str:
    base = slug(base)
    candidate, n = base, 2
    while any((root / candidate).exists() for root in roots or (FIXTURES_DIR, INBOX_DIR)):
        candidate, n = f'{base}-{n}', n + 1
    return candidate


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read(path: Path) -> Optional[str]:
    return path.read_text() if path.exists() else None


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
