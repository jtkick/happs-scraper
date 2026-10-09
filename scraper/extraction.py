"""
Page → events. Shared by the spider and the test-case runner (scraper/eval/run.py).

extract_page(html, url, ...)  → PageResult
    Every event the page's own content yields, most trustworthy source first:

    Multi-event pages (listings)
      ≥2 JSON-LD / microdata events    → those
      ≥2 complete inline-JSON events   → those
      ≥2 recipe events (learned CSS)   → those
    Single-event pages (details) — the original waterfall, merged field by field
      JSON-LD → inline JSON (always; may lengthen description) → OpenGraph
      → AI (only if still no title + start date)
    AI may itself return a list, which then replaces the single event.

finalize(data, ...)  → dict | None
    Fill gaps from the listing partial and the ambient context, then run the
    unconditional passes: recurrence, explicit dates, tags. Ambient context
    may never supply a title or start date — those must come per event.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from scraper import text as text_module
from scraper.extractors import (
    jsonld,
    opengraph,
    inline_json,
    tags as tag_matcher,
    recurrence as recurrence_extractor,
    dates as dates_extractor,
)

logger = logging.getLogger(__name__)

# Per-event keys that ambient (venue/listing-level) context must never supply.
PER_EVENT_KEYS = frozenset({'title', 'start_datetime', 'end_datetime'})

# Bump whenever a change makes the same page parse differently: pages parsed by
# an older version are fetched again instead of being skipped as unchanged.
EXTRACTION_VERSION = 1


@dataclass
class PageResult:
    # (fields, raw JSON-LD node or None) per event
    events: list[tuple[dict, Optional[dict]]] = field(default_factory=list)
    # True when the page is one event, so page-wide text belongs to it
    single: bool = True
    # 'jsonld' | 'inline_json' | 'recipe' | 'ai' | 'waterfall' | ''
    strategy: str = ''
    ai_used: bool = False
    ai_truncated: bool = False


def extract_page(
    html: str,
    url: str,
    *,
    recipe_events: Optional[list[dict]] = None,
    ai: Optional[Callable] = None,
) -> PageResult:
    """
    `recipe_events`  — events a learned recipe pulled from this page
    `ai`             — callable(html, url) → ai.AIResult, or None to disable AI
    """
    jl_pairs = jsonld.extract_all_with_nodes(html, url)
    if len(jl_pairs) >= 2:
        return PageResult(
            events=[(_tag(d, 'jsonld'), node) for d, node in jl_pairs],
            single=False, strategy='jsonld')

    if not jl_pairs:
        inline_all = inline_json.extract_all(html, url)
        if len(inline_all) >= 2:
            return PageResult(
                events=[(_tag(d, 'inline_json'), None) for d in inline_all],
                single=False, strategy='inline_json')

    if recipe_events and len(recipe_events) >= 2:
        return PageResult(
            events=[(_tag(d, 'recipe'), None) for d in recipe_events],
            single=False, strategy='recipe')

    # ── Single-event waterfall ────────────────────────────────────────────────
    page: dict = {}
    jl_node = None
    if jl_pairs:
        merge(page, jl_pairs[0][0])
        jl_node = jl_pairs[0][1]
        page.setdefault('extraction_method', 'jsonld')

    inline = inline_json.extract(html, url)
    if inline:
        merge_enriched(page, inline)
        page.setdefault('extraction_method', 'inline_json')

    if not sufficient(page):
        og = opengraph.extract(html, url)
        if og:
            merge(page, og)
            page.setdefault('extraction_method', 'opengraph')

    if not sufficient(page) and recipe_events:
        merge(page, recipe_events[0])
        page.setdefault('extraction_method', 'recipe')

    result = PageResult(strategy='waterfall')
    if not sufficient(page) and ai is not None:
        ai_result = ai(html, url)
        if ai_result is not None:
            result.ai_used = True
            result.ai_truncated = ai_result.truncated
            if len(ai_result.events) >= 2:
                result.events = [(_tag(d, 'ai'), None) for d in ai_result.events]
                result.single = False
                result.strategy = 'ai'
                return result
            if ai_result.events:
                event = ai_result.events[0]
                if 'drop_reason' in event and not page:
                    page = _tag(event, 'ai')
                elif 'drop_reason' not in event:
                    merge(page, event)
                    page.setdefault('extraction_method', 'ai')

    if page:
        result.events = [(page, jl_node)]
    return result


def finalize(
    data: dict,
    *,
    context: Optional[dict] = None,
    partial: Optional[dict] = None,
    jsonld_node: Optional[dict] = None,
    page_text: Optional[str] = None,
    full_text: Optional[str] = None,
) -> Optional[dict]:
    """
    Page data wins; the listing partial (this same event as seen on its
    listing page) fills gaps; ambient context fills what is left, minus
    per-event keys. Returns None when no title survives.

    `page_text` is the page's main content (for dates); `full_text` is all of
    it (for recurrence phrases near the title). Both only on single-event pages.
    """
    data = dict(data)
    for key, value in (partial or {}).items():
        if value is not None and not data.get(key):
            data[key] = value
    for key, value in (context or {}).items():
        if key in PER_EVENT_KEYS:
            continue
        if value is not None and not data.get(key):
            data[key] = value

    if not data.get('title'):
        return None

    rec = recurrence_extractor.extract(
        title=data.get('title', ''),
        description=data.get('description', ''),
        jsonld_node=jsonld_node,
        schedule_text=data.get('_schedule_text'),
        page_text=full_text,
    )
    for key, value in (rec or {}).items():
        if not data.get(key):
            data[key] = value

    # Explicit dates: page-wide text only describes this event on a
    # single-event page; on listings it would smear one event's dates
    # onto all of them.
    for text_src in filter(None, [page_text, data.get('_schedule_text'), data.get('description')]):
        date_info = dates_extractor.extract(text_src)
        if not date_info:
            continue
        if date_info.get('start_datetime') and not data.get('start_datetime'):
            data['start_datetime'] = date_info['start_datetime']
        if date_info.get('end_datetime') and not data.get('end_datetime'):
            data['end_datetime'] = date_info['end_datetime']
        if date_info.get('rdates'):
            existing = data.get('rdates') or []
            data['rdates'] = list(dict.fromkeys(existing + date_info['rdates']))
        if date_info.get('exdates'):
            data.setdefault('exdates', [])
            seen = {e['datetime'] for e in data['exdates']}
            data['exdates'] += [e for e in date_info['exdates'] if e['datetime'] not in seen]
        break

    _fit_recurrence_span(data)

    matched = tag_matcher.match(data.get('title', ''), data.get('description', ''), ctx=data)
    data['tag_names'] = list(dict.fromkeys((data.get('tag_names') or []) + matched))
    return data


def finalize_page(result: PageResult, html: str, *, context: Optional[dict] = None,
                  partial: Optional[dict] = None) -> list[dict]:
    """
    Every event a spider emits for one page: each extracted event finalized,
    or the listing partial alone when the page itself yielded nothing.
    """
    full = text_module.full_text(html) if result.single and result.events else None
    text = page_text(html, full=full) if result.single else None
    events = []
    for data, node in result.events:
        final = finalize(data, context=context, partial=partial, jsonld_node=node,
                         page_text=text, full_text=full)
        if final is None:
            logger.debug("Extracted event without a title — skipping")
            continue
        events.append(final)
    if not result.events and partial:
        final = finalize(partial, context=context)
        if final:
            events.append(final)
    return events


# Below this, trafilatura found no real main content (a JS shell's "browser not supported").
THIN_TEXT = 200


def page_text(html: str, full: Optional[str] = None) -> Optional[str]:
    """Main-content text via trafilatura; all the page's text when that comes back thin."""
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False, include_tables=False)
    except ImportError:
        text = None
    if text and len(text) >= THIN_TEXT:
        return text
    return (full if full is not None else text_module.full_text(html)) or text


_PERIOD_DAYS = {'daily': 1, 'weekly': 7, 'monthly': 28, 'yearly': 365}


def _fit_recurrence_span(data: dict) -> None:
    """
    A recurring event whose start and end are days apart is usually listed as
    its whole run ("Sep 30 – Oct 31, recurring daily"): the end date is the
    last occurrence, not when the first one ends. A span shorter than the
    repeat is one long occurrence instead (a 4-day festival held every year).
    """
    freq = data.get('recurrence_freq')
    if freq in (None, 'none') or data.get('recurrence_count'):
        return
    start, end = parse_iso(data.get('start_datetime')), parse_iso(data.get('end_datetime'))
    if not start or not end or end.date() <= start.date():
        return
    if (end.date() - start.date()).days < _PERIOD_DAYS.get(freq, 1) * (data.get('recurrence_interval') or 1):
        return
    until = data.get('recurrence_until')
    if until and until != end.date().isoformat():
        return
    data['recurrence_until'] = end.date().isoformat()
    timed = 'T' in str(data['end_datetime'])
    if timed and end.time() > start.time():
        data['end_datetime'] = datetime.combine(start.date(), end.timetz()).isoformat()
    else:
        data['end_datetime'] = None


def parse_iso(value) -> Optional[datetime]:
    """An ISO date or datetime string as a datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


# Fields every event gets whether or not its page said anything more.
_BOOKKEEPING = frozenset({'extraction_method', 'tag_names', 'url', 'timezone', 'evidence',
                          'confidence', 'review_required'})


def added_info(partial: dict, final: dict) -> bool:
    """Did the detail page tell us more than the listing (`partial`) already had?"""
    empty = (None, '', [], 'none')
    for key, value in final.items():
        if key.startswith('_') or key in _BOOKKEEPING or value in empty:
            continue
        if partial.get(key) in empty:
            return True
    if len(final.get('description') or '') >= len(partial.get('description') or '') + 50:
        return True
    return 'T' in str(final.get('end_datetime') or '') and 'T' not in str(partial.get('end_datetime') or '')


# ── Merge helpers ─────────────────────────────────────────────────────────────

def sufficient(data: dict) -> bool:
    """True if we have the minimum two fields the backend requires."""
    return bool(data.get('title') and data.get('start_datetime'))


def merge(base: dict, override: dict) -> None:
    """Copy non-null values into `base` without clobbering: first source wins."""
    for key, value in override.items():
        if value is not None and not base.get(key):
            base[key] = value


def merge_enriched(base: dict, override: dict) -> None:
    """Like merge, but a longer description replaces a shorter one."""
    for key, value in override.items():
        if value is None:
            continue
        if not base.get(key):
            base[key] = value
        elif key == 'description' and isinstance(value, str) and len(value) > len(str(base[key])):
            base[key] = value


def _tag(data: dict, method: str) -> dict:
    return {**data, 'extraction_method': data.get('extraction_method') or method}
