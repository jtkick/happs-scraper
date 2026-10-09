"""Tests for scraper/snapshots.py: which crawled pages are saved for review."""
import base64
import gzip
import json
import random
from datetime import datetime, timedelta, timezone

import scrapy
from scrapy.http import HtmlResponse
from scrapy.settings import Settings

from scraper.extraction import PageResult
from scraper.snapshots import SnapshotSampler
from scraper.spiders.generic import GenericEventSpider


class FakeClient:
    def __init__(self):
        self.uploaded = []

    def upload_snapshot(self, snapshot):
        self.uploaded.append(snapshot)
        return {'id': str(len(self.uploaded))}


def _spider():
    spider = GenericEventSpider()
    spider.settings = Settings({'ANTHROPIC_API_KEY': '', 'REVIEW_THRESHOLD': 0.7})
    return spider


def _soon(days=7):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _response(url='https://venue.test/events', body='<html><body>hi</body></html>', **meta):
    request = scrapy.Request(url, meta={'source_id': 'src-1', 'context': {'timezone': 'UTC'}, **meta})
    return HtmlResponse(url, body=body.encode(), encoding='utf-8', request=request)


def _event(title='Jazz Night', method='jsonld', **extra):
    return {'title': title, 'start_datetime': _soon(), 'extraction_method': method,
            'description': 'd', 'end_datetime': _soon(8), **extra}


def _sampler(rate=0.0, **kwargs):
    return SnapshotSampler(FakeClient(), sample_rate=rate, rng=random.Random(1), **kwargs)


def test_ai_page_is_saved_with_the_models_answer():
    sampler = _sampler()
    answer = {'model': 'm', 'prompt_hash': 'h', 'response': {'events': []}}
    response = _response(ai_response=answer)
    reasons = sampler.consider(_spider(), response, PageResult(strategy='ai', ai_used=True),
                               [_event(method='ai')], kind='listing')
    assert reasons == ['ai', 'review']                        # AI events score 0.5 + bonuses < 0.7
    [snap] = sampler.pending
    assert snap['ai_response'] == answer
    assert gzip.decompress(base64.b64decode(snap['html_gz'])) == response.body
    assert snap['parsed']['events'][0]['title'] == 'Jazz Night'
    assert snap['context']['kind'] == 'listing' and snap['context']['seed_context'] == {'timezone': 'UTC'}
    json.dumps(snap)


def test_unremarkable_page_is_only_sampled():
    page = (_response(), PageResult(strategy='jsonld', single=False), [_event()])
    assert _sampler(rate=0.0).consider(_spider(), *page, kind='listing') is None
    assert _sampler(rate=1.0).consider(_spider(), *page, kind='listing') == ['sample']


def test_past_events_alone_dont_count_as_drops():
    past = _event(start_datetime='2020-01-01T19:00:00+00:00', end_datetime=None)
    assert _sampler().consider(_spider(), _response(), PageResult(), [past], kind='detail') is None
    menu = _event(title='Gift Cards')
    assert _sampler().consider(_spider(), _response(), PageResult(), [menu], kind='detail') == ['dropped']


def test_known_events_page_coming_back_empty_is_saved():
    response = _response(recipe={'events_urls': ['https://venue.test/events/']})
    assert _sampler().consider(_spider(), response, PageResult(), [], kind='listing') == ['empty']
    assert _sampler().consider(_spider(), _response(), PageResult(), [], kind='listing') is None


def test_cap_per_run_and_flush():
    sampler = _sampler(rate=1.0, max_per_run=2)
    for i in range(4):
        sampler.consider(_spider(), _response(f'https://venue.test/e{i}'), PageResult(), [_event()], kind='detail')
    assert len(sampler.pending) == 2
    assert sampler.flush() == 2
    assert sampler.pending == [] and len(sampler.client.uploaded) == 2


def test_spider_snapshots_listing_pages():
    spider = _spider()
    spider.snapshots = _sampler(rate=1.0)
    [request] = spider.entry_requests({
        'id': 'src-1', 'domain': 'venue.test', 'homepage_url': 'https://venue.test/', 'status': 'active',
        'recipe': {}, 'override': {}, 'context': {'timezone': 'UTC'},
        'effective_recipe': {'events_urls': ['https://venue.test/events']}})
    node = json.dumps([{'@type': 'Event', 'name': n, 'startDate': _soon(), 'description': 'd'}
                       for n in ('Jazz', 'Trivia')])
    body = f'<html><head><script type="application/ld+json">{node}</script></head></html>'
    list(spider.parse_listing(HtmlResponse(request.url, body=body.encode(), encoding='utf-8', request=request)))
    [snap] = spider.snapshots.pending
    assert [e['title'] for e in snap['parsed']['events']] == ['Jazz', 'Trivia']
    assert snap['source_id'] == 'src-1'


def test_ai_extractor_keeps_the_answer_on_the_response(monkeypatch):
    from scraper.extractors import ai
    answer = {'events': []}
    monkeypatch.setattr(ai, '_call', lambda *a, **k: answer)
    spider = _spider()
    spider.settings = Settings({'ANTHROPIC_API_KEY': 'k'})
    response = _response(body='<html><body><p>' + 'Live music every week. ' * 20 + '</p></body></html>')
    spider._ai_extractor(response)(response.text, response.url)
    assert response.meta['ai_response']['response'] is answer
    assert response.meta['ai_response']['prompt_hash']
