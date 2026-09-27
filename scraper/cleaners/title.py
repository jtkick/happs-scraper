"""
Title cleaning functions.

Each function has the signature:
    (title: str, ctx: dict) -> str

`ctx` is the full EventItem dict (after whitespace normalization of all other
fields), available for cross-field context.  Return the cleaned title; never
return an empty string — callers treat that as a failure and fall back to the
original value.

Add new functions here, then register them in scraper/cleaners/__init__.py.
"""
from __future__ import annotations
import re

# Separators that commonly join an event title to a venue/brand suffix
_SEP = re.compile(r'\s*(?:—|–|-{1,2}|/|\|)\s*')
_TRAILING_SEP = re.compile(r'[\s—–\-|/,;:]+$')


def strip_venue_suffix(title: str, ctx: dict) -> str:
    """
    Remove the venue name when it has been appended to the event title.

    Handles patterns like:
        "Jazz Night — Blues Alley"
        "Jazz Night | Blues Alley"
        "Jazz Night - Blues Alley"
        "Jazz Night / Blues Alley"

    Matching is case-insensitive.  Also does a fuzzy check so partial matches
    work (e.g. venue "Afterlife Bar" still strips "— Afterlife").
    """
    venue = (ctx.get('location_title') or '').strip()
    if not venue or not title:
        return title

    # Exact match: "Title [sep] Venue"
    exact = re.compile(
        r'\s*(?:—|–|-{1,2}|/|\|)\s*' + re.escape(venue) + r'\s*$',
        re.IGNORECASE,
    )
    cleaned = exact.sub('', title).strip()
    if cleaned and cleaned != title:
        return cleaned

    # Fuzzy fallback: check whether the last segment after any separator is a
    # substring of the venue name, or the venue name is a substring of it.
    segments = _SEP.split(title)
    if len(segments) > 1:
        last = segments[-1].strip().lower()
        vlow = venue.lower()
        if last and (last in vlow or vlow in last):
            sep_spans = list(_SEP.finditer(title))
            return title[:sep_spans[-1].start()].strip()

    return title


def strip_trailing_punctuation(title: str, ctx: dict) -> str:
    """Remove orphaned separators or punctuation left at the end of a title."""
    return _TRAILING_SEP.sub('', title).strip()
