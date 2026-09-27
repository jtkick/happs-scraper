"""Tests for scraper/extractors/recipe.py — applying and learning CSS recipes."""
import pytest
from scrapy.http import HtmlResponse

from scraper.extractors import ai, recipe

URL = 'https://venue.test/events'

LISTING = '''<html><head><script>var x = 1;</script></head><body>
  <nav><a href="/">Home</a></nav>
  <div class="event-card"><h3>Jazz Night</h3><time datetime="2026-06-05">Fri Jun 5</time>
    <span class="time">7pm</span><a href="/events/jazz">More</a><img src="/j.jpg"></div>
  <div class="event-card"><h3>Trivia</h3><time datetime="2026-06-06">Sat Jun 6</time>
    <span class="time">8pm</span><a href="/events/trivia">More</a></div>
  <div class="event-card"><h3>No Date Yet</h3></div>
</body></html>'''

RECIPE = {
    'item_css': 'div.event-card',
    'fields': {'title': 'h3', 'date': 'time::attr(datetime)', 'time': '.time',
               'link': 'a', 'image': 'img'},
}


def resp(body=LISTING):
    return HtmlResponse(URL, body=body.encode(), encoding='utf-8')


def test_extract_applies_fields():
    events = recipe.extract(resp(), RECIPE)
    assert [e['title'] for e in events] == ['Jazz Night', 'Trivia']
    assert events[0]['start_datetime'] == '2026-06-05 7pm'
    assert events[0]['url'] == 'https://venue.test/events/jazz'
    assert events[0]['image_url'] == 'https://venue.test/j.jpg'
    assert 'image_url' not in events[1]
    assert events[0]['extraction_method'] == 'recipe'


def test_bad_selector_does_not_crash():
    assert recipe.extract(resp(), {'item_css': 'div[[['}) == []


EXPECTED = [{'title': 'Jazz Night', 'start_datetime': '2026-06-05T19:00'},
            {'title': 'Trivia', 'start_datetime': '2026-06-06T20:00'}]


def test_agreement_matches_title_and_day():
    assert recipe.agreement(recipe.extract(resp(), RECIPE), EXPECTED) == 1.0
    wrong_day = [dict(EXPECTED[0], start_datetime='2026-07-01'), EXPECTED[1]]
    assert recipe.agreement(recipe.extract(resp(), RECIPE), wrong_day) == 0.5


def test_agreement_rejects_over_broad_recipes():
    found = [{'title': f'Item {i}', 'start_datetime': '2026-06-05'} for i in range(20)]
    assert recipe.agreement(found, EXPECTED) == 0.0


def test_skeleton_strips_scripts_and_long_text():
    body = LISTING.replace('Jazz Night', 'J' * 200)
    sk = recipe.skeleton(resp(body))
    assert '<script' not in sk and 'var x' not in sk
    assert 'event-card' in sk and 'J' * 61 not in sk


@pytest.mark.parametrize('suggested, accepted', [
    (dict(RECIPE, detail_link_css=None, pagination_css=None), True),
    ({'item_css': 'nav', 'fields': {'title': 'a', 'date': 'a'}}, False),
])
def test_learn_keeps_only_verified_recipes(monkeypatch, suggested, accepted):
    monkeypatch.setattr(ai, 'suggest_recipe', lambda *a, **k: suggested)
    learned = recipe.learn(resp(), EXPECTED, 'key')
    assert (learned is not None) is accepted
    if accepted:
        assert learned['item_css'] == 'div.event-card'
        assert 'detail_link_css' not in learned      # nulls dropped


def test_learn_needs_verified_ai_events(monkeypatch):
    monkeypatch.setattr(ai, 'suggest_recipe', lambda *a, **k: RECIPE)
    unverified = [dict(e, drop_reason='ai_unverified') for e in EXPECTED]
    assert recipe.learn(resp(), unverified, 'key') is None
    assert recipe.learn(resp(), EXPECTED, '') is None
