"""
Item pipelines — run in priority order for every EventItem.

  100  NormalizePipeline           — clean & parse dates/text (venue timezone)
  150  ValidatePipeline            — drop non-/past events, score confidence
  200  FingerprintDedupPipeline    — source fingerprint, drop in-run repeats
  300  APISubmitPipeline           — upsert into the backend

Drops raise DropItem('<reason_code>: detail'); the run tracker records the
code so a crawl can explain what it missed.
"""

from __future__ import annotations
import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import dateparser
from scrapy.exceptions import DropItem

from scraper import cleaners

logger = logging.getLogger(__name__)


def spider_setting(spider, name, default=None):
    settings = getattr(spider, 'settings', None)
    return settings.get(name, default) if settings is not None else default


# ── 100 · Normalize ───────────────────────────────────────────────────────────

class NormalizePipeline:
    _DATE_SETTINGS = {
        'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DAY_OF_MONTH': 'first',
        # Listings print "Fri, Jun 5" without a year: that means the next one.
        'PREFER_DATES_FROM': 'future',
        'TO_TIMEZONE': 'UTC',
    }

    def process_item(self, item, spider):
        # Validate required fields before anything else.
        raw_title = self._clean(item.get('title'))
        if not raw_title:
            raise DropItem("missing_title")

        tz = item.get('timezone') or spider_setting(spider, 'DEFAULT_EVENT_TIMEZONE')
        start = self._parse_date(item.get('start_datetime'), tz)
        if not start:
            raise DropItem(f"unparseable_start: {raw_title}")
        item['start_datetime'] = start

        # Normalize all other fields first so cross-field cleaners (e.g.
        # venue-suffix stripper) see clean values when they run on the title.
        item['description']      = self._clean(item.get('description'))
        item['end_datetime']     = self._parse_date(item.get('end_datetime'), tz)
        item['location_title']   = self._clean(item.get('location_title'))
        item['location_address'] = self._clean(item.get('location_address'))
        item['url']              = self._clean(item.get('url'))
        item['image_url']        = self._clean(item.get('image_url'))
        item['ticket_url']       = self._clean(item.get('ticket_url'))

        item['title'] = cleaners.clean_title(raw_title, dict(item)) or raw_title

        item['ticket_price'] = self._parse_price(item.get('ticket_price'))

        if not isinstance(item.get('tag_names'), list):
            item['tag_names'] = []

        # Recurrence defaults. Spiders set every field, so an unset one is None, not missing.
        for key, default in (('recurrence_freq', 'none'), ('recurrence_interval', 1),
                             ('recurrence_byday', []), ('recurrence_month_mode', 'day'),
                             ('recurrence_until', None), ('recurrence_count', None)):
            if item.get(key) is None:
                item[key] = default

        # Normalize rdates: parse each entry as a date string
        raw_rdates = item.get('rdates') or []
        item['rdates'] = [
            p for r in raw_rdates if (p := self._parse_date(r, tz))
        ]
        if raw_rdates:
            logger.debug("rdates: %d raw → %d parsed", len(raw_rdates), len(item['rdates']))

        # Normalize exdates: ensure each entry is {"datetime": str, "reason": str}
        raw_exdates = item.get('exdates') or []
        normalized_ex = []
        for ex in raw_exdates:
            if isinstance(ex, dict):
                dt = self._parse_date(ex.get('datetime'), tz)
                if dt:
                    normalized_ex.append({'datetime': dt, 'reason': ex.get('reason', '')})
            elif isinstance(ex, str):
                dt = self._parse_date(ex, tz)
                if dt:
                    normalized_ex.append({'datetime': dt, 'reason': ''})
        item['exdates'] = normalized_ex

        return item

    def _clean(self, value) -> Optional[str]:
        if not value:
            return None
        value = re.sub(r'\s+', ' ', str(value)).strip()
        return value or None

    @staticmethod
    def _parse_price(value) -> Optional[float]:
        """'$25.00' → 25.0, 'Free' → 0.0, '$10–$15' → 10.0 (the lowest price)."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value)
        if re.search(r'\bfree\b', text, re.IGNORECASE):
            return 0.0
        m = re.search(r'\d[\d,]*(?:\.\d+)?', text)
        return float(m.group().replace(',', '')) if m else None

    def _parse_date(self, value, tz: Optional[str] = None) -> Optional[str]:
        """
        Parse to an aware UTC ISO string. Strings without an offset are read
        in `tz` (the venue's zone) — without it they would silently take the
        scraper machine's local zone.
        """
        if not value:
            return None
        if hasattr(value, 'isoformat'):
            if tz and getattr(value, 'tzinfo', 1) is None:
                value = value.replace(tzinfo=ZoneInfo(tz))
            return value.isoformat()
        settings = {**self._DATE_SETTINGS, 'TIMEZONE': tz} if tz else self._DATE_SETTINGS
        parsed = dateparser.parse(str(value), settings=settings)
        if parsed is None:
            logger.warning("_parse_date: could not parse %r", value)
        return parsed.isoformat() if parsed else None


# ── 150 · Validate + confidence ───────────────────────────────────────────────

# Titles that are site furniture, not events.
_NON_EVENT_TITLE = re.compile(
    r'^(?:opening|business|store|kitchen|office)?\s*hours\b|\bgift\s*cards?\b|\bmenus?$|'
    r'\bbook\s+a\s+table\b|\bprivate\s+(?:hire|events?|parties)\b|\breservations?$|'
    r'^(?:closed|we are closed|temporarily closed)\b|\bcareers?\b|\bjobs?\b',
    re.IGNORECASE,
)

_METHOD_CONFIDENCE = {
    'ical': 0.95, 'jsonld': 0.9, 'inline_json': 0.8, 'recipe': 0.75,
    'selectors': 0.75, 'opengraph': 0.6, 'ai': 0.5,
}


class ValidatePipeline:
    """
    Drop things that aren't upcoming public events, and score the rest.

    Every drop raises DropItem('<reason_code>: detail') so crawl runs can
    report *why* an event was missed. Items below REVIEW_THRESHOLD are sent
    to the backend with review_required=True (held for a human).
    """

    MAX_FUTURE = timedelta(days=548)

    def process_item(self, item, spider):
        if item.get('drop_reason'):
            raise DropItem(f"{item['drop_reason']}: {item.get('title')}")

        title = item.get('title') or ''
        if not 3 <= len(title) <= 200:
            raise DropItem(f"bad_title_length: {title[:60]}")
        if _NON_EVENT_TITLE.search(title):
            raise DropItem(f"not_an_event: {title}")
        venue = item.get('location_title')
        if venue and _norm(title) == _norm(venue):
            raise DropItem(f"title_is_venue: {title}")

        now = datetime.now(timezone.utc)
        start = _iso(item.get('start_datetime'))
        end = _iso(item.get('end_datetime'))
        if start and start > now + self.MAX_FUTURE:
            raise DropItem(f"too_far_future: {title}")
        if start and self._is_past(item, start, end, now):
            raise DropItem(f"past_event: {title}")

        item['confidence'] = self.score(item)
        threshold = spider_setting(spider, 'REVIEW_THRESHOLD', 0.7)
        item['review_required'] = item['confidence'] < threshold
        return item

    @staticmethod
    def _is_past(item, start, end, now) -> bool:
        if item.get('recurrence_freq', 'none') != 'none':
            until = item.get('recurrence_until')
            return bool(until) and str(until) < now.date().isoformat()
        if item.get('rdates'):
            last = max(filter(None, map(_iso, item['rdates'])), default=None)
            if last and last >= now:
                return False
        return (end or start + timedelta(hours=12)) < now

    @staticmethod
    def score(item) -> float:
        method = (item.get('extraction_method') or '').split(':')[0]
        base = 0.9 if method == 'platform' else _METHOD_CONFIDENCE.get(method, 0.5)
        bonus = sum(0.05 for present in (
            item.get('end_datetime'),
            item.get('location_address') or item.get('location_lat') is not None,
            item.get('description'),
        ) if present)
        return round(min(1.0, base + bonus), 2)


def _iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(text: str) -> str:
    return FingerprintDedupPipeline._normalize(text)


# Item keys that describe the crawl, not the event.
_BOOKKEEPING = frozenset({'source_url', 'source_id', 'fingerprint', 'ingest_status'})


def dry_run(items, spider) -> tuple[list[dict], list[dict]]:
    """
    Normalize + Validate without sending anything: (kept, dropped) as plain
    dicts, each dropped one with its 'drop_reason' code. Used to snapshot a
    page with the outcome a crawl gives it, and by the review tools.
    """
    normalize, validate = NormalizePipeline(), ValidatePipeline()
    kept, dropped = [], []
    for item in items:
        item = item.copy()
        try:
            item = validate.process_item(normalize.process_item(item, spider), spider)
        except DropItem as exc:
            dropped.append({**_plain(item), 'drop_reason': str(exc).split(':', 1)[0]})
            continue
        kept.append(_plain(item))
    return kept, dropped


def _plain(item) -> dict:
    return {k: v for k, v in dict(item).items() if k not in _BOOKKEEPING and v is not None}


# ── 200 · Fingerprint (in-run dedup) ──────────────────────────────────────────

class FingerprintDedupPipeline:
    """
    Assign each event its source_fingerprint and drop repeats within one run
    (e.g. the same show on a listing and on its detail page). Cross-run
    identity and updates are the backend's job (upsert by fingerprint).

    Recurring events
      sha256(title + location + recurrence_signature) — the date is excluded
      because sites keep bumping the "next occurrence".
    One-off events
      sha256(title + date + location).

    Fingerprints are prefixed with the backend source id (or the spider
    name), so two sites never collide.
    """

    def open_spider(self, spider):
        self.seen: set[str] = set()

    def process_item(self, item, spider):
        from scraper.extractors.recurrence import signature
        if item.get('recurrence_freq', 'none') != 'none':
            fp = self._recurring_fingerprint(item, signature)
        else:
            fp = self._oneoff_fingerprint(item)
        prefix = item.get('source_id') or getattr(spider, 'name', None) or 'unknown'
        item['fingerprint'] = f'{prefix}:{fp}'
        if item['fingerprint'] in self.seen:
            raise DropItem(f"duplicate_in_run: {item.get('title')}")
        self.seen.add(item['fingerprint'])
        return item

    # ── Fingerprint helpers ───────────────────────────────────────────────────

    def _oneoff_fingerprint(self, item: dict) -> str:
        key = '|'.join([
            self._normalize(item.get('title', '')),
            self._date_only(item.get('start_datetime', '')),
            self._normalize(
                item.get('location_title', '') or item.get('location_address', '')
            ),
        ])
        return hashlib.sha256(key.encode()).hexdigest()

    def _recurring_fingerprint(self, item: dict, signature_fn) -> str:
        rec = {k: item.get(k) for k in (
            'recurrence_freq', 'recurrence_interval',
            'recurrence_byday', 'recurrence_month_mode',
        )}
        key = '|'.join([
            self._normalize(item.get('title', '')),
            self._normalize(
                item.get('location_title', '') or item.get('location_address', '')
            ),
            signature_fn(rec),
        ])
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize('NFKD', str(text or '').lower())
        text = re.sub(r'[^\w\s]', '', text)
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _date_only(dt_str: str) -> str:
        return str(dt_str)[:10] if dt_str else ''


# ── 300 · API submit ──────────────────────────────────────────────────────────

class APISubmitPipeline:
    """
    Upsert each event into the backend (POST api/scraper/events/). The
    backend creates, updates, or no-ops by fingerprint; the outcome is
    recorded on item['ingest_status'] for the crawl-run report.
    """

    def open_spider(self, spider):
        from scraper.sources.client import BackendClient
        self.client = BackendClient.from_settings(spider.settings)
        if not self.client.token:
            logger.warning("HAPPS_SCRAPER_TOKEN not set — submissions will be rejected")

    def close_spider(self, spider):
        self.client.close()

    def process_item(self, item, spider):
        status, body = self.client.ingest(self._build_payload(item))
        if status == 201:
            item['ingest_status'] = 'created'
            logger.info("Created: '%s' (%s)", item.get('title'), body.get('id'))
        elif status == 200:
            item['ingest_status'] = 'updated' if body.get('changed') else 'unchanged'
            if body.get('changed'):
                logger.info("Updated: '%s' %s", item.get('title'), body['changed'])
        else:
            item['ingest_status'] = 'failed'
            logger.error("Ingest failed (%s) for '%s': %s", status, item.get('title'), body)
        return item

    @staticmethod
    def _build_payload(item: dict) -> dict:
        return {
            'title':                item.get('title'),
            'description':          item.get('description'),
            'start_datetime':       item.get('start_datetime'),
            'end_datetime':         item.get('end_datetime'),
            'location_title':       item.get('location_title'),
            'location_address':     item.get('location_address'),
            'location_lat':         item.get('location_lat'),
            'location_lon':         item.get('location_lon'),
            'ticket_price':         item.get('ticket_price'),
            'ticket_url':           item.get('ticket_url'),
            'url':                  item.get('url') or item.get('source_url'),
            'image_url':            item.get('image_url'),
            'tag_names':            item.get('tag_names', []),
            'source_id':            item.get('source_id'),
            'source_url':           item.get('source_url'),
            'source_fingerprint':   item.get('fingerprint'),
            'extraction_method':    item.get('extraction_method'),
            'confidence':           item.get('confidence'),
            'review_required':      bool(item.get('review_required')),
            'recurrence_freq':      item.get('recurrence_freq', 'none'),
            'recurrence_interval':  item.get('recurrence_interval', 1),
            'recurrence_byday':     item.get('recurrence_byday', []),
            'recurrence_month_mode': item.get('recurrence_month_mode', 'day'),
            'recurrence_until':     item.get('recurrence_until'),
            'recurrence_count':     item.get('recurrence_count'),
            'rdates':               item.get('rdates', []),
            'exdates':              item.get('exdates', []),
        }
