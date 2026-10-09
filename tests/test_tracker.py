"""Tests for scraper/sources/tracker.py — per-source run accounting and reporting."""
from scrapy.exceptions import DropItem, IgnoreRequest
from scrapy.http import Request
from twisted.python.failure import Failure

from scraper.sources.tracker import RunTracker

SOURCE = {'id': 'src-1', 'domain': 'venue.test', 'homepage_url': 'https://venue.test/',
          'recipe': {'events_urls': ['https://venue.test/events']}, 'recipe_version': 3,
          'baseline_events': 10}


class FakeClient:
    def __init__(self):
        self.reports, self.updates = [], []

    def report_run(self, report):
        self.reports.append(report)
        return {'outcome': 'healthy', 'source_status': 'active'}

    def update_source(self, source_id, data):
        self.updates.append((source_id, data))


def item(fp, status='created', **extra):
    return {'fingerprint': fp, 'source_id': 'src-1', 'ingest_status': status, 'title': 'T', **extra}


def req(run, stage='listing'):
    return Request('https://venue.test/x', meta={'run_key': run.key, 'stage': stage})


def failure(exc):
    try:
        raise exc
    except Exception:
        return Failure()


def test_items_and_drops_are_counted():
    tracker = RunTracker(FakeClient())
    run = tracker.start(dict(SOURCE))
    tracker.item_scraped(item('a'))
    tracker.item_scraped(item('b', 'unchanged'))
    tracker.item_dropped(item('c'), exception=DropItem('past_event: Old Show'))
    tracker.item_dropped(item('d'), exception=DropItem('duplicate_in_run: T'))
    report = run.report()
    assert report['seen_fingerprints'] == ['a', 'b']
    assert report['created'] == 1 and report['unchanged'] == 1
    assert report['dropped'] == [{'title': 'T', 'reason': 'past_event'}]


def test_first_page_failure_is_a_fetch_failure():
    tracker = RunTracker()
    run = tracker.start(dict(SOURCE))
    tracker.error(req(run, 'home'), failure(ConnectionError('refused')), 'home')
    assert run.report()['fetch_failed'] is True


def test_robots_block_is_distinguished():
    tracker = RunTracker()
    run = tracker.start(dict(SOURCE))
    tracker.error(req(run, 'home'), failure(IgnoreRequest('Forbidden by robots.txt')), 'home')
    assert run.report()['robots_blocked'] is True and not run.report()['fetch_failed']


def test_lost_detail_page_marks_run_incomplete():
    tracker = RunTracker()
    run = tracker.start(dict(SOURCE))
    run.listing_pages = 1
    tracker.error(req(run, 'detail'), failure(ConnectionError('x')), 'detail')
    assert run.report()['complete'] is False and not run.report()['fetch_failed']


def test_flush_saves_learned_recipe_and_reports():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE))
    tracker.learn(run, 'ai', item_css='div.card')
    tracker.flush()
    [(sid, update)] = client.updates
    assert sid == 'src-1'
    assert update['recipe'] == {'events_urls': ['https://venue.test/events'], 'item_css': 'div.card'}
    assert update['recipe_origin'] == 'ai'
    assert client.reports[0]['recipe_version'] == 3


def test_relearn_replaces_recipe_when_it_performs():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE), relearning=True)
    run.seen = {f'fp{i}' for i in range(9)}
    tracker.learn(run, 'heuristic', events_urls=['https://venue.test/calendar'])
    tracker.flush()
    [(_, update)] = client.updates
    assert update['recipe'] == {'events_urls': ['https://venue.test/calendar']}
    assert update['relearn_requested'] is False


def test_relearn_that_underperforms_keeps_old_recipe():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE), relearning=True)
    run.seen = {'fp1'}
    tracker.learn(run, 'heuristic', events_urls=['https://venue.test/wrong'])
    tracker.flush()
    [(_, update)] = client.updates
    assert 'recipe' not in update and update['relearn_requested'] is False


def test_locked_recipe_is_never_written():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE, recipe_locked=True))
    tracker.learn(run, 'ai', item_css='div.card')
    tracker.flush()
    assert client.updates == [] and len(client.reports) == 1


def test_adhoc_runs_are_not_reported():
    client = FakeClient()
    tracker = RunTracker(client)
    tracker.start({'id': None, 'homepage_url': 'https://x.test/'})
    tracker.flush()
    assert client.reports == []


def test_unchanged_pages_carry_their_events_and_are_reported():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE))
    listing_a, listing_b = req(run), Request('https://venue.test/b', meta={'run_key': run.key})
    tracker.not_modified(listing_a, ['old1', 'old2'])
    tracker.page_fetched(listing_b, 'https://venue.test/b')
    response = type('R', (), {'meta': {'stage': 'listing'}, 'url': 'https://venue.test/b'})()
    tracker.item_scraped(item('new1'), response)
    tracker.flush()
    report = client.reports[0]
    assert report['seen_fingerprints'] == ['new1', 'old1', 'old2']
    assert report['not_modified'] == 1
    pages = {p['url']: p for p in report['pages']}
    assert pages['https://venue.test/x']['fingerprints'] == ['old1', 'old2']
    assert pages['https://venue.test/b']['fingerprints'] == ['new1']
    assert all(p['kind'] == 'listing' and p['last_listed_at'] for p in pages.values())


def test_skipped_detail_pages_carry_their_events():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE))
    tracker.carry(run, ['fp-a', 'fp-b'], 'https://venue.test/events')
    tracker.flush()
    report = client.reports[0]
    assert report['skipped_unchanged'] == 1
    assert report['seen_fingerprints'] == ['fp-a', 'fp-b']
    [listing] = report['pages']
    assert listing['fingerprints'] == ['fp-a', 'fp-b']


def test_detail_page_fingerprints_are_reported_per_page():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE))
    run.pages.update('https://venue.test/e/1', 'detail', content_hash='h', fingerprints=[])
    detail = type('R', (), {'meta': {'stage': 'detail', 'listing_url': 'https://venue.test/events',
                                     'page_url': 'https://venue.test/e/1'},
                            'url': 'https://venue.test/e/1?redirected'})()
    tracker.item_scraped(item('fp'), detail)
    tracker.flush()
    pages = {p['url']: p for p in client.reports[0]['pages']}
    assert pages['https://venue.test/e/1'] == {'url': 'https://venue.test/e/1', 'kind': 'detail',
                                               'content_hash': 'h', 'fingerprints': ['fp']}


def test_known_pages_load_into_the_run():
    run = RunTracker().start(dict(SOURCE), pages=[{'url': 'https://venue.test/e/1', 'kind': 'detail'}])
    assert run.pages.get('https://venue.test/e/1')['kind'] == 'detail'
    assert run.report()['pages'] == []                   # nothing changed yet


def test_detail_page_items_are_attributed_to_their_listing():
    tracker = RunTracker()
    run = tracker.start(dict(SOURCE))
    detail = type('R', (), {'meta': {'stage': 'detail', 'listing_url': 'https://venue.test/events'},
                            'url': 'https://venue.test/events/x'})()
    tracker.item_scraped(item('fp'), detail)
    assert run.page_fps == {'https://venue.test/events': {'fp'}}


def test_detail_usefulness_is_learned_after_enough_soft_follows():
    from scraper.sources.tracker import DETAIL_SAMPLE_MIN
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE))
    for i in range(DETAIL_SAMPLE_MIN):
        tracker.soft_detail_parsed(req(run, 'detail'), useful=False)
    tracker.flush()
    [(_, update)] = client.updates
    assert update['recipe']['detail_useful'] is False


def test_too_few_soft_follows_learn_nothing():
    client = FakeClient()
    tracker = RunTracker(client)
    run = tracker.start(dict(SOURCE))
    tracker.soft_detail_parsed(req(run, 'detail'), useful=True)
    tracker.flush()
    assert client.updates == []
