"""Tests for scraper/discovery/sitemap.py — reading sitemaps for lastmod."""
import gzip
from datetime import datetime, timezone

from scraper.discovery import sitemap

URLSET = b'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://x.test/event/a/1/</loc><lastmod>2026-10-03T10:46:15Z</lastmod></url>
<url><loc> https://x.test/event/b/2/ </loc><lastmod>2026-10-01</lastmod></url>
<url><loc>https://x.test/about/</loc></url>
</urlset>'''

INDEX = b'''<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://x.test/sitemap-pages.xml</loc></sitemap>
<sitemap><loc>https://x.test/sitemap-events.xml</loc></sitemap>
</sitemapindex>'''


def test_urlset_entries_and_lastmod_formats():
    kind, entries = sitemap.parse(URLSET)
    assert kind == 'urlset'
    assert entries == [
        ('https://x.test/event/a/1/', datetime(2026, 10, 3, 10, 46, 15, tzinfo=timezone.utc)),
        ('https://x.test/event/b/2/', datetime(2026, 10, 1, tzinfo=timezone.utc)),
        ('https://x.test/about/', None),
    ]


def test_gzipped_sitemap():
    assert sitemap.parse(gzip.compress(URLSET))[0] == 'urlset'


def test_index_children_put_event_sitemaps_first():
    kind, entries = sitemap.parse(INDEX)
    assert kind == 'sitemapindex'
    assert sitemap.pick_children([loc for loc, _ in entries]) == [
        'https://x.test/sitemap-events.xml', 'https://x.test/sitemap-pages.xml']
    assert sitemap.pick_children([loc for loc, _ in entries], limit=1) == ['https://x.test/sitemap-events.xml']


def test_not_a_sitemap():
    assert sitemap.parse(b'<html><body>Not found</body></html>') == ('', [])
    assert sitemap.parse(b'') == ('', [])
    assert sitemap.parse(b'\x1f\x8bbroken') == ('', [])


def test_roots_come_from_robots_or_default():
    robots = b'User-agent: *\nDisallow: /admin\nSitemap: https://x.test/sm.xml\nSitemap: https://x.test/sm.xml\n'
    assert sitemap.roots(robots, 'https://x.test/robots.txt') == ['https://x.test/sm.xml']
    assert sitemap.roots(b'User-agent: *', 'https://x.test/robots.txt') == ['https://x.test/sitemap.xml']


def test_keys_match_across_spellings():
    assert sitemap.key('https://x.test/event/2026-biennial%3a-the-long-view/19581/') == \
        sitemap.key('https://x.test/event/2026-biennial%3A-the-long-view/19581')
