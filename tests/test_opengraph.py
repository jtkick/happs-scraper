"""Tests for the OpenGraph extractor — the gap-filler after JSON-LD."""
import pytest

from scraper.extractors import opengraph

URL = 'https://x.test/comedy'


def _page(*meta: str) -> str:
    return '<html><head>' + ''.join(meta) + '</head><body></body></html>'


def _og(prop: str, content: str) -> str:
    return f'<meta property="{prop}" content="{content}"/>'


FULL = _page(
    _og('og:title', 'Comedy Showcase'),
    _og('og:description', 'Laughs all night'),
    _og('og:image', 'https://img.test/c.jpg'),
    _og('og:url', 'https://x.test/comedy'),
    _og('og:start_time', '2026-08-01T20:00:00-04:00'),
    _og('og:end_time', '2026-08-01T23:00:00-04:00'),
    _og('og:location', 'The Laugh Box'),
)


@pytest.mark.parametrize('field, expected', [
    ('title',          'Comedy Showcase'),
    ('description',    'Laughs all night'),
    ('image_url',      'https://img.test/c.jpg'),
    ('url',            'https://x.test/comedy'),
    ('start_datetime', '2026-08-01T20:00:00-04:00'),
    ('end_datetime',   '2026-08-01T23:00:00-04:00'),
    ('location_title', 'The Laugh Box'),
])
def test_full_meta_fields(field, expected):
    assert opengraph.extract(FULL, URL)[field] == expected


def test_values_are_whitespace_stripped():
    html = _page(_og('og:title', '   Spaced Out   '))
    assert opengraph.extract(html, URL)['title'] == 'Spaced Out'


def test_partial_page_omits_missing_fields():
    result = opengraph.extract(_page(_og('og:title', 'Only A Title')), URL)
    assert result == {'title': 'Only A Title'}


@pytest.mark.parametrize('html', [
    '<html><head></head><body>nothing</body></html>',
    _page('<meta name="author" content="Someone"/>'),
], ids=['no-meta', 'unrelated-meta'])
def test_returns_none_without_opengraph(html):
    assert opengraph.extract(html, URL) is None


@pytest.mark.xfail(
    strict=True,
    reason="extruct's opengraph syntax only keeps og:* properties, so the "
           "event:start_time / event:end_time / event:location branches in "
           "opengraph.extract are unreachable. Facebook-style event pages use "
           "exactly those tags.",
)
def test_facebook_event_namespace_is_read():
    html = _page(
        _og('og:title', 'Block Party'),
        _og('event:start_time', '2026-08-01T20:00:00-04:00'),
    )
    assert opengraph.extract(html, URL)['start_datetime'] == '2026-08-01T20:00:00-04:00'
