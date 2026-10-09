"""
AI-powered extraction with Claude Haiku — the last resort when a page has no
structured data — plus the cheap helper calls that let the generic spider
learn a site once and then run without AI:

  extract_many()        page text → every event on the page
  extract()             single-event convenience wrapper (first event)
  pick_events_links()   homepage links → the ones that lead to event listings
  suggest_recipe()      page structure + known events → CSS selectors (recipe.py verifies)

Output is constrained with structured outputs (output_config.format), so the
response is always schema-valid JSON — no fence stripping or repair.

Hallucination guard: every event must quote an `evidence` snippet (the text
its date/time came from). Events whose snippet is not in the page text are
returned with drop_reason='ai_unverified' so the pipeline drops them *and*
records why.

Disable entirely by leaving ANTHROPIC_API_KEY blank in .env.
"""

from __future__ import annotations
import functools
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from scraper.text import main_text
from scraper.util import fold

logger = logging.getLogger(__name__)

MODEL = 'claude-haiku-4-5'
MAX_TEXT_CHARS = 40_000
MIN_TEXT_CHARS = 200

# Keys each extracted event carries (also the fine-tuning target shape).
OUTPUT_KEYS = [
    'title', 'description', 'start_datetime', 'end_datetime',
    'location_title', 'location_address',
    'ticket_price', 'ticket_url', 'url', 'image_url', 'evidence',
]

_NULLABLE_STR = {'anyOf': [{'type': 'string'}, {'type': 'null'}]}
_NULLABLE_NUM = {'anyOf': [{'type': 'number'}, {'type': 'null'}]}

EVENTS_SCHEMA = {
    'type': 'object',
    'properties': {
        'events': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'title':            {'type': 'string'},
                    'description':      _NULLABLE_STR,
                    'start_datetime':   {'type': 'string'},
                    'end_datetime':     _NULLABLE_STR,
                    'location_title':   _NULLABLE_STR,
                    'location_address': _NULLABLE_STR,
                    'ticket_price':     _NULLABLE_NUM,
                    'ticket_url':       _NULLABLE_STR,
                    'url':              _NULLABLE_STR,
                    'image_url':        _NULLABLE_STR,
                    'evidence':         {'type': 'string'},
                },
                'required': OUTPUT_KEYS,
                'additionalProperties': False,
            },
        },
    },
    'required': ['events'],
    'additionalProperties': False,
}

_LINKS_SCHEMA = {
    'type': 'object',
    'properties': {'urls': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['urls'],
    'additionalProperties': False,
}

_SYSTEM = """\
You extract upcoming public events from the text of a web page for an event-discovery app.

Rules:
- Return every distinct event or event date listed on the page. A listing page may hold dozens.
- An event needs a name and a specific start date. Skip opening hours, menus, specials without a date, \
gift cards, private-hire offers and past events.
- start_datetime / end_datetime: ISO-8601 local time exactly as the page states it, WITHOUT a UTC \
offset unless the page prints one (e.g. 2026-06-15T19:00). If no time is given, use the date only \
(2026-06-15). If the year is missing, choose the next occurrence on or after today's date.
- evidence: copy, verbatim, the shortest span of page text that states the event's date and time. \
Do not paraphrase it.
- ticket_price: the lowest price in the page's currency; 0 for free; null if not stated.
- Use null for anything the page does not state. Never invent URLs or details."""


@dataclass
class AIResult:
    events: list[dict] = field(default_factory=list)
    truncated: bool = False


def build_user_message(text: str, *, url: str = '', venue: Optional[str] = None,
                       today: Optional[date] = None) -> str:
    """The exact user turn sent to the model (scraper/eval/dataset.py reuses it)."""
    header = [f'Today: {(today or date.today()).isoformat()}', f'Page URL: {url}']
    if venue:
        header.append(f'Venue (if the page does not name another): {venue}')
    return '\n'.join(header) + '\n\nPage text:\n' + text[:MAX_TEXT_CHARS]


def prompt_hash(user_message: str) -> str:
    """Identifies the exact prompt an answer was given for, so a replay can tell it's stale."""
    return hashlib.sha256((_SYSTEM + '\n' + user_message).encode()).hexdigest()[:16]


def extract_many(html: str, base_url: str, api_key: str, *,
                 venue: Optional[str] = None, today: Optional[date] = None,
                 respond: Optional[Callable] = None) -> AIResult:
    """
    Ask Claude for every event on the page. Returns an empty result on any failure.

    `respond(system, user, schema, label)` stands in for the API call; the
    review tools use it to record a response and replay it offline.
    """
    text = page_text(html)
    if not text or len(text) < MIN_TEXT_CHARS:
        return AIResult()

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        logger.info("AI input for %s truncated from %d to %d chars", base_url, len(text), MAX_TEXT_CHARS)

    respond = respond or functools.partial(_call, api_key)
    data = respond(_SYSTEM, build_user_message(text, url=base_url, venue=venue, today=today),
                   EVENTS_SCHEMA, base_url)
    if not data:
        return AIResult(truncated=truncated)

    haystack = fold(text)
    events = []
    for event in data.get('events', []):
        clean = {k: v for k, v in event.items() if v not in (None, '')}
        evidence = fold(clean.get('evidence', ''))
        if not evidence or evidence not in haystack:
            clean['drop_reason'] = 'ai_unverified'
        events.append(clean)
    return AIResult(events=events, truncated=truncated)


def extractor(api_key: str, *, venue: Optional[str] = None, today: Optional[date] = None,
              respond: Optional[Callable] = None) -> Callable:
    """The `ai` callable scraper/extraction.py takes: (html, url) → AIResult."""
    return lambda html, url: extract_many(html, url, api_key, venue=venue, today=today, respond=respond)


def recorder(api_key: str, record: Callable[[dict], None], model: Optional[str] = None) -> Callable:
    """A `respond` that calls the model and hands `record` its exact answer (for snapshots and test cases)."""
    used = model or MODEL

    def respond(system, user, schema, label):
        data = _call(api_key, system, user, schema, label, model=used)
        record({'model': used, 'prompt_hash': prompt_hash(user), 'response': data})
        return data
    return respond


def extract(html: str, base_url: str, api_key: str) -> Optional[dict]:
    """Single-event convenience wrapper: the first verified event, or None."""
    for event in extract_many(html, base_url, api_key).events:
        if 'drop_reason' not in event:
            return event
    return None


def pick_events_links(links: list[tuple[str, str]], homepage_url: str, api_key: str,
                      limit: int = 3) -> list[str]:
    """
    Given a homepage's (anchor text, absolute URL) pairs, return up to `limit`
    URLs most likely to list the site's upcoming events. Only URLs from the
    input are ever returned.
    """
    if not links:
        return []
    listing = '\n'.join(f'- {text.strip()[:80] or "(no text)"} | {url}' for text, url in links[:200])
    prompt = (
        f'These are the links on the homepage of {homepage_url}.\n'
        f'Return up to {limit} URLs that most likely list upcoming events, shows, a calendar or a '
        'schedule, best first. Return an empty list if none do.\n\n' + listing
    )
    data = _call(api_key, None, prompt, _LINKS_SCHEMA, homepage_url, max_tokens=1024)
    allowed = {url for _, url in links}
    return [u for u in (data or {}).get('urls', []) if u in allowed][:limit]


_RECIPE_SCHEMA = {
    'type': 'object',
    'properties': {
        'item_css': {'type': 'string'},
        'fields': {
            'type': 'object',
            'properties': {name: _NULLABLE_STR for name in
                           ('title', 'date', 'time', 'description', 'link', 'image', 'price')},
            'required': ['title', 'date', 'time', 'description', 'link', 'image', 'price'],
            'additionalProperties': False,
        },
        'detail_link_css': _NULLABLE_STR,
        'pagination_css': _NULLABLE_STR,
    },
    'required': ['item_css', 'fields', 'detail_link_css', 'pagination_css'],
    'additionalProperties': False,
}

_RECIPE_SYSTEM = """\
You write CSS selectors (as used by Python's parsel/Scrapy) that extract events from a listing page.

- item_css selects one element per event. It must match every listed event and nothing else.
- Each field selector is relative to the item element. Without a pseudo-element the element's full \
text is used; append ::attr(name) for an attribute (prefer time::attr(datetime) when present) or \
::text for direct text only. link defaults to the href attribute and image to src.
- date must yield the event's date; time its start time if shown separately, else null.
- Prefer stable class names and structure over positional selectors like :nth-child.
- detail_link_css (absolute, not relative to the item) selects the href of each event's own page, \
or null. pagination_css selects the href of the next page of the listing, or null."""


def suggest_recipe(skeleton_html: str, events: list[dict], url: str, api_key: str) -> Optional[dict]:
    """Ask Claude for CSS selectors reproducing `events` on this page (verified by the caller)."""
    sample = [{'title': e.get('title'), 'start': e.get('start_datetime'), 'evidence': e.get('evidence')}
              for e in events[:12]]
    prompt = (f'Page URL: {url}\n\nEvents already extracted from this page:\n'
              f'{json.dumps(sample, ensure_ascii=False, indent=1)}\n\n'
              f'Page structure (scripts removed, long text truncated):\n{skeleton_html}')
    return _call(api_key, _RECIPE_SYSTEM, prompt, _RECIPE_SCHEMA, url, max_tokens=2048)


# ── Internals ─────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=4)
def _client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def _call(api_key: str, system: Optional[str], user: str, schema: dict, label: str,
          max_tokens: int = 16000, model: str = MODEL) -> Optional[dict]:
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping AI call")
        return None

    kwargs = {
        'model': model,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': user}],
        'output_config': {'format': {'type': 'json_schema', 'schema': schema}},
    }
    if system:
        kwargs['system'] = system

    try:
        message = _client(api_key).messages.create(**kwargs)
    except anthropic.RateLimitError as exc:
        logger.warning("Claude rate-limited for %s: %s", label, exc)
        return None
    except anthropic.APIStatusError as exc:
        logger.warning("Claude API error %s for %s: %s", exc.status_code, label, exc.message)
        return None
    except anthropic.APIConnectionError as exc:
        logger.warning("Claude connection error for %s: %s", label, exc)
        return None

    if message.stop_reason in ('refusal', 'max_tokens'):
        logger.warning("Claude stopped with %s for %s", message.stop_reason, label)
        return None

    raw = next((b.text for b in message.content if b.type == 'text'), '')
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("AI returned invalid JSON for %s: %s", label, exc)
        return None
    return data if isinstance(data, dict) else None


def page_text(html: str) -> Optional[str]:
    """The page text the model sees (tables kept: schedules often live in them)."""
    return main_text(html, tables=True)
