"""
Platform adapter contract. One adapter covers every site built on that
platform, so adding a platform is one file plus one fixture test.

  detect(response)    → bool       is this page built on / embedding the platform?
  feed_urls(response) → [url]      where its machine-readable events live
  parse(response)     → [dict] | None
                                    events from a feed response; None means
                                    "this is a normal page — extract it generically"
  next_url(response)  → url | None  feed pagination

`render_js`: the feed pages need a browser (scrapy-playwright).
`rerender_only`: the adapter adds nothing but "render this page in a browser";
the spider re-requests the same page rendered and carries on as usual.
"""
from __future__ import annotations
from typing import Optional


class Platform:
    name = ''
    render_js = False
    rerender_only = False

    def detect(self, response) -> bool:
        raise NotImplementedError

    def feed_urls(self, response) -> list[str]:
        return [response.url]

    def parse(self, response) -> Optional[list[dict]]:
        return None

    def next_url(self, response) -> Optional[str]:
        return None


def is_html(response) -> bool:
    return b'html' in (response.headers.get('Content-Type') or b'text/html').lower()
