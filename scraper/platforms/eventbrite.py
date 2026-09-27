"""
Eventbrite: venue sites often just link out to their Eventbrite organizer
page (or to individual events). Follow those; Eventbrite pages carry
schema.org JSON-LD, so the generic extractor handles them.
"""
from __future__ import annotations
import re

from .base import Platform

_ORGANIZER = re.compile(r'https?://(?:www\.)?eventbrite\.[a-z.]+/o/[^"\'\s?#]+', re.IGNORECASE)
_EVENT = re.compile(r'https?://(?:www\.)?eventbrite\.[a-z.]+/e/[^"\'\s?#]+', re.IGNORECASE)
MAX_EVENT_LINKS = 20


class Eventbrite(Platform):
    name = 'eventbrite'

    def detect(self, response) -> bool:
        return bool(self.feed_urls(response))

    def feed_urls(self, response) -> list[str]:
        hrefs = response.css('a::attr(href)').getall()
        organizers = [m.group() for h in hrefs if (m := _ORGANIZER.match(h))]
        if organizers:
            return list(dict.fromkeys(organizers))[:2]
        events = [m.group() for h in hrefs if (m := _EVENT.match(h))]
        # One "buy tickets" link isn't a calendar; several are.
        return list(dict.fromkeys(events))[:MAX_EVENT_LINKS] if len(set(events)) >= 2 else []
