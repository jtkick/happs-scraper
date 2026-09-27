"""
Page → events. Shared by every spider (site-specific and generic).

extract_page(html, url, ...)  → PageResult
    Every event the page's own content yields, most trustworthy source first:

    Multi-event pages (listings)
      ≥2 JSON-LD / microdata events    → those
      ≥2 complete inline-JSON events   → those
      ≥2 recipe events (learned CSS)   → those
    Single-event pages (details) — the original waterfall, merged field by field
      JSON-LD → inline JSON (always; may lengthen description) → OpenGraph
      → site selectors → AI (only if still no title + start date)
    AI may itself return a list, which then replaces the single event.

finalize(data, ...)  → dict | None
    Fill gaps from the listing partial and the ambient context, then run the
    unconditional passes: recurrence, explicit dates, tags. Ambient context
    may never supply a title or start date — those must come per event.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

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
    selectors: Optional[dict] = None,
    recipe_events: Optional[list[dict]] = None,
    ai: Optional[Callable] = None,
) -> PageResult:
    """
    `selectors`      — already-extracted {field: value} from a site spider's CSS
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

    if not sufficient(page) and selectors:
        merge(page, selectors)
        page.setdefault('extraction_method', 'selectors')

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
) -> Optional[dict]:
    """
    Page data wins; the listing partial (this same event as seen on its
    listing page) fills gaps; ambient context fills what is left, minus
    per-event keys. Returns None when no title survives.
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

    matched = tag_matcher.match(data.get('title', ''), data.get('description', ''), ctx=data)
    data['tag_names'] = list(dict.fromkeys((data.get('tag_names') or []) + matched))
    return data


def page_text(html: str) -> Optional[str]:
    """Readable text via trafilatura, or None if unavailable."""
    try:
        import trafilatura
        return trafilatura.extract(html, include_comments=False, include_tables=False)
    except ImportError:
        return None


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
