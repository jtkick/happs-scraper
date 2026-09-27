"""Unit tests for scraper/cleaners/."""
import pytest
from scraper.cleaners.title import strip_venue_suffix, strip_trailing_punctuation
from scraper.cleaners import clean_title


# ── strip_venue_suffix ────────────────────────────────────────────────────────

@pytest.mark.parametrize('title, venue, expected', [
    # em dash
    ('Jazz Night — Blues Alley',        'Blues Alley',   'Jazz Night'),
    # pipe
    ('Jazz Night | Blues Alley',        'Blues Alley',   'Jazz Night'),
    # hyphen
    ('Jazz Night - Blues Alley',        'Blues Alley',   'Jazz Night'),
    # slash
    ('Jazz Night / Blues Alley',        'Blues Alley',   'Jazz Night'),
    # double hyphen
    ('Jazz Night -- Blues Alley',       'Blues Alley',   'Jazz Night'),
    # case-insensitive
    ('Jazz Night — BLUES ALLEY',        'Blues Alley',   'Jazz Night'),
    # extra surrounding spaces
    ('Jazz Night  —  Blues Alley  ',    'Blues Alley',   'Jazz Night'),
    # no venue → unchanged
    ('Jazz Night',                      '',              'Jazz Night'),
    # venue not present → unchanged
    ('Jazz Night — Other Place',        'Blues Alley',   'Jazz Night — Other Place'),
    # fuzzy: venue word is substring of suffix
    ('The Night We Got Lost — Afterlife', 'Afterlife Bar', 'The Night We Got Lost'),
    # fuzzy: suffix is substring of venue name
    ('Concert — Alley',                 'Blues Alley',   'Concert'),
])
def test_strip_venue_suffix(title, venue, expected):
    ctx = {'location_title': venue}
    assert strip_venue_suffix(title, ctx) == expected


# ── strip_trailing_punctuation ────────────────────────────────────────────────

@pytest.mark.parametrize('title, expected', [
    ('Jazz Night —',   'Jazz Night'),
    ('Jazz Night |',   'Jazz Night'),
    ('Jazz Night -',   'Jazz Night'),
    ('Jazz Night ,',   'Jazz Night'),
    ('Jazz Night ;',   'Jazz Night'),
    ('Jazz Night :',   'Jazz Night'),
    ('Jazz Night',     'Jazz Night'),       # nothing to strip
    ('Jazz Night — ',  'Jazz Night'),       # trailing space after sep
])
def test_strip_trailing_punctuation(title, expected):
    assert strip_trailing_punctuation(title, {}) == expected


# ── clean_title (full pipeline) ───────────────────────────────────────────────

def test_clean_title_strips_venue_then_punctuation():
    # After venue strip, a trailing separator would be left — clean_title removes it too
    ctx = {'location_title': 'Blues Alley'}
    assert clean_title('Jazz Night — Blues Alley', ctx) == 'Jazz Night'


def test_clean_title_none_input():
    assert clean_title(None, {}) is None


def test_clean_title_preserves_when_nothing_to_strip():
    assert clean_title('Jazz Night', {'location_title': 'Blues Alley'}) == 'Jazz Night'
