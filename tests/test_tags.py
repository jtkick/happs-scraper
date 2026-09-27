"""Tests for the tag matcher — keyword matching and context inference."""
import pytest
from scraper.extractors.tags import match, _infer_from_context


# ── Keyword matching ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('title, description, expected_tags', [
    # Core keyword hits
    ('Friday Jazz Night',          'Live music and drinks',     ['Music']),
    ('Stand-Up Comedy Showcase',   '',                          ['Comedy']),
    ('Karaoke Night',              '',                          ['Karaoke']),
    ('Trivia Night',               '',                          ['Trivia']),
    ('Pub Quiz',                   '',                          ['Trivia']),
    ('Happy Hour Specials',        '',                          ['Happy Hour']),
    ('Dance Party',                '',                          ['Dancing']),
    ('Drag Show',                  '',                          ['Drag']),
    ('Charity Fundraiser',         '',                          ['Fundraiser']),
    ('21+ Event',                  '',                          ['21+']),
    ('Vinyl Night',                'Open turntable all night',  ['Open Turntable']),
    ('Bingo Night',                '',                          ['Bingo']),
    ('Volunteer Day',              'Community service project', ['Volunteering']),
    ('Pop-Up Market',              '',                          ['Pop-Up']),
    # Multi-tag
    ('DJ Night & Dancing',         'Dance floor open 10pm',    ['Music', 'Dancing']),
    # No match
    ('Private Event',              'Details TBD',              []),
])
def test_keyword_matching(title, description, expected_tags):
    result = match(title, description)
    for tag in expected_tags:
        assert tag in result, f"Expected {tag!r} in result {result!r}"


# ── Context inference — venue_type ────────────────────────────────────────────

@pytest.mark.parametrize('venue_type, expected_tags', [
    ('bar',         ['21+', 'Happy Hour']),
    ('pub',         ['21+', 'Happy Hour']),
    ('nightclub',   ['21+', 'Dancing']),
    ('brewery',     ['21+']),
    ('cocktail_bar', ['21+']),
    ('restaurant',  []),          # no inference for restaurants
    ('theatre',     []),
])
def test_venue_type_inference(venue_type, expected_tags):
    result = _infer_from_context({'venue_type': venue_type})
    for tag in expected_tags:
        assert tag in result, f"Expected {tag!r} for venue_type={venue_type!r}, got {result!r}"


# ── Context inference — venue name ────────────────────────────────────────────

@pytest.mark.parametrize('location_title, expected_tag', [
    ("Uncle Leo's Bar",     '21+'),
    ('The Rusty Tap',       '21+'),
    ('Riverside Brewery',   '21+'),
    ('The Velvet Lounge',   '21+'),
    ('Old Town Tavern',     '21+'),
    ('Laugh Factory Comedy Club', 'Comedy'),
    ('City Auditorium',     None),   # no inference
])
def test_venue_name_inference(location_title, expected_tag):
    result = _infer_from_context({'location_title': location_title})
    if expected_tag:
        assert expected_tag in result, f"Expected {expected_tag!r} for {location_title!r}, got {result!r}"
    else:
        assert result == [], f"Expected no tags for {location_title!r}, got {result!r}"


# ── Full match() with ctx ─────────────────────────────────────────────────────

def test_ctx_adds_21_plus_for_bar_venue():
    result = match('Friday Night Special', ctx={'venue_type': 'bar'})
    assert '21+' in result

def test_ctx_does_not_duplicate_keyword_tag():
    # "21+" already matched by keyword — context inference shouldn't add it twice
    result = match('21+ Event Tonight', ctx={'venue_type': 'bar'})
    assert result.count('21+') == 1

def test_max_tags_respected():
    # A description with many keywords should still cap at max_tags
    result = match(
        'Trivia Karaoke Dancing Fundraiser',
        '21+ comedy bingo open turntable',
        max_tags=4,
    )
    assert len(result) <= 4

def test_no_ctx_still_works():
    result = match('Trivia Night', 'Come test your knowledge')
    assert 'Trivia' in result
