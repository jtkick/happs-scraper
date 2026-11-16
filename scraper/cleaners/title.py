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
from urllib.parse import urlparse

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


# A separator between a title and what a site appends to it; a hyphen only with spaces ("Hip-Hop" stays).
_SUFFIX_SEP = re.compile(r'\s+-{1,2}\s+|\s*[|—–·]\s*')
_CALL_TO_ACTION = re.compile(
    r'^(?:(?:book|buy|get|reserve|register|rsvp|order|purchase)\s+(?:your|now|today|tickets?|here|a|an)\b|'
    r'official\s+(?:ticket\s+source|tickets?|site|website)$|tickets?$)', re.IGNORECASE)
_MONTH = (r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|'
          r'sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?')
_DATE_ONLY = re.compile(
    r'^(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?(?:' + _MONTH + r'\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|'
    r'\d{1,2}(?:st|nd|rd|th)?\s+' + _MONTH + r'(?:,?\s+\d{4})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)$', re.IGNORECASE)


def _letters(text: str) -> str:
    return re.sub(r'[^a-z0-9]', '', text.casefold())


def _site_names(ctx: dict) -> set[str]:
    """The site's name as letters, from its domains ("myfountainsquare" for myfountainsquare.com)."""
    names = set()
    for key in ('url', 'source_url'):
        host = (urlparse(ctx.get(key) or '').hostname or '').removeprefix('www.')
        if host:
            names.add(_letters(host.rsplit('.', 1)[0].rsplit('.', 1)[-1]))
    return {n for n in names if len(n) >= 4}


def _site_suffix(segment: str, sites: set[str]) -> bool:
    letters = _letters(segment)
    if _DATE_ONLY.match(segment.strip()) or _CALL_TO_ACTION.match(segment.strip()):
        return True
    return len(letters) >= 4 and any(letters in site or site in letters for site in sites)


def strip_site_suffix(title: str, ctx: dict) -> str:
    """
    Remove what the site appends to every page's title: its own name ("Jazz at Winterhaus | Fountain
    Square" on myfountainsquare.com), the date ("Murder Mystery Show | February 26th") or a call to action
    ("MIRACLE | Book Your Experience Today"). The first segment always stays.
    """
    sites = _site_names(ctx)
    while True:
        seps = list(_SUFFIX_SEP.finditer(title))
        if not seps:
            return title
        last = seps[-1]
        if not _site_suffix(title[last.end():], sites) or not title[:last.start()].strip():
            return title
        title = title[:last.start()].rstrip()


def strip_trailing_punctuation(title: str, ctx: dict) -> str:
    """Remove orphaned separators or punctuation left at the end of a title."""
    return _TRAILING_SEP.sub('', title).strip()
