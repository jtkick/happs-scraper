"""
Extract event fields from OpenGraph and standard <meta> tags.

Runs after JSON-LD as a secondary source to fill in any gaps.
"""

from __future__ import annotations
import logging
from typing import Optional

import extruct

logger = logging.getLogger(__name__)


def extract(html: str, base_url: str) -> Optional[dict]:
    try:
        data = extruct.extract(
            html,
            base_url=base_url,
            syntaxes=['opengraph'],
            uniform=True,
        )
    except Exception as exc:
        logger.debug("extruct OG failed on %s: %s", base_url, exc)
        return None

    og = {}
    for item in data.get('opengraph', []):
        og.update(item)

    if not og:
        return None

    result: dict = {}

    title = og.get('og:title') or og.get('title')
    if title:
        result['title'] = title.strip()

    desc = og.get('og:description') or og.get('description')
    if desc:
        result['description'] = desc.strip()

    image = og.get('og:image') or og.get('og:image:url')
    if image:
        result['image_url'] = image.strip()

    url = og.get('og:url')
    if url:
        result['url'] = url.strip()

    # Some sites use event-specific OG namespaces
    start = og.get('event:start_time') or og.get('og:start_time')
    if start:
        result['start_datetime'] = start.strip()

    end = og.get('event:end_time') or og.get('og:end_time')
    if end:
        result['end_datetime'] = end.strip()

    loc = og.get('event:location') or og.get('og:location')
    if loc:
        result['location_title'] = loc.strip()

    return result or None
