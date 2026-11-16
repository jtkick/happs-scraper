"""WordPress + The Events Calendar (Tribe) — public REST API with every upcoming event."""
from __future__ import annotations
import json
import re
from html import unescape
from typing import Optional

from scraper.util import strip_tags

from .base import Platform

_MARKERS = ('/wp-content/plugins/the-events-calendar/', 'tribe-events', '/wp-json/tribe/events/')
class WordPressTribe(Platform):
    name = 'wordpress_tribe'

    def detect(self, response) -> bool:
        return any(m in response.text for m in _MARKERS)

    def feed_urls(self, response) -> list[str]:
        api_root = response.css('link[rel="https://api.w.org/"]::attr(href)').get() \
            or response.urljoin('/wp-json/')
        return [api_root.rstrip('/') + '/tribe/events/v1/events?per_page=50&start_date=now']

    def parse(self, response) -> Optional[list[dict]]:
        try:
            data = json.loads(response.text)
        except ValueError:
            return []
        return [self._event(e) for e in data.get('events', []) if e.get('title')]

    def next_url(self, response) -> Optional[str]:
        try:
            return json.loads(response.text).get('next_rest_url')
        except ValueError:
            return None

    @staticmethod
    def _event(e: dict) -> dict:
        venue = e.get('venue') if isinstance(e.get('venue'), dict) else {}
        address = ', '.join(filter(None, (venue.get(k) for k in ('address', 'city', 'state', 'zip'))))
        image = e.get('image') if isinstance(e.get('image'), dict) else {}
        values = (e.get('cost_details') or {}).get('values') or []
        # A WordPress manual offset ("UTC-4") has no clock changes, so its UTC fields are an hour off half
        # the year: then the local fields are read in the venue's zone instead.
        fixed = bool(_FIXED_OFFSET.match(e.get('timezone') or ''))
        # An all-day event's times are midnight to 23:59:59, often in UTC: it's the day, not a time.
        day = e.get('all_day') is True
        return {
            'title':            strip_tags(e.get('title')),
            'description':      strip_tags(e.get('description')),
            # UTC fields avoid depending on the site's configured zone.
            'start_datetime':   _day(e.get('start_date')) if day else
                                (not fixed and _utc(e.get('utc_start_date'))) or _local(e.get('start_date')),
            'end_datetime':     _day(e.get('end_date')) if day else
                                (not fixed and _utc(e.get('utc_end_date'))) or _local(e.get('end_date')),
            'url':              unescape(e['url']) if e.get('url') else None,
            'image_url':        image.get('url'),
            'ticket_price':     values[0] if values else (e.get('cost') or None),
            'ticket_url':       unescape(e['website']) if e.get('website') else None,
            'location_title':   strip_tags(venue.get('venue')),
            'location_address': address or None,
            'location_lat':     _float(venue.get('geo_lat')),
            'location_lon':     _float(venue.get('geo_lng')),
            'timezone':         None if fixed else e.get('timezone') or None,
        }


_FIXED_OFFSET = re.compile(r'^UTC(?:[+-]\d{1,2}(?::?\d{2})?)?$')


def _day(value) -> Optional[str]:
    return value[:10] if value else None


def _local(value) -> Optional[str]:
    return value.replace(' ', 'T') if value else None


def _utc(value) -> Optional[str]:
    return f"{value.replace(' ', 'T')}+00:00" if value else None



def _float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
