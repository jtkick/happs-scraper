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
      → AI (only if still no title + start date, or always on a listing page)
    AI may itself return a list, which then replaces the single event.

parse_page(response, kind, ...) → (PageResult, [event])
parse_page_async(...)              the same, with the model called in a thread
parse_feed(adapter, response, ...) → [event] | None
    One page start to finish, as the crawl parses it. The spider and the
    test-case runner (scraper/eval/run.py) both call these, so the fixtures
    test exactly what the crawl does. extract_page and parse_page are written
    once, as generators that pause (yield) when they need the model and are
    sent its answer; the sync and async entry points only differ in how they
    get that answer, so the crawl never blocks on Claude.
    A listing's events that came without a link get the one in their entry
    (scraper/discovery/event_links.py), and its links are classified
    (PageResult.links). A detail page found by such a guessed link only
    counts for the event it shows.

finalize(data, ...)  → dict | None
    Fill gaps from the listing partial and the ambient context, then run the
    unconditional passes: recurrence, explicit dates, tags. Ambient context
    may never supply a title or start date — those must come per event.
"""

from __future__ import annotations
import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from scraper import text as text_module
from scraper.discovery import event_links, links as link_classifier
from scraper.items import RECURRENCE_DEFAULTS
from scraper.util import fold, parse_iso
from scraper.extractors import (
    recipe as recipe_extractor,
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
EXTRACTION_VERSION = 13


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
    # A listing's detail links and next page (scraper/discovery/links.py)
    links: Optional[link_classifier.ListingLinks] = None


def parse_page(response, *, kind: str, recipe: Optional[dict] = None, context: Optional[dict] = None,
               partial: Optional[dict] = None, ai: Optional[Callable] = None) -> tuple[PageResult, list[dict]]:
    """
    A listing (`kind='listing'`) adds the learned recipe's events to the waterfall,
    and asks the model even when the waterfall found one complete event, since
    that may be one of many the page lists.
    A detail page takes `partial`, its listing entry, which fills its gaps; AI is
    skipped when that entry already has a title and start.
    """
    steps = _parse_steps(response, kind, recipe, context, partial, use_ai=ai is not None)
    return _run(steps, lambda: ai(response.text, response.url))


async def parse_page_async(response, *, kind: str, recipe: Optional[dict] = None,
                           context: Optional[dict] = None, partial: Optional[dict] = None,
                           ai: Optional[Callable] = None) -> tuple[PageResult, list[dict]]:
    """parse_page, with the model called in a thread so the crawl's other pages keep moving."""
    steps = _parse_steps(response, kind, recipe, context, partial, use_ai=ai is not None)
    return await _run_async(steps, lambda: asyncio.to_thread(ai, response.text, response.url))


def _parse_steps(response, kind, recipe, context, partial, use_ai):
    recipe = recipe or {}
    recipe_events = None
    if kind == 'listing':
        partial = None
        if recipe.get('item_css'):
            recipe_events = recipe_extractor.extract(response, recipe)
    elif partial and sufficient(partial):
        use_ai = False
    listing = kind == 'listing'
    result = yield from _extract_steps(response.text, response.url, recipe_events, use_ai, listing)
    if partial and not listing and result.single and any(
            NOT_FOUND_TITLE.match(data.get('title') or '') for data, _ in result.events):
        # A soft 404 (served with 200): its listing entry stands as it was, as for a failed request.
        result.events = []
    if not listing and result.single:
        # The event's own page knows where its tickets are, better than its listing entry (whose
        # link back into this site is no ticket link).
        title = (result.events[0][0] if result.events else partial or {}).get('title')
        ticket = ticket_link(response, title)
        if ticket and result.events and not result.events[0][0].get('ticket_url'):
            result.events[0][0]['ticket_url'] = ticket
        elif ticket and not result.events and partial and (
                not partial.get('ticket_url') or _same_host(partial['ticket_url'], response.url)):
            partial = {**partial, 'ticket_url': ticket}
    events = finalize_page(result, response.text, context=context, partial=partial,
                           page_is_event=not (listing and result.single))
    if listing:
        result.links = link_classifier.classify(response, detail_css=recipe.get('detail_link_css'),
                                                pagination_css=recipe.get('pagination_css'))
        event_links.attach(response.text, response.url, events, result.links.details)
    elif partial and partial.get(event_links.GUESSED):
        events = _same_event(events, partial, context)
    return result, events


def _same_event(events: list[dict], partial: dict, context: Optional[dict]) -> list[dict]:
    """
    A detail page found by a guessed link (scraper/discovery/event_links.py) only
    counts where it shows the listing's event; otherwise the listing entry stands alone.
    """
    kept = [e for e in events if event_links.same_event(partial, e)]
    if kept or not events:
        return kept
    final = finalize(partial, context=context)
    return [final] if final else []


def _run(steps, ask):
    """Drive a parse generator, answering its one request for the model with ask()."""
    try:
        next(steps)
        steps.send(ask())
    except StopIteration as done:
        return done.value
    raise RuntimeError('a parse asked for the model twice')


async def _run_async(steps, ask):
    try:
        next(steps)
        steps.send(await ask())
    except StopIteration as done:
        return done.value
    raise RuntimeError('a parse asked for the model twice')


def parse_feed(adapter, response, *, context: Optional[dict] = None) -> Optional[list[dict]]:
    """A platform feed's events, finalized; None when the adapter wants the page extracted generically."""
    events = adapter.parse(response)
    if events is None:
        return None
    finals = []
    for data in events:
        data.setdefault('extraction_method', f'platform:{adapter.name}')
        final = finalize(data, context=context)
        if final:
            finals.append(final)
    return finals


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
    return _run(_extract_steps(html, url, recipe_events, ai is not None), lambda: ai(html, url))


def _extract_steps(html, url, recipe_events, use_ai, listing=False):
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
    if use_ai and (listing or not sufficient(page)):
        ai_result = yield
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
                    if listing and page.get('extraction_method') in ('opengraph', 'inline_json'):
                        # A listing's OpenGraph or page-level JSON describes the page, not the event
                        # the model found on it: that only fills the event's gaps (its image).
                        page, base = _tag(dict(event), 'ai'), page
                        merge(page, {k: v for k, v in base.items() if k not in ('extraction_method', 'title', 'description')})
                    else:
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
    guess_start: bool = True,
) -> Optional[dict]:
    """
    Page data wins; the listing partial (this same event as seen on its
    listing page) fills gaps; ambient context fills what is left, minus
    per-event keys. Returns None when no title survives.

    `page_text` is the page's main content (for dates); `full_text` is all of
    it (for recurrence phrases near the title). Both only on single-event pages.
    `guess_start` lets a start be read from loose text (_fill_start); off for a
    listing page that came out as one event, which is the page, not an event.
    """
    data = dict(data)
    _all_day_as_dates(data)
    if partial and page_text:
        _listing_day_if_page_agrees(data, partial, page_text)
    if (partial and partial.get('title') and data.get('extraction_method') == 'opengraph'
            and not partial.get(event_links.GUESSED) and not _share_words(data.get('title'), partial['title'])):
        # OpenGraph's title is the page's; the listing named the event. (A guessed link's page is
        # judged by its own title, so that one stays.)
        data['title'] = partial['title']
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

    # A start that names no date ("EVERY THU 8–11pm" from a recipe) is a schedule,
    # and so is the AI's evidence for an event it dated from one.
    undated = _undated(data.get('start_datetime'))
    own_text = dict(
        title=data.get('title', ''),
        description=' '.join(filter(None, [data.get('description'), data.get('evidence'), undated])),
        jsonld_node=jsonld_node,
        schedule_text=data.get('_schedule_text'),
    )
    if _session_title(data.get('title')) and not (jsonld_node and recurrence_extractor.extract('', '', jsonld_node)):
        # One dated session of a series: the series' schedule isn't this event's.
        rec, rec_from_page = None, False
        for key in RECURRENCE_DEFAULTS:
            data.pop(key, None)
    else:
        rec = recurrence_extractor.extract(**own_text)
        rec_from_page = not rec and bool(full_text)
        if rec_from_page:
            rec = recurrence_extractor.extract(**own_text, page_text=full_text)
    for key, value in (rec or {}).items():
        if not data.get(key):
            data[key] = value

    # Explicit dates: page-wide text only describes this event on a
    # single-event page; on listings it would smear one event's dates
    # onto all of them.
    for text_src in filter(None, [page_text and _without_pager(page_text), data.get('_schedule_text'),
                                  data.get('description')]):
        date_info = dates_extractor.extract(text_src)
        if not date_info:
            continue
        if date_info.get('start_datetime') and not data.get('start_datetime'):
            data['start_datetime'] = date_info['start_datetime']
        if date_info.get('end_datetime') and not data.get('end_datetime'):
            data['end_datetime'] = date_info['end_datetime']
        rdates = _other_days(data, date_info.get('rdates') or [])
        if rdates:
            existing = data.get('rdates') or []
            data['rdates'] = list(dict.fromkeys(existing + rdates))
        if date_info.get('exdates'):
            data.setdefault('exdates', [])
            seen = {e['datetime'] for e in data['exdates']}
            data['exdates'] += [e for e in date_info['exdates'] if e['datetime'] not in seen]
        break

    # A page naming several days is a listing or shows other events: its text
    # can't say which date, or whose schedule, is this event's.
    if guess_start:
        one_event = bool(page_text) and dates_extractor.at_most_one_day(page_text, data.get('title'))
        _fill_start(data, page_text if one_event else None, undated,
                    weekly_ok=one_event or not rec_from_page)
        if one_event:
            _time_for_the_day(data, page_text)
    _fit_recurrence_span(data)

    matched = tag_matcher.match(data.get('title', ''), data.get('description', ''), ctx=data)
    data['tag_names'] = list(dict.fromkeys((data.get('tag_names') or []) + matched))
    return data


_ALL_DAY = re.compile(r'^(\d{4}-\d{2}-\d{2})T(?:00:00(?::00)?|23:59(?::59)?)(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$')


def _all_day_as_dates(data: dict) -> None:
    """Midnight to 23:59 on one day is the day itself, not times (an all-day entry, often written in UTC)."""
    start, end = (_ALL_DAY.match(str(data.get(k) or '')) for k in ('start_datetime', 'end_datetime'))
    if start and end and start.group(1) == end.group(1) and str(data['start_datetime'])[11:13] == '00' \
            and str(data['end_datetime'])[11:13] == '23':
        data['start_datetime'] = data['end_datetime'] = start.group(1)


def _listing_day_if_page_agrees(data: dict, partial: dict, page_text: str) -> None:
    """Structured data that puts the event on another day than the page's text and its listing entry
    both say (vintageonrace.com's JSON-LD dates it the day the page was fetched) loses to the entry."""
    own, listed = parse_iso(data.get('start_datetime')), parse_iso(partial.get('start_datetime'))
    if not own or not listed or own.date() == listed.date():
        return
    days = {day for day, _, _ in dates_extractor.find_dates(dates_extractor.after_title(page_text, data.get('title')))}
    if days == {listed.date()}:
        data['start_datetime'], data['end_datetime'] = partial['start_datetime'], partial.get('end_datetime')


_RECURRING_WORD = re.compile(r'\b(?:every|each|weekly|monthly|daily|nightly|through|thru|until|starting)\b', re.I)


def _session_title(title: Optional[str]) -> bool:
    """A title that names one session of a series: its own date ("First Friday November 6, 2026"), or a
    series named for its schedule with this session's name after it ("2nd Sunday Showtime: COMER")."""
    if not title or _RECURRING_WORD.search(title):
        return False
    if dates_extractor.find_date(title):
        return True
    series, _, session = title.partition(':')
    return bool(session.strip()) and bool(recurrence_extractor.extract(series, '')) \
        and not recurrence_extractor.extract(session, '')


def _series_name(title: str) -> str:
    """What a series' sessions' titles share: the part before ":", or the title without its date."""
    series, sep, _ = title.partition(':')
    if sep:
        return fold(series)
    found = dates_extractor.find_dates(title)
    for _, at, end in reversed(found):
        title = title[:at] + title[end:]
    return fold(re.sub(r'[\s,|–-]+$', '', title))


def _dated_sessions(events: list[dict]) -> None:
    """
    A listing that lists a series' sessions on their own dates ("Young Adult Lab: Paper Portraits", Oct 1;
    "…: Glow Face Paint", Oct 8, each blurb saying "every Thursday") shows occurrences, not one endless
    series per entry: each is a one-off. Entries of one series share a name and a rule.
    """
    groups: dict[tuple, set] = {}
    for e in events:
        if e.get('recurrence_freq') in (None, 'none') or not parse_iso(e.get('start_datetime')):
            continue
        key = (_series_name(e.get('title') or ''), e['recurrence_freq'], tuple(sorted(e.get('recurrence_byday') or [])))
        groups.setdefault(key, set()).add(parse_iso(e['start_datetime']).date())
    sessions = {key for key, days in groups.items() if len(days) >= 2}
    for e in events:
        if e.get('recurrence_freq') in (None, 'none') or not parse_iso(e.get('start_datetime')):
            continue
        key = (_series_name(e.get('title') or ''), e['recurrence_freq'], tuple(sorted(e.get('recurrence_byday') or [])))
        if key in sessions:
            e.update(RECURRENCE_DEFAULTS)
            e['recurrence_byday'] = []


_TICKET_TEXT = re.compile(r'^\W*(?:(?:buy|get|purchase|book|order)\s+(?:your\s+)?)?tickets?(?:\s+(?:now|here|online))?\W*$',
                          re.IGNORECASE)
TICKET_HOSTS = ('cincyticket.com', 'etix.com', 'ticketweb.com', 'eventbrite.com', 'dice.fm', 'seetickets.us',
                'ticketmaster.com', 'axs.com', 'tixr.com', 'showclix.com', 'ticketleap.com', 'universe.com',
                'brownpapertickets.com', 'ludus.com', 'simpletix.com', 'tix.com', 'eventim.com', 'humanitix.com')
_SLUG_STOP = frozenset({'tickets', 'ticket', 'event', 'events', 'the', 'and', 'with', 'for', 'presents', 'live'})


def _host(url: str) -> str:
    return (urlparse(url).hostname or '').removeprefix('www.')


def _same_host(a: str, b: str) -> bool:
    return _host(a) == _host(b)


def _about_another_event(url: str, title: str, page_url: str) -> bool:
    """A link whose slug names an event ("rumble-at-rhinegeist-nightmare-on-elm-street") sharing no word
    with this one's title, the site's name aside: a promotion for something else."""
    slug = next((seg for seg in reversed(urlparse(url).path.split('/')) if seg), '')
    parts = [w for w in re.split(r'[-_]+', slug.casefold()) if len(w) >= 3 and not w.isdigit()]
    if len(parts) < 3:
        return False
    site = re.sub(r'[^a-z0-9]', '', _host(page_url).split('.')[0])
    words = {w for w in parts if w not in _SLUG_STOP and w not in site}
    return not words & {w.casefold() for w in _WORD.findall(title or '')}


def ticket_link(response, title: Optional[str] = None) -> Optional[str]:
    """An event page's ticket link: the first anchor outside the site's header, nav and footer saying
    "Tickets" that leaves this site (or names tickets in its path), and isn't about another event."""
    if any(_host(response.url) == h or _host(response.url).endswith('.' + h) for h in TICKET_HOSTS):
        return None     # the page is the ticket page
    for anchor in response.xpath('//a[@href][not(ancestor::nav or ancestor::header or ancestor::footer)]'):
        href = anchor.attrib.get('href', '').strip()
        if not href or href.startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            continue
        url = response.urljoin(href)
        text = re.sub(r'\(.*?\)', ' ', ' '.join(t.strip() for t in anchor.css('::text').getall() if t.strip()))
        if not _TICKET_TEXT.match(text):
            continue
        if _same_host(url, response.url) and 'ticket' not in urlparse(url).path.casefold():
            continue
        if not _about_another_event(url, title, response.url):
            return url
    return None


_VERSUS = re.compile(r'^(.+?)\s+(?:vs\.?|v\.?|versus)\s+\S', re.IGNORECASE)
_AT = re.compile(r'^(.+?)\s+at\s+\S', re.IGNORECASE)


def _away_games(events: list[dict]) -> None:
    """On a team's schedule ("Cincinnati vs. Utah"), "Cincinnati at BYU" is a road game, somewhere else:
    not a local event."""
    home = {fold(m.group(1)) for e in events if (m := _VERSUS.match(e.get('title') or ''))}
    for e in events:
        m = _AT.match(e.get('title') or '')
        if m and fold(m.group(1)) in home and not _VERSUS.match(e['title']):
            e['drop_reason'] = 'away_game'


NOT_FOUND_TITLE = re.compile(r'^\W*(?:page not found|not found|(?:error )?404\b|nothing found|page unavailable)',
                             re.IGNORECASE)
_WORD = re.compile(r'[^\W\d_]{3,}')


def _share_words(a: Optional[str], b: Optional[str]) -> bool:
    return bool({w.casefold() for w in _WORD.findall(a or '')} & {w.casefold() for w in _WORD.findall(b or '')})


_PAGER_WORD = re.compile(r'^\W*(?:previous|prev|next|older|newer)(?: (?:event|post))?\W*$', re.IGNORECASE)


def _without_pager(text: str) -> str:
    """The page text without a post pager ("Previous / March 7 / Kaleidoscope / Next / June 13 / …",
    Squarespace's .item-pagination): those are the neighbouring events' dates and titles."""
    lines = text.split('\n')
    out, i = [], 0
    while i < len(lines):
        if _PAGER_WORD.match(lines[i]):
            j = i
            while j < len(lines) and (_PAGER_WORD.match(lines[j]) or not lines[j].strip()):
                j += 1
            if j < len(lines) and len(lines[j]) <= 40 and dates_extractor.find_date(lines[j]):
                i = j + 2
                continue
        out.append(lines[i])
        i += 1
    return '\n'.join(out)


def _local(value, tz: Optional[str]) -> Optional[datetime]:
    moment = parse_iso(value)
    if moment and moment.tzinfo and tz:
        try:
            return moment.astimezone(ZoneInfo(tz))
        except (KeyError, ValueError):
            pass
    return moment


def _other_days(data: dict, rdates: list[str]) -> list[str]:
    """Explicit dates that are further occurrences: not a day within the event's own run (an overnight
    end, a themed night of a play's run). One read without a time takes the event's start time."""
    tz = data.get('timezone')
    start = _local(data.get('start_datetime'), tz)
    if not start:
        return rdates
    end = _local(data.get('end_datetime'), tz)
    last = max(start.date(), end.date()) if end else start.date()
    kept = []
    for value in rdates:
        day = parse_iso(value)
        if day is None:
            kept.append(value)
            continue
        if start.date() <= day.date() <= last:
            continue
        if day.time() == time(0) and start.time() != time(0):
            value = day.replace(hour=start.hour, minute=start.minute, second=start.second).isoformat()
        kept.append(value)
    return kept


def finalize_page(result: PageResult, html: str, *, context: Optional[dict] = None,
                  partial: Optional[dict] = None, page_is_event: bool = True) -> list[dict]:
    """
    Every event a spider emits for one page: each extracted event finalized,
    or the listing partial alone when the page itself yielded nothing.

    `page_is_event` is off for a listing page that came out as one event: its
    text lists other events, so it may not supply dates, recurrence or a start.
    """
    page_wide = result.single and page_is_event
    full = text_module.full_text(html) if page_wide and result.events else None
    text = text_module.main_text(html, full=full) if page_wide else None
    events = []
    for data, node in result.events:
        final = finalize(data, context=context, partial=partial, jsonld_node=node,
                         page_text=text, full_text=full, guess_start=page_is_event)
        if final is None:
            logger.debug("Extracted event without a title — skipping")
            continue
        events.append(final)
    if not result.single:
        _dated_sessions(events)
        _away_games(events)
    if not result.events and partial:
        final = finalize(partial, context=context)
        if final:
            events.append(final)
    return events


def _undated(start) -> Optional[str]:
    """`start` when it's text with no date in it, else None."""
    if not isinstance(start, str) or re.match(r'\s*\d{4}-\d{2}-\d{2}', start):
        return None
    return None if dates_extractor.find_date(start) else start


def _fill_start(data: dict, page_text: Optional[str], undated: Optional[str], weekly_ok: bool = True) -> None:
    """
    The last try for an event's start. A dated one from its own text (one date
    and an informal time: "January 9th | 6:30 doors, 7 show"); failing that,
    a weekly one's next occurrence ("Thursdays at 8" seen on a Friday starts
    next Thursday), its days from the event's schedule or, with nothing else
    to go on, its title ("Tasting Tuesdays").
    """
    if data.get('start_datetime') and not undated:
        return
    texts = [data.get('_schedule_text'), data.get('description'), data.get('evidence')]
    found = None
    if not undated:
        found = next(filter(None, (dates_extractor.find_start(text, data.get('title'))
                                   for text in filter(None, [page_text, *texts]))), None)
    if not found:
        if data.get('recurrence_freq') in (None, 'none'):
            if undated:
                return
            for key, value in (recurrence_extractor.from_title_days(data.get('title')) or {}).items():
                data[key] = value
        if data.get('recurrence_freq') != 'weekly' or not weekly_ok:
            return
        page = dates_extractor.after_title(page_text, data.get('title')) if page_text else None
        clock = next(filter(None, map(dates_extractor.find_time, filter(None, [undated, *texts, page]))), None)
        found = dates_extractor.next_occurrence(data.get('recurrence_byday') or [], clock, data.get('timezone'))
        if not found:
            return
    data['start_datetime'] = found['start_datetime']
    if found.get('end_datetime') and not data.get('end_datetime'):
        data['end_datetime'] = found['end_datetime']


def _time_for_the_day(data: dict, page_text: str) -> None:
    """A start that's only a date takes the time the page states for that date ("February 26, 2027
    07:00 PM"); the end too when it's a range ("5-9pm")."""
    start = str(data.get('start_datetime') or '')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', start):
        return
    found = dates_extractor.find_start(page_text, data.get('title'))
    if not found or not found['start_datetime'].startswith(start + 'T'):
        return
    data['start_datetime'] = found['start_datetime']
    if not found.get('end_datetime'):
        found['end_datetime'] = _until_time(page_text, data.get('title'), found['start_datetime'])
    end = str(data.get('end_datetime') or '')
    if found.get('end_datetime') and (not end or end == start):
        data['end_datetime'] = found['end_datetime']
    elif end == start:
        data['end_datetime'] = None


def _until_time(page_text: str, title: Optional[str], start: str) -> Optional[str]:
    """The end on the start's own line: "February 26, 2027 07:00 PM until February 26, 2027 10:00 PM"."""
    region = dates_extractor.after_title(page_text, title)
    begin = datetime.fromisoformat(start)
    for day, _, at in dates_extractor.find_dates(region)[:3]:
        if day != begin.date():
            continue
        line_end = region.find('\n', at)
        line = region[at:line_end if line_end >= 0 else len(region)]
        m = re.search(r'\b(?:until|till|to)\b|[–—]', line, re.IGNORECASE)
        clock = dates_extractor.find_time(line[m.end():]) if m else None
        if clock:
            break
    else:
        return None
    end = begin.replace(hour=clock[0], minute=clock[1])
    return (end if end > begin else end + timedelta(days=1)).isoformat()


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



# Fields every event gets whether or not its page said anything more (unlike
# page_state._HOW_FOUND, which only leaves out how an event was found).
_ALWAYS_SET = frozenset({'extraction_method', 'tag_names', 'url', 'timezone', 'evidence',
                          'confidence', 'review_required'})


def added_info(partial: dict, final: dict) -> bool:
    """Did the detail page tell us more than the listing (`partial`) already had?"""
    empty = (None, '', [], 'none')
    for key, value in final.items():
        if key.startswith('_') or key in _ALWAYS_SET or value in empty:
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
