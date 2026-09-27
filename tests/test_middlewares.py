"""Tests for scraper/middlewares.py — ConditionalFetchMiddleware."""
import pytest
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response

from scraper.middlewares import ConditionalFetchMiddleware

URL = 'https://x.test/events'
LISTING = {'cacheable': True, 'conditional': True}


class Tracker:
    def __init__(self):
        self.carried = {}
        self.page_store = None

    def not_modified(self, request, carried=()):
        self.carried[request.url] = list(carried)


@pytest.fixture
def mw(tmp_path):
    m = ConditionalFetchMiddleware(str(tmp_path / 'etags.db'))
    yield m
    m.close()


def conditional_headers(mw):
    request = Request(URL, meta=LISTING)
    mw.process_request(request)
    return request.headers


def test_validators_are_replayed_once_page_events_are_known(mw):
    mw.process_response(Request(URL, meta=LISTING), Response(URL, headers={'ETag': '"abc"'}))
    assert b'If-None-Match' not in conditional_headers(mw)     # no page events on file yet
    mw.remember_events(URL, ['fp1'])
    assert conditional_headers(mw)['If-None-Match'] == b'"abc"'


def test_cacheable_but_not_conditional_stores_without_sending(mw):
    mw.process_response(Request(URL, meta={'cacheable': True}), Response(URL, headers={'ETag': '"e"'}))
    mw.remember_events(URL, [])
    request = Request(URL, meta={'cacheable': True})
    mw.process_request(request)
    assert b'If-None-Match' not in request.headers
    assert conditional_headers(mw)['If-None-Match'] == b'"e"'


def test_other_requests_are_untouched(mw):
    mw.remember(URL, etag='"abc"')
    mw.remember_events(URL, [])
    request = Request(URL)
    mw.process_request(request)
    assert b'If-None-Match' not in request.headers
    assert mw.process_response(request, Response(URL, status=304)).status == 304


def test_304_carries_the_pages_events_to_the_tracker(mw):
    mw.remember_events(URL, ['fp1', 'fp2'])
    spider = type('S', (), {'tracker': Tracker()})()
    with pytest.raises(IgnoreRequest):
        mw.process_response(Request(URL, meta=LISTING), Response(URL, status=304), spider)
    assert spider.tracker.carried == {URL: ['fp1', 'fp2']}
    assert spider.tracker.page_store is mw


def test_304_reaches_the_tracker_via_the_crawler(tmp_path):
    """Scrapy 2.13+ calls middleware hooks without `spider`; the crawler has it."""
    spider = type('S', (), {'tracker': Tracker()})()
    crawler = type('C', (), {'spider': spider})()
    m = ConditionalFetchMiddleware(str(tmp_path / 'etags.db'), crawler)
    m.remember_events(URL, ['fp1'])
    with pytest.raises(IgnoreRequest):
        m.process_response(Request(URL, meta=LISTING), Response(URL, status=304))
    assert spider.tracker.carried == {URL: ['fp1']}
    m.close()


def test_remember_overwrites(mw):
    mw.remember(URL, etag='"old"')
    mw.remember(URL, etag='"new"')
    mw.remember_events(URL, [])
    assert conditional_headers(mw)['If-None-Match'] == b'"new"'
