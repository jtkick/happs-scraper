"""
Data cleaning applied by NormalizePipeline after basic whitespace normalization.

To add a new title cleaner:
  1. Write a function in title.py with signature (title: str, ctx: dict) -> str.
  2. Append it to TITLE_CLEANERS below.

Functions are applied left-to-right; each receives the output of the previous.
ctx is the full EventItem dict with all other fields already normalized.
"""
from __future__ import annotations
from .title import strip_venue_suffix, strip_trailing_punctuation

TITLE_CLEANERS: list = [
    strip_venue_suffix,
    strip_trailing_punctuation,
]


def clean_title(raw: str | None, ctx: dict) -> str | None:
    """Apply every registered title cleaner in sequence."""
    result = raw
    for fn in TITLE_CLEANERS:
        if not result:
            break
        result = fn(result, ctx)
    return result or None
