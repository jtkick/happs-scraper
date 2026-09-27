"""
Keyword-based tag matcher with venue-context inference.

Two signal sources:
  1. Keyword matching — scans title + description for known terms.
  2. Context inference — uses venue_type (OSM amenity) and location_title
     to assign tags that wouldn't appear in the event text itself (e.g.
     all events at a bar get "21+").

Tag names must match the backend Tag fixture exactly (see seed.py REQUIRED_TAGS).
"""

from __future__ import annotations
import re

# ── Tag definitions ────────────────────────────────────────────────────────────
# Each entry: (tag_name, [keyword_terms]).
# Use the most specific terms first; word-boundary matching is applied to all.

TAG_DEFINITIONS: list[tuple[str, list[str]]] = [
    ("Music",           ["live music", "concert", "band", "dj set", "dj night",
                         "open mic", "acoustic", "jazz night", "blues night",
                         "music", "dj"]),
    ("Comedy",          ["stand-up comedy", "standup comedy", "comedy night",
                         "comedy show", "comedian", "improv comedy", "improv",
                         "comedy"]),
    ("Karaoke",         ["karaoke night", "karaoke bar", "karaoke"]),
    ("Trivia",          ["trivia night", "pub quiz", "bar trivia", "game night",
                         "quiz night", "trivia"]),
    ("Happy Hour",      ["happy hour", "drink specials", "discounted drinks",
                         "half-price drinks", "bar specials"]),
    ("Dancing",         ["dance party", "club night", "dance floor", "dancing",
                         "salsa night", "swing dancing", "bachata"]),
    ("Drag",            ["drag show", "drag queen", "drag king", "drag night",
                         "rupaul", "drag"]),
    ("Fundraiser",      ["fundraiser", "charity event", "benefit concert",
                         "benefit show", "raise money", "donation event",
                         "nonprofit event", "charity"]),
    ("21+",             ["21+", "21 and up", "21 & up", "must be 21",
                         "ages 21", "over 21"]),
    ("Open Turntable",  ["open turntable", "open decks", "vinyl night",
                         "bring your records", "dj open decks"]),
    ("Bingo",           ["bingo night", "drag bingo", "musical bingo", "bingo"]),
    ("Volunteering",    ["volunteer opportunity", "community service",
                         "service project", "volunteering", "volunteer"]),
    ("Pop-Up",          ["pop-up", "pop up", "popup",
                         "limited time", "one night only"]),
]

def _compile(term: str) -> re.Pattern:
    # \b after a non-word character (like '+') never matches, so omit the
    # trailing boundary for terms that end with one.
    prefix = r'\b'
    suffix = r'\b' if term[-1].isalnum() or term[-1] == '_' else r'(?=\s|$)'
    return re.compile(prefix + re.escape(term) + suffix, re.IGNORECASE)


_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    (name, [_compile(term) for term in terms])
    for name, terms in TAG_DEFINITIONS
]

# ── Venue-type → tags inference ────────────────────────────────────────────────
# Maps OSM amenity/tourism/leisure values to tags that apply to all events there.
# Values are lower-cased before lookup.

_VENUE_TYPE_TAGS: dict[str, list[str]] = {
    'bar':           ['21+', 'Happy Hour'],
    'pub':           ['21+', 'Happy Hour'],
    'nightclub':     ['21+', 'Dancing'],
    'cocktail_bar':  ['21+'],
    'wine_bar':      ['21+'],
    'brewery':       ['21+'],
    'biergarten':    ['21+'],
    'social_club':   ['21+'],
}

# Keywords in the venue *name* (location_title) that imply tags.
# Checked as substrings, lower-cased.
_VENUE_NAME_TAGS: list[tuple[str, str]] = [
    # (substring, tag)
    ('bar',         '21+'),
    ('pub',         '21+'),
    ('brewery',     '21+'),
    ('taproom',     '21+'),
    (' tap',        '21+'),     # "The Rusty Tap", "East Side Tap"
    ('tavern',      '21+'),
    ('saloon',      '21+'),
    ('lounge',      '21+'),
    ('speakeasy',   '21+'),
    ('nightclub',   '21+'),
    ('club',        '21+'),
    ('comedy',      'Comedy'),
]


# ── Public API ────────────────────────────────────────────────────────────────

def match(title: str, description: str = '', ctx: dict | None = None,
          max_tags: int = 4) -> list[str]:
    """
    Return up to `max_tags` tag names for an event.

    Keyword matching runs first; context inference fills remaining slots.
    ctx should be the full item dict (or request context) and may contain:
      venue_type      — OSM amenity value, e.g. 'bar', 'nightclub'
      location_title  — venue name, e.g. 'Uncle Leo's Bar'
    """
    haystack = f"{title} {description}"
    matched: list[str] = []

    for tag_name, patterns in _PATTERNS:
        if len(matched) >= max_tags:
            break
        for pat in patterns:
            if pat.search(haystack):
                matched.append(tag_name)
                break

    if ctx and len(matched) < max_tags:
        for tag in _infer_from_context(ctx):
            if tag not in matched:
                matched.append(tag)
            if len(matched) >= max_tags:
                break

    return matched


# ── Internal ──────────────────────────────────────────────────────────────────

def _infer_from_context(ctx: dict) -> list[str]:
    """Return tags implied by venue type / venue name, without duplicating matched ones."""
    tags: list[str] = []
    seen: set[str] = set()

    def _add(tag: str):
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)

    venue_type = (ctx.get('venue_type') or '').lower().strip()
    if venue_type and venue_type in _VENUE_TYPE_TAGS:
        for t in _VENUE_TYPE_TAGS[venue_type]:
            _add(t)

    venue_name = (ctx.get('location_title') or '').lower()
    if venue_name:
        for substring, tag in _VENUE_NAME_TAGS:
            if substring in venue_name:
                _add(tag)

    return tags
