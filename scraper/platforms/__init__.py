"""
Platform adapters — see base.py for the contract.

Order matters: the first adapter whose detect() matches wins, so feeds
with full structured data come before "just render it" adapters.
"""
from __future__ import annotations
from typing import Optional

from .base import Platform, is_html
from .ical import ICalFeed
from .wordpress_tribe import WordPressTribe
from .squarespace import Squarespace
from .eventbrite import Eventbrite
from .widgets import Tockify, Timely
from .spa import Wix, ScriptRenderedPage

ADAPTERS: list[Platform] = [
    WordPressTribe(),
    Squarespace(),
    ICalFeed(),
    Eventbrite(),
    Tockify(),
    Timely(),
    Wix(),
    ScriptRenderedPage(),
]
_BY_NAME = {a.name: a for a in ADAPTERS}


def detect(response) -> Optional[Platform]:
    if not is_html(response):
        return None
    for adapter in ADAPTERS:
        try:
            if adapter.detect(response):
                return adapter
        except Exception:
            continue
    return None


def get(name: Optional[str]) -> Optional[Platform]:
    return _BY_NAME.get(name) if name else None
