"""Tests for scraper/text.py — all of a page's text, visible and embedded."""
from scraper.text import full_text, visible_text


def test_visible_text_puts_blocks_on_lines_and_drops_code():
    page = '<html><head><title>T</title><style>p{}</style></head><body><h1>Jazz</h1>' \
           '<ul><li>Fri</li><li>Recurring <b>weekly</b></li></ul><script>var x = 1;</script>' \
           '<noscript>Enable JS</noscript><!-- note --></body></html>'
    assert visible_text(page) == 'Jazz\nFri\nRecurring weekly'


def test_embedded_json_text_is_added_once():
    page = '<html><body><h1>Long View</h1><script>var data = {"title": "Long View", ' \
           '"recurrence": "Recurring daily", "description": "<p>An exhibition &amp; more</p>", ' \
           '"url": "https://x.test/a b", "id": "abc", "again": "Recurring daily"}</script></body></html>'
    text = full_text(page)
    assert text.splitlines() == ['Long View', 'Recurring daily', 'An exhibition & more']


def test_json_ld_is_not_repeated():
    page = '<html><body><p>Hi there</p><script type="application/ld+json">{"name": "Some Event"}' \
           '</script></body></html>'
    assert full_text(page) == 'Hi there'


def test_unparseable_html_still_gives_text():
    assert full_text('') == ''
    assert 'hello world' in full_text('<p>hello world')
