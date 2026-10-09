"""Tests for scraper/middlewares.py — ConditionalFetchMiddleware."""
import pytest
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response

from scraper.middlewares import ConditionalFetchMiddleware
from scraper.sources.tracker import RunTracker

URL = 'https://x.test/events'
LISTING = {'cacheable': True, 'conditional': True}


SOURCE = {'id': 'src-1', 'domain': 'x.test', 'homepage_url': 'https://x.test/'}


@pytest.fixture
def spider():
    s = type('S', (), {})()
    s.tracker = RunTracker()
    s.run = s.tracker.start(dict(SOURCE))
    return s


@pytest.fixture
def mw(spider):
    return ConditionalFetchMiddleware(type('C', (), {'spider': spider})())


def listing(spider, **meta):
    return Request(URL, meta={**LISTING, 'run_key': spider.run.key, **meta})


def known(spider, etag='"abc"', fingerprints=('fp1',)):
    spider.run.pages.update(URL, 'listing', etag=etag, last_modified='', fingerprints=list(fingerprints))


def test_validators_are_stored_and_replayed_once_page_events_are_known(mw, spider):
    mw.process_response(listing(spider), Response(URL, headers={'ETag': '"abc"'}))
    assert spider.run.pages.get(URL)['etag'] == '"abc"'
    request = listing(spider)
    assert mw.process_request(request) is None and b'If-None-Match' not in request.headers
    spider.run.pages.update(URL, 'listing', fingerprints=['fp1'])
    request = listing(spider)
    mw.process_request(request)
    assert request.headers['If-None-Match'] == b'"abc"'


def test_validators_are_only_sent_on_conditional_requests(mw, spider):
    known(spider)
    request = listing(spider, conditional=False)
    mw.process_request(request)
    assert b'If-None-Match' not in request.headers


def test_other_requests_are_untouched(mw, spider):
    known(spider)
    request = Request(URL, meta={'run_key': spider.run.key})
    mw.process_request(request)
    assert b'If-None-Match' not in request.headers
    assert mw.process_response(request, Response(URL, status=304)).status == 304


def test_304_carries_the_pages_events_to_the_tracker(mw, spider):
    known(spider, fingerprints=['fp1', 'fp2'])
    with pytest.raises(IgnoreRequest):
        mw.process_response(listing(spider), Response(URL, status=304))
    assert spider.run.seen == {'fp1', 'fp2'} and spider.run.not_modified == 1


def test_304_with_spider_passed_explicitly(spider):
    known(spider)
    with pytest.raises(IgnoreRequest):
        ConditionalFetchMiddleware().process_response(listing(spider), Response(URL, status=304), spider)
    assert spider.run.seen == {'fp1'}


def test_new_validators_replace_old_ones(mw, spider):
    known(spider, etag='"old"')
    mw.process_response(listing(spider), Response(URL, headers={'ETag': '"new"'}))
    request = listing(spider)
    mw.process_request(request)
    assert request.headers['If-None-Match'] == b'"new"'


def test_rendered_request_with_validators_is_checked_raw_first(mw, spider):
    known(spider)
    rendered = listing(spider, playwright=True, playwright_page_methods=[])
    raw = mw.process_request(rendered)
    assert raw is not None and raw.dont_filter
    assert not any(k.startswith('playwright') for k in raw.meta) and raw.meta['render_if_changed']
    assert mw.process_request(raw) is None and raw.headers['If-None-Match'] == b'"abc"'


def test_rendered_request_without_validators_goes_straight_through(mw, spider):
    assert mw.process_request(listing(spider, playwright=True)) is None
