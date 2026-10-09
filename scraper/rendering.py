"""
Rendering in headless Chromium (scrapy-playwright), when PLAYWRIGHT_ENABLED.

Every HTML page a spider fetches is rendered, so text JavaScript draws
(listing cards, schedules filled in from an API) is in the response.
Feeds (iCal, JSON) and sitemaps are fetched raw. A page whose render fails
is fetched once more raw (meta['render_failed']) rather than lost.
"""
from __future__ import annotations

# After the load event, how long to wait for XHR-filled content to settle.
SETTLE_MS = 5000
_HEAVY = frozenset({'image', 'media', 'font'})


def abort_request(request) -> bool:
    """PLAYWRIGHT_ABORT_REQUEST: skip what can't add text."""
    return request.resource_type in _HEAVY


async def _settle(page):
    """Wait for the network to go quiet, but don't fail a page over a site that never does."""
    try:
        await page.wait_for_load_state('networkidle', timeout=SETTLE_MS)
    except Exception:
        pass


def render_meta(settings) -> dict:
    """Request meta that renders the page, or {} when rendering is off."""
    if not settings.getbool('PLAYWRIGHT_ENABLED'):
        return {}
    meta = {'playwright': True}
    try:
        from scrapy_playwright.page import PageMethod
    except ImportError:
        return meta
    meta['playwright_page_methods'] = [PageMethod(_settle)]
    return meta


_CONDITIONAL_HEADERS = (b'If-None-Match', b'If-Modified-Since')


def rendered_after_check(request, settings):
    """
    The page again, rendered, after a raw conditional check came back changed
    (ConditionalFetchMiddleware); None when the request wasn't such a check.
    """
    if not request.meta.get('render_if_changed'):
        return None
    meta = {k: v for k, v in request.meta.items()
            if k not in ('render_if_changed', 'conditional', 'cacheable')}
    headers = {k: v for k, v in request.headers.items() if k not in _CONDITIONAL_HEADERS}
    return request.replace(meta={**meta, **render_meta(settings)}, headers=headers, dont_filter=True)


def raw_retry(request):
    """The same request without the browser, after a failed render; None if it wasn't rendered."""
    if not request.meta.get('playwright') or request.meta.get('render_failed'):
        return None
    meta = {k: v for k, v in request.meta.items() if not k.startswith('playwright')}
    return request.replace(meta={**meta, 'render_failed': True}, dont_filter=True)
