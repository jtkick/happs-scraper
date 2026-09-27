"""
Extract schema.org Event objects from JSON-LD embedded in a page.

Many venue and ticketing sites emit perfectly structured data here, so this
extractor runs first and costs nothing.
"""

from __future__ import annotations
import logging
from typing import Optional

import extruct

logger = logging.getLogger(__name__)


def extract(html: str, base_url: str) -> Optional[dict]:
    """Return the first schema.org Event found in the page's JSON-LD, or None."""
    results = extract_all(html, base_url)
    return results[0] if results else None


def extract_all(html: str, base_url: str) -> list[dict]:
    """
    Return all schema.org Events found in the page's JSON-LD blocks.
    Handles @graph containers and pages with multiple Event nodes.
    """
    try:
        data = extruct.extract(
            html,
            base_url=base_url,
            syntaxes=['json-ld'],
            uniform=True,
        )
    except Exception as exc:
        logger.debug("extruct failed on %s: %s", base_url, exc)
        return []

    results = []
    for item in data.get('json-ld', []):
        for node in _find_all_events(item):
            parsed = _parse(node)
            if parsed.get('title') or parsed.get('start_datetime'):
                results.append(parsed)
    return results


def _find_all_events(node: dict) -> list[dict]:
    """Recursively collect all Event nodes, handling @graph containers."""
    if not isinstance(node, dict):
        return []
    node_type = node.get('@type', '')
    if isinstance(node_type, list):
        node_type = node_type[0] if node_type else ''
    results = []
    if 'Event' in str(node_type):
        results.append(node)
    for item in node.get('@graph', []):
        results.extend(_find_all_events(item))
    return results


def _find_event(node: dict) -> Optional[dict]:
    """Return the first Event node found. Used by BaseEventSpider for the recurrence extractor."""
    found = _find_all_events(node)
    return found[0] if found else None


def _text(value) -> Optional[str]:
    """Normalise a JSON-LD value that might be a string or {'@value': ...}."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return str(value.get('@value', '')).strip() or None
    return None


def _location(node: dict) -> tuple[Optional[str], Optional[str]]:
    loc = node.get('location') or node.get('Place') or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    name = _text(loc.get('name'))
    address = loc.get('address')
    if isinstance(address, dict):
        parts = filter(None, [
            _text(address.get('streetAddress')),
            _text(address.get('addressLocality')),
            _text(address.get('addressRegion')),
            _text(address.get('postalCode')),
        ])
        address = ', '.join(parts) or None
    else:
        address = _text(address)
    return name, address


def _price(node: dict) -> Optional[float]:
    offers = node.get('offers') or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    raw = offers.get('price') or offers.get('lowPrice')
    if raw is None:
        return None
    try:
        return float(str(raw).replace(',', ''))
    except (ValueError, TypeError):
        return None


def _ticket_url(node: dict) -> Optional[str]:
    offers = node.get('offers') or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return _text(offers.get('url'))


def _parse(node: dict) -> dict:
    loc_title, loc_address = _location(node)
    image = node.get('image')
    if isinstance(image, list):
        image = image[0]
    if isinstance(image, dict):
        image = image.get('url') or image.get('@id')

    return {
        'title':            _text(node.get('name')),
        'description':      _text(node.get('description')),
        'start_datetime':   _text(node.get('startDate')),
        'end_datetime':     _text(node.get('endDate')),
        'location_title':   loc_title,
        'location_address': loc_address,
        'ticket_price':     _price(node),
        'ticket_url':       _ticket_url(node),
        'url':              _text(node.get('url')),
        'image_url':        _text(image) if image else None,
    }
