"""Squarespace events collections — `?format=json` returns the collection as JSON."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse, parse_qsl, urlunparse

from scraper.util import strip_tags

from .base import Platform

class Squarespace(Platform):
    name = 'squarespace'

    def detect(self, response) -> bool:
        text = response.text
        # Only an events *collection* page has a JSON feed; a Squarespace
        # homepage falls through to normal discovery, then matches here.
        return 'squarespace' in text.lower() and ('eventlist' in text or 'events-collection' in text)

    def feed_urls(self, response) -> list[str]:
        return [_with_query(response.url, format='json')]

    def parse(self, response) -> Optional[list[dict]]:
        try:
            data = json.loads(response.text)
        except ValueError:
            return []
        items = data.get('upcoming')
        if items is None:
            items = data.get('items') or []
        return [self._event(e, response) for e in items if e.get('title')]

    @staticmethod
    def _event(e: dict, response) -> dict:
        loc = e.get('location') or {}
        address = ', '.join(filter(None, (loc.get('addressLine1'), loc.get('addressLine2'))))
        lat, lon = loc.get('mapLat') or loc.get('markerLat'), loc.get('mapLng') or loc.get('markerLng')
        return {
            'title':            e.get('title'),
            'description':      strip_tags(e.get('excerpt')) or strip_tags(e.get('body')),
            'start_datetime':   _ms(e.get('startDate')),
            'end_datetime':     _ms(e.get('endDate')),
            'url':              response.urljoin(e['fullUrl']) if e.get('fullUrl') else None,
            'image_url':        e.get('assetUrl'),
            'location_title':   loc.get('addressTitle') or None,
            'location_address': address or None,
            # Squarespace's default map pin sits at its NYC office — ignore it.
            'location_lat':     lat if lat and address else None,
            'location_lon':     lon if lon and address else None,
        }


def _ms(value) -> Optional[str]:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()



def _with_query(url: str, **params) -> str:
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    query.update(params)
    return urlunparse(parts._replace(query=urlencode(query)))
