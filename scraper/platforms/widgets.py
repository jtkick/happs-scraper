"""
Hosted calendar widgets embedded via iframe/script. Their own public
calendar pages are rendered client-side, so the adapter points the spider
at that page, rendered, and lets generic extraction (JSON-LD / AI) read it.
"""
from __future__ import annotations
import re

from .base import Platform


class Tockify(Platform):
    name = 'tockify'
    render_js = True

    _CAL = re.compile(r'tockify\.com/(?:[a-z]+/)*?([A-Za-z0-9_-]+)(?:["\'/?]|$)')

    def detect(self, response) -> bool:
        return bool(self.feed_urls(response))

    def feed_urls(self, response) -> list[str]:
        names = response.css('[data-tockify-calendar]::attr(data-tockify-calendar)').getall()
        for src in response.css('iframe::attr(src)').getall():
            if 'tockify.com' in src and (m := self._CAL.search(src)):
                names.append(m.group(1))
        names = [n for n in dict.fromkeys(names) if n not in ('embed', 'api', 'tkf2', 'cdn')]
        return [f'https://tockify.com/{n}' for n in names][:2]


class Timely(Platform):
    name = 'timely'
    render_js = True

    def detect(self, response) -> bool:
        return bool(self.feed_urls(response))

    def feed_urls(self, response) -> list[str]:
        urls = [response.urljoin(src) for src in response.css('iframe::attr(src)').getall()
                if 'time.ly' in src or 'timely.fun' in src]
        return list(dict.fromkeys(urls))[:2]
