"""
iCalendar feeds: <link type="text/calendar">, webcal:/.ics subscribe links,
and embedded Google Calendars (which all have a public ICS export).

The best source there is — exact times with zones, and RRULE recurrence
that maps directly onto the backend's recurrence fields.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from .base import Platform

MAX_EVENTS = 500
# More distinct .ics links than this on one page are per-event "add to
# calendar" buttons, not a calendar feed.
MAX_FEED_LINKS = 2


class ICalFeed(Platform):
    name = 'ical'

    def detect(self, response) -> bool:
        return bool(self.feed_urls(response))

    def feed_urls(self, response) -> list[str]:
        urls = [response.urljoin(h) for h in
                response.css('link[type="text/calendar"]::attr(href)').getall()]
        for src in response.css('iframe::attr(src)').getall():
            urls += _google_feeds(response.urljoin(src))
        subscribe = []
        for href in response.css('a::attr(href)').getall():
            href = href.strip()
            if href.startswith('webcal:'):
                subscribe.append('https:' + href[len('webcal:'):])
            elif urlparse(href).path.lower().endswith('.ics'):
                subscribe.append(response.urljoin(href))
        subscribe = list(dict.fromkeys(subscribe))
        if len(subscribe) <= MAX_FEED_LINKS:
            urls += subscribe
        return list(dict.fromkeys(urls))

    def parse(self, response) -> Optional[list[dict]]:
        try:
            from icalendar import Calendar
            cal = Calendar.from_ical(response.body)
        except Exception:
            return []
        now = datetime.now(timezone.utc)
        events = []
        for comp in cal.walk('VEVENT'):
            if comp.get('RECURRENCE-ID') is not None:
                continue      # a moved instance of a series; the series itself is enough
            if str(comp.get('STATUS', '')).upper() == 'CANCELLED':
                continue
            event = _event(comp)
            if event and _upcoming(event, comp, now):
                events.append(event)
            if len(events) >= MAX_EVENTS:
                break
        return events


def _google_feeds(src_url: str) -> list[str]:
    parsed = urlparse(src_url)
    if 'calendar.google.com' not in parsed.netloc or '/embed' not in parsed.path:
        return []
    return [f'https://calendar.google.com/calendar/ical/{quote(cal, safe="")}/public/basic.ics'
            for cal in parse_qs(parsed.query).get('src', [])]


def _event(comp) -> Optional[dict]:
    title = str(comp.get('SUMMARY') or '').strip()
    start = _dt(comp.get('DTSTART'))
    if not title or not start:
        return None
    event = {
        'title':            title,
        'description':      str(comp.get('DESCRIPTION') or '').strip() or None,
        'start_datetime':   start,
        'end_datetime':     _dt(comp.get('DTEND')),
        'location_title':   str(comp.get('LOCATION') or '').strip() or None,
        'url':              str(comp.get('URL') or '').strip() or None,
        'extraction_method': 'ical',
    }
    rrule = comp.get('RRULE')
    if rrule:
        event.update(_recurrence(rrule))
    exdates = [_dt_value(d.dt) for ex in _as_list(comp.get('EXDATE')) for d in ex.dts]
    if exdates:
        event['exdates'] = [{'datetime': d, 'reason': ''} for d in exdates if d]
    rdates = [_dt_value(d.dt) for rd in _as_list(comp.get('RDATE')) for d in rd.dts]
    if rdates:
        event['rdates'] = [d for d in rdates if d]
    return event


def _recurrence(rrule) -> dict:
    freq = (rrule.get('FREQ') or [''])[0].lower()
    if freq not in ('daily', 'weekly', 'monthly', 'yearly'):
        return {}
    byday = [str(d).upper() for d in rrule.get('BYDAY') or []]
    until = (rrule.get('UNTIL') or [None])[0]
    out = {
        'recurrence_freq':     freq,
        'recurrence_interval': int((rrule.get('INTERVAL') or [1])[0]),
        'recurrence_byday':    byday,
        'recurrence_until':    (until.date() if isinstance(until, datetime) else until).isoformat()
                               if until else None,
        'recurrence_count':    int((rrule.get('COUNT') or [0])[0]) or None,
    }
    if freq == 'monthly':
        out['recurrence_month_mode'] = 'weekday' if any(d[:-2] for d in byday) else 'day'
    return out


def _upcoming(event: dict, comp, now: datetime) -> bool:
    if event.get('recurrence_freq'):
        until = event.get('recurrence_until')
        return not until or until >= now.date().isoformat()
    last = _as_utc(comp.get('DTEND')) or _as_utc(comp.get('DTSTART'))
    return last is None or last >= now - timedelta(days=1)


def _dt(prop) -> Optional[str]:
    return _dt_value(prop.dt) if prop is not None else None


def _dt_value(value) -> Optional[str]:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return None


def _as_utc(prop) -> Optional[datetime]:
    if prop is None:
        return None
    value = prop.dt
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
