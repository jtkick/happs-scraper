"""
Pages whose content only exists after JavaScript runs. These adapters add
nothing but "render this page in a browser" (scrapy-playwright); the spider
re-requests the same page rendered and continues normally.
"""
from __future__ import annotations
import re

from .base import Platform

_SPA_ROOT = re.compile(r'<div[^>]+id=["\'](?:root|app|__next|__nuxt)["\'][^>]*>\s*</div>', re.IGNORECASE)
MIN_TEXT = 300


class Wix(Platform):
    name = 'wix'
    render_js = True
    rerender_only = True

    def detect(self, response) -> bool:
        generator = (response.css('meta[name="generator"]::attr(content)').get() or '').lower()
        return 'wix.com' in generator or 'static.wixstatic.com' in response.text[:20000]


class ScriptRenderedPage(Platform):
    """Generic SPA: an empty app root and almost no readable text."""
    name = 'script_rendered'
    render_js = True
    rerender_only = True

    def detect(self, response) -> bool:
        if not _SPA_ROOT.search(response.text):
            return False
        text = ' '.join(response.xpath('//body//text()[not(ancestor::script) and '
                                       'not(ancestor::style) and not(ancestor::noscript)]').getall())
        return len(re.sub(r'\s+', ' ', text).strip()) < MIN_TEXT
