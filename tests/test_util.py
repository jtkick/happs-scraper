"""Tests for scraper/util.py — shared time and text helpers."""
from datetime import datetime, timezone

from scraper import util


def test_parse_utc_reads_dates_zulu_and_naive_times_as_utc():
    assert util.parse_utc('2026-10-03') == datetime(2026, 10, 3, tzinfo=timezone.utc)
    assert util.parse_utc('2026-10-03T10:46:15Z') == datetime(2026, 10, 3, 10, 46, 15, tzinfo=timezone.utc)
    assert util.parse_utc('soon') is None and util.parse_utc(None) is None


def test_parse_iso_keeps_naive_times_naive():
    assert util.parse_iso('2026-10-03T19:00').tzinfo is None
    assert util.parse_iso(datetime(2026, 10, 3)) == datetime(2026, 10, 3)


def test_strip_tags():
    assert util.strip_tags('<p>Jazz &amp; blues</p><p>at&nbsp;8</p>') == 'Jazz & blues at 8'
    assert util.strip_tags('<br/>') is None and util.strip_tags(None) is None


def test_fold():
    assert util.fold('  Jazz\n  NIGHT ') == 'jazz night'
