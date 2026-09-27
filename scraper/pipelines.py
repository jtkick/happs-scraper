"""
Item pipelines — run in priority order for every EventItem.

  100  NormalizePipeline           — clean & parse dates/text
  200  FingerprintDedupPipeline    — drop or flag items already seen
  300  APISubmitPipeline           — POST (new) or PATCH (recurring update)
"""

from __future__ import annotations
import hashlib
import logging
import re
import sqlite3
import unicodedata
from typing import Optional

import dateparser
import requests
from scrapy.exceptions import DropItem

from scraper import cleaners

logger = logging.getLogger(__name__)


# ── 100 · Normalize ───────────────────────────────────────────────────────────

class NormalizePipeline:
    _DATE_SETTINGS = {
        'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DAY_OF_MONTH': 'first',
        'TO_TIMEZONE': 'UTC',
    }

    def process_item(self, item, spider):
        # Validate required fields before anything else.
        raw_title = self._clean(item.get('title'))
        if not raw_title:
            raise DropItem("Missing title")

        start = self._parse_date(item.get('start_datetime'))
        if not start:
            raise DropItem(f"Unparseable start_datetime for '{raw_title}'")
        item['start_datetime'] = start

        # Normalize all other fields first so cross-field cleaners (e.g.
        # venue-suffix stripper) see clean values when they run on the title.
        item['description']      = self._clean(item.get('description'))
        item['end_datetime']     = self._parse_date(item.get('end_datetime'))
        item['location_title']   = self._clean(item.get('location_title'))
        item['location_address'] = self._clean(item.get('location_address'))
        item['url']              = self._clean(item.get('url'))
        item['image_url']        = self._clean(item.get('image_url'))
        item['ticket_url']       = self._clean(item.get('ticket_url'))

        item['title'] = cleaners.clean_title(raw_title, dict(item)) or raw_title

        price = item.get('ticket_price')
        if price is not None:
            try:
                item['ticket_price'] = float(str(price).replace(',', ''))
            except (ValueError, TypeError):
                item['ticket_price'] = None

        if not isinstance(item.get('tag_names'), list):
            item['tag_names'] = []

        # Recurrence defaults
        item.setdefault('recurrence_freq', 'none')
        item.setdefault('recurrence_interval', 1)
        item.setdefault('recurrence_byday', [])
        item.setdefault('recurrence_month_mode', 'day')
        item.setdefault('recurrence_until', None)
        item.setdefault('recurrence_count', None)

        # Normalize rdates: parse each entry as a date string
        raw_rdates = item.get('rdates') or []
        item['rdates'] = [
            p for r in raw_rdates if (p := self._parse_date(r))
        ]
        if raw_rdates:
            logger.debug("rdates: %d raw → %d parsed", len(raw_rdates), len(item['rdates']))

        # Normalize exdates: ensure each entry is {"datetime": str, "reason": str}
        raw_exdates = item.get('exdates') or []
        normalized_ex = []
        for ex in raw_exdates:
            if isinstance(ex, dict):
                dt = self._parse_date(ex.get('datetime'))
                if dt:
                    normalized_ex.append({'datetime': dt, 'reason': ex.get('reason', '')})
            elif isinstance(ex, str):
                dt = self._parse_date(ex)
                if dt:
                    normalized_ex.append({'datetime': dt, 'reason': ''})
        item['exdates'] = normalized_ex

        return item

    def _clean(self, value) -> Optional[str]:
        if not value:
            return None
        value = re.sub(r'\s+', ' ', str(value)).strip()
        return value or None

    def _parse_date(self, value) -> Optional[str]:
        if not value:
            return None
        if hasattr(value, 'isoformat'):
            return value.isoformat()
        parsed = dateparser.parse(str(value), settings=self._DATE_SETTINGS)
        if parsed is None:
            logger.warning("_parse_date: could not parse %r", value)
        return parsed.isoformat() if parsed else None


# ── 200 · Fingerprint dedup ───────────────────────────────────────────────────

class FingerprintDedupPipeline:
    """
    Two dedup paths:

    Recurring events
      Fingerprint = sha256(title + location + recurrence_signature).
      The specific date is intentionally excluded because websites update
      the "next occurrence" date while the recurrence pattern stays the same.
      - First encounter  → create via API, store backend_id in recurring_fps table
      - Re-encounter     → flag item as an update (is_recurring_update=True +
                           backend_event_id) so APISubmitPipeline sends a PATCH

    Non-recurring events
      Fingerprint = sha256(title + date + location).
      Exact duplicates are dropped silently.
    """

    def open_spider(self, spider):
        path = spider.settings.get('DEDUP_DB_PATH', 'dedup.db')
        self.conn = sqlite3.connect(path)
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS fingerprints (
                fingerprint  TEXT PRIMARY KEY,
                title        TEXT,
                source_url   TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS recurring_fingerprints (
                fingerprint    TEXT PRIMARY KEY,
                title          TEXT,
                source_url     TEXT,
                backend_id     TEXT,
                start_datetime TEXT,
                created_at     TEXT DEFAULT (datetime('now')),
                updated_at     TEXT DEFAULT (datetime('now'))
            )
        ''')
        self.conn.execute('''
            CREATE TABLE IF NOT EXISTS scraped_urls (
                url           TEXT PRIMARY KEY,
                last_scraped  TEXT DEFAULT (datetime('now')),
                etag          TEXT,
                last_modified TEXT
            )
        ''')
        self.conn.commit()

    def close_spider(self, spider):
        self.conn.close()

    def process_item(self, item, spider):
        is_recurring = item.get('recurrence_freq', 'none') != 'none'

        if is_recurring:
            return self._process_recurring(item)
        else:
            return self._process_oneoff(item)

    # ── Recurring path ────────────────────────────────────────────────────────

    def _process_recurring(self, item):
        from scraper.extractors.recurrence import signature
        fp = self._recurring_fingerprint(item, signature)
        item['fingerprint'] = fp

        row = self.conn.execute(
            'SELECT backend_id, start_datetime FROM recurring_fingerprints WHERE fingerprint = ?',
            (fp,),
        ).fetchone()

        if row:
            backend_id, old_start = row
            new_start = item.get('start_datetime', '')

            if old_start == new_start:
                # Identical occurrence already stored — nothing to do.
                raise DropItem(
                    f"Recurring event already up-to-date: {item.get('title')}"
                )

            # New occurrence date — flag for a PATCH update.
            logger.info(
                "Recurring event date updated: '%s'  %s → %s",
                item.get('title'), old_start, new_start,
            )
            item['is_recurring_update'] = True
            item['backend_event_id']    = backend_id

            # Update local record now; the API call may still fail, but on
            # the next run we'll attempt the PATCH again with the current date.
            self.conn.execute(
                '''UPDATE recurring_fingerprints
                   SET start_datetime = ?, updated_at = datetime('now')
                   WHERE fingerprint = ?''',
                (new_start, fp),
            )
            self.conn.commit()
            return item

        # First time we've seen this recurring event.
        item['is_recurring_update'] = False
        item['backend_event_id']    = None
        return item

    def store_recurring_backend_id(self, fingerprint: str, backend_id: str, title: str,
                                   source_url: str, start_datetime: str):
        """Called by APISubmitPipeline after a successful POST."""
        self.conn.execute(
            '''INSERT OR REPLACE INTO recurring_fingerprints
               (fingerprint, title, source_url, backend_id, start_datetime)
               VALUES (?, ?, ?, ?, ?)''',
            (fingerprint, title, source_url, backend_id, start_datetime),
        )
        self.conn.commit()

    # ── One-off path ──────────────────────────────────────────────────────────

    def _process_oneoff(self, item):
        fp = self._oneoff_fingerprint(item)
        item['fingerprint'] = fp
        item['is_recurring_update'] = False

        if self.conn.execute(
            'SELECT fingerprint FROM fingerprints WHERE fingerprint = ?', (fp,)
        ).fetchone():
            raise DropItem(f"Duplicate event: {item.get('title')}")

        self.conn.execute(
            'INSERT INTO fingerprints (fingerprint, title, source_url) VALUES (?, ?, ?)',
            (fp, item.get('title'), item.get('source_url')),
        )
        self.conn.commit()
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
        text = unicodedata.normalize('NFKD', str(text).lower())
        text = re.sub(r'[^\w\s]', '', text)
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _date_only(dt_str: str) -> str:
        return str(dt_str)[:10] if dt_str else ''

    # ── URL tracking (used by ConditionalFetchMiddleware) ─────────────────────

    def url_seen(self, url: str) -> bool:
        return bool(self.conn.execute(
            'SELECT url FROM scraped_urls WHERE url = ?', (url,)
        ).fetchone())

    def mark_url(self, url: str, etag: str = None, last_modified: str = None):
        self.conn.execute(
            '''INSERT OR REPLACE INTO scraped_urls (url, etag, last_modified)
               VALUES (?, ?, ?)''',
            (url, etag, last_modified),
        )
        self.conn.commit()


# ── 300 · API submit ──────────────────────────────────────────────────────────

class APISubmitPipeline:
    """
    POST new events; PATCH recurring events whose date has changed.

    To PATCH, we need the backend event ID stored in the local dedup DB.
    The dedup pipeline passes it through item['backend_event_id'].

    After a successful POST of a new recurring event, we call back into the
    dedup pipeline to store the returned backend UUID for future PATCHes.
    """

    def open_spider(self, spider):
        api_base = spider.settings.get('HAPPS_API_BASE', '').rstrip('/')
        self.post_url  = f"{api_base}/events/scrape/"
        self.patch_url = f"{api_base}/events/scrape/{{id}}/"
        token = spider.settings.get('HAPPS_SCRAPER_TOKEN', '')
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Token {token}',
            'Content-Type': 'application/json',
        })
        if not token:
            logger.warning("HAPPS_SCRAPER_TOKEN not set — submissions will be rejected")

    def close_spider(self, spider):
        self.session.close()

    def process_item(self, item, spider):
        if item.get('is_recurring_update') and item.get('backend_event_id'):
            self._patch(item, spider)
        else:
            self._post(item, spider)
        return item

    def _post(self, item, spider):
        payload = self._build_payload(item)
        try:
            resp = self.session.post(self.post_url, json=payload, timeout=30)
            if resp.status_code == 201:
                backend_id = resp.json().get('id')
                logger.info("Created: '%s' (%s)", item.get('title'), backend_id)

                # For recurring events, persist the backend ID so future
                # scrape runs can PATCH instead of attempting a duplicate POST.
                if item.get('recurrence_freq', 'none') != 'none' and backend_id:
                    dedup = self._get_dedup(spider)
                    if dedup:
                        dedup.store_recurring_backend_id(
                            fingerprint=item.get('fingerprint'),
                            backend_id=backend_id,
                            title=item.get('title', ''),
                            source_url=item.get('source_url', ''),
                            start_datetime=item.get('start_datetime', ''),
                        )
            elif resp.status_code == 409:
                logger.debug("Already exists server-side: '%s'", item.get('title'))
            else:
                resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("HTTP %s posting '%s': %s",
                         exc.response.status_code, item.get('title'), exc)
        except requests.RequestException as exc:
            logger.error("Network error posting '%s': %s", item.get('title'), exc)

    def _patch(self, item, spider):
        url = self.patch_url.format(id=item['backend_event_id'])
        payload = {
            'start_datetime': item.get('start_datetime'),
            'end_datetime':   item.get('end_datetime'),
        }
        try:
            resp = self.session.patch(url, json=payload, timeout=30)
            if resp.status_code == 200:
                logger.info("Updated occurrence: '%s' → %s",
                            item.get('title'), item.get('start_datetime'))
            else:
                resp.raise_for_status()
        except requests.HTTPError as exc:
            logger.error("HTTP %s patching '%s': %s",
                         exc.response.status_code, item.get('title'), exc)
        except requests.RequestException as exc:
            logger.error("Network error patching '%s': %s", item.get('title'), exc)

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
            'source_url':           item.get('source_url'),
            'fingerprint':          item.get('fingerprint'),
            'extraction_method':    item.get('extraction_method'),
            'recurrence_freq':      item.get('recurrence_freq', 'none'),
            'recurrence_interval':  item.get('recurrence_interval', 1),
            'recurrence_byday':     item.get('recurrence_byday', []),
            'recurrence_month_mode': item.get('recurrence_month_mode', 'day'),
            'recurrence_until':     item.get('recurrence_until'),
            'recurrence_count':     item.get('recurrence_count'),
            'rdates':               item.get('rdates', []),
            'exdates':              item.get('exdates', []),
        }

    @staticmethod
    def _get_dedup(spider) -> Optional['FingerprintDedupPipeline']:
        try:
            for mw in spider.crawler.engine.scraper.itemproc.middlewares:
                if isinstance(mw, FingerprintDedupPipeline):
                    return mw
        except Exception:
            pass
        return None
