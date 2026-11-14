"""The eval store client, the run worker and the MCP tools, against a fake store."""
import asyncio
import copy
import json

import pytest

from scraper.eval.case import FIXTURES_DIR, Case, load_all
from scraper.eval.run import run_case
from scraper.eval.scorecard import aggregate, score_case, scorecard
from scraper.eval.store import StoreError, case_from_api, case_to_api

HTML = """<html><head><script type="application/ld+json">
[{"@context": "https://schema.org", "@type": "Event", "name": "Jazz Night",
  "startDate": "2026-10-01T19:00:00-04:00"},
 {"@context": "https://schema.org", "@type": "Event", "name": "Pub Quiz",
  "startDate": "2026-10-02T20:00:00-04:00"}]
</script></head><body><h1>What's on</h1></body></html>"""


def api_case(case: Case, case_id='c1', version=1) -> dict:
    """A case as the store returns it (GET cases/<id>/?include=ai,parsed)."""
    body = case_to_api(case)
    return {
        'id': case_id, 'external_id': case.id, 'version': version, 'status': 'draft', 'kind': body['kind'],
        'url': case.url, 'page': {'id': 'p1', 'url': case.url, 'captured_at': case.captured_at},
        'captured_at': case.captured_at, 'context': body['context'], 'partial': body['partial'],
        'recipe': body['recipe'], 'platform': body['platform'], 'complete': body['complete'],
        'not_events': body['not_events'], 'known_failures': body['known_failures'], 'notes': body['notes'],
        'tags': [], 'source': body['source'], 'source_ref': body['source_ref'], 'parsed': body['parsed'],
        'labels': [{'id': f'l{i}', 'index': i, 'fields': f, 'note': ''} for i, f in enumerate(case.events)],
        'ai_response': {**case.ai_response, 'id': 'a1'} if case.ai_response else None, 'reviewer': '',
    }


def synthetic_case(**over) -> Case:
    fields = dict(id='synthetic-jazz', url='https://brewpub.test/events', html=HTML, kind='listing',
                  captured_at='2026-09-15T12:00:00+00:00', seed_context={'timezone': 'America/New_York'},
                  events=[{'title': 'Jazz Night', 'start_datetime': '2026-10-01T19:00:00-04:00'},
                          {'title': 'Pub Quiz', 'start_datetime': '2026-10-02T21:00:00-04:00'}])
    fields.update(over)
    return Case(**fields)


class FakeStore:
    """Answers the calls the worker and MCP tools make, and records the writes."""

    def __init__(self, cases: dict):
        self.cases = cases          # id -> api case
        self.html_by_page = {'p1': HTML}
        self.writes = []
        self.leases = []
        self.run_items = []

    def get(self, path, **params):
        if path.startswith('cases/') and path.count('/') == 2:
            return copy.deepcopy(self.cases[path.split('/')[1]])
        if path == 'schema/':
            from scraper.eval.case import LABEL_FIELDS
            return {'label_fields': list(LABEL_FIELDS), 'workers': []}
        if path.startswith('runs/') and path.endswith('/cases/'):
            return copy.deepcopy(self.run_items)
        raise AssertionError(path)

    def html(self, page_id):
        return self.html_by_page[page_id]

    def load_case(self, case_id, version=None):
        if case_id not in self.cases:
            raise StoreError(404, [{'path': '', 'code': 'not_found', 'message': 'No case has that id.'}])
        return case_from_api(self.cases[case_id], self.html('p1'))

    def schema(self):
        return self.get('schema/')

    def lease(self, worker, run_id=None):
        return self.post(f'runs/{run_id}/lease/' if run_id else 'runs/lease/', {'worker': worker})

    def post(self, path, data=None, *, idempotent=True):
        self.writes.append((path, data))
        if path.endswith('lease/'):
            return self.leases.pop(0) if self.leases else None
        if path.endswith('/finish/'):
            return {'status': 'failed' if data.get('error') else 'done', 'scorecard': data.get('scorecard')}
        if path == 'cases/' and data.get('external_id') == 'taken':
            raise StoreError(409, [{'path': 'external_id', 'code': 'exists', 'message': 'taken'}])
        if path == 'cases/':
            return {**api_case(synthetic_case()), 'external_id': data.get('external_id'), 'warnings': []}
        return {}


def test_cases_round_trip_through_the_store_format():
    for case in load_all(FIXTURES_DIR):
        back = case_from_api(api_case(case), case.html)
        for key in ('url', 'kind', 'captured_at', 'seed_context', 'partial', 'recipe', 'events',
                    'not_events', 'complete', 'known_failures', 'ai_response'):
            assert getattr(back, key) == getattr(case, key) or (key == 'recipe' and not case.recipe), (case.id, key)
        assert back.id == case.id


def test_synthetic_urls_are_marked_synthetic():
    assert case_to_api(synthetic_case())['page']['origin'] == 'synthetic'
    assert case_to_api(synthetic_case(url='https://venue.example/'))['page']['origin'] == 'captured'


def test_scoring_one_case_at_a_time_adds_up_to_the_scorecard():
    cases = load_all(FIXTURES_DIR) + [synthetic_case()]
    together = scorecard(cases)
    apart = aggregate({c.id: score_case(c, run_case(c)) for c in cases})
    assert apart == together


def test_worker_scores_each_case_and_finishes_the_run():
    from scraper.eval.runner import run_one
    good = synthetic_case()
    broken = synthetic_case(captured_at='not a date')
    fake = FakeStore({'c1': api_case(good), 'c2': api_case(broken, 'c2')})
    run = {'id': 'r1', 'ai': 'replay', 'model': '', 'items': [{'case_id': 'c1', 'case_version': 1},
                                                              {'case_id': 'c2', 'case_version': 1}]}
    run_one(fake, run, 'w')
    results = [r for path, data in fake.writes if path == 'runs/r1/results/' for r in data['results']]
    assert results[0]['case_id'] == 'c1'
    assert results[0]['failures'] == ['events[1].start_datetime']
    assert results[0]['passed'] is False
    assert results[0]['result']['events'][0]['title'] == 'Jazz Night'
    assert 'error' in results[1]
    finish = fake.writes[-1]
    assert finish[0] == 'runs/r1/finish/'
    assert finish[1]['scorecard']['events'] == 2
    assert finish[1]['scorecard']['recall'] == 1.0
    json.dumps(finish[1])


def test_worker_refuses_a_live_run_without_a_key():
    from scraper.eval.runner import run_one
    fake = FakeStore({})
    run_one(fake, {'id': 'r1', 'ai': 'live', 'items': []}, 'w', api_key='')
    assert 'ANTHROPIC_API_KEY' in fake.writes[-1][1]['error']


@pytest.fixture
def mcp_tools(monkeypatch):
    import tools.eval_mcp as module
    fake = FakeStore({'c1': api_case(synthetic_case())})
    monkeypatch.setattr(module, '_store', fake)
    return module, fake


def call_tool(module, name, args):
    result = asyncio.run(module.mcp.call_tool(name, args))
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_mcp_preview_shows_what_is_wrong(mcp_tools):
    module, _ = mcp_tools
    out = call_tool(module, 'preview', {'case_id': 'c1', 'ai': 'none'})
    assert out['failures'] == ['events[1].start_datetime']
    wrong = out['labels'][1]['wrong']['start_datetime']
    assert wrong == {'expected': '2026-10-02T21:00:00-04:00', 'actual': out['parsed'][1]['start_datetime']}


def test_mcp_read_page_text_pages_through(mcp_tools):
    module, _ = mcp_tools
    out = call_tool(module, 'read_page_text', {'page_id': 'p1', 'limit': 5})
    assert out['length'] >= len(out['text']) and out['offset'] == 0


def test_mcp_create_case_parses_and_picks_a_free_key(mcp_tools):
    module, fake = mcp_tools
    out = call_tool(module, 'create_case', {
        'url': 'https://brewpub.test/events', 'html': HTML, 'external_id': None,
        'labels': [{'title': 'taken'}], 'context': {'timezone': 'America/New_York'},
        'captured_at': '2026-09-15T12:00:00+00:00'})
    sent = [d for p, d in fake.writes if p == 'cases/']
    assert [d['external_id'] for d in sent] == ['taken', 'taken-2']
    assert sent[-1]['page']['origin'] == 'synthetic'
    assert [e['title'] for e in sent[-1]['parsed']['events']] == ['Jazz Night', 'Pub Quiz']
    assert out['external_id'] == 'taken-2'


def test_mcp_store_refusals_become_tool_errors(mcp_tools, monkeypatch):
    module, fake = mcp_tools

    def refuse(path, data=None, **kw):
        raise StoreError(409, [{'path': 'version', 'code': 'stale',
                                'message': 'The case is at version 4; you sent 1.'}])
    monkeypatch.setattr(fake, 'post', refuse)
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError, match='at version 4'):
        asyncio.run(module.mcp.call_tool('mark_reviewed', {'case_id': 'c1', 'version': 1}))


def test_mcp_lists_its_tools(mcp_tools):
    module, _ = mcp_tools
    names = {t.name for t in asyncio.run(module.mcp.list_tools())}
    assert {'capture_page', 'create_case', 'preview', 'set_labels', 'run_suite', 'compare_runs'} <= names


def _run(*case_ids, done=()):
    return {'id': 'r1', 'ai': 'replay', 'model': '',
            'items': [{'case_id': c, 'case_version': 1, 'status': 'done' if c in done else 'pending'}
                      for c in case_ids]}


def _results(fake):
    return {r['case_id']: r for path, data in fake.writes if path == 'runs/r1/results/' for r in data['results']}


def test_a_resumed_run_skips_finished_cases_but_counts_them():
    from scraper.eval.runner import run_one, score_one
    case = synthetic_case()
    fake = FakeStore({'c1': api_case(case), 'c2': api_case(case, 'c2')})
    first = score_one(case_from_api(fake.cases['c1'], HTML), 'replay', '', None)['score']
    fake.run_items = [{'case_id': 'c1', 'status': 'done', 'failures': first['failures'],
                       'new_failures': first['new_failures'],
                       'stats': {k: v for k, v in first.items() if k not in ('failures', 'new_failures')}}]
    run_one(fake, _run('c1', 'c2', done=('c1',)), 'w')
    assert list(_results(fake)) == ['c2']
    assert fake.writes[-1][1]['scorecard']['cases'] == 2
    assert fake.writes[-1][1]['scorecard']['events'] == 4


def test_a_missing_case_is_that_cases_error():
    from scraper.eval.runner import run_one
    fake = FakeStore({'c1': api_case(synthetic_case())})
    done = run_one(fake, _run('c1', 'gone'), 'w')
    results = _results(fake)
    assert 'Could not load' in results['gone']['error'] and 'error' not in results['c1']
    assert done['status'] == 'done'


def test_parallel_jobs_score_the_same():
    from scraper.eval.runner import run_one
    cases = {f'c{i}': api_case(synthetic_case(), f'c{i}') for i in range(4)}
    one, two = FakeStore(copy.deepcopy(cases)), FakeStore(copy.deepcopy(cases))
    run_one(one, _run(*cases), 'w', jobs=1)
    run_one(two, _run(*cases), 'w', jobs=2)
    strip = lambda card: {k: v for k, v in card.items() if k != 'slowest'}  # noqa: E731
    for card in (one.writes[-1][1]['scorecard'], two.writes[-1][1]['scorecard']):
        for entry in card['per_case'].values():
            entry.pop('duration_ms')
    assert strip(one.writes[-1][1]['scorecard']) == strip(two.writes[-1][1]['scorecard'])
    assert all('duration_ms' in r['stats'] for r in _results(two).values())


def test_schema_mismatch_stops_the_worker():
    from scraper.eval.runner import SchemaMismatch, check_schema
    fake = FakeStore({})
    fake.get = lambda path, **p: {'label_fields': ['title', 'colour']}
    with pytest.raises(SchemaMismatch, match='colour'):
        check_schema(fake)


def test_mcp_local_run_scores_in_process(mcp_tools, monkeypatch):
    module, fake = mcp_tools
    fake.leases = [_run('c1')]
    queued = {'id': 'r1', 'status': 'queued', 'counts': {}}
    monkeypatch.setattr(fake, 'queue_run', lambda **body: queued, raising=False)
    out = call_tool(module, 'run_suite', {'case_ids': ['c1'], 'local': True})
    assert out['status'] == 'done'
    assert list(_results(fake)) == ['c1']


def test_mcp_says_when_no_worker_will_run_it(mcp_tools, monkeypatch):
    module, fake = mcp_tools
    monkeypatch.setattr(fake, 'queue_run', lambda **body: {'id': 'r1', 'status': 'queued'}, raising=False)
    out = call_tool(module, 'run_suite', {'case_ids': ['c1'], 'wait': 0})
    assert 'No eval worker' in out['note']


def test_mcp_accept_failures_keeps_earlier_reasons(mcp_tools, monkeypatch):
    module, fake = mcp_tools
    fake.cases['c1']['known_failures'] = [{'path': 'events[0]', 'reason': 'old'}]
    sent = {}

    def patch_case(case_id, version, changes):
        sent.update(changes)
        return {**fake.cases['c1'], **changes, 'version': version + 1}
    monkeypatch.setattr(fake, 'case', lambda case_id, **p: copy.deepcopy(fake.cases[case_id]), raising=False)
    monkeypatch.setattr(fake, 'patch_case', patch_case, raising=False)
    out = call_tool(module, 'accept_failures', {'case_id': 'c1', 'version': 1, 'paths': ['events[1].start_datetime'],
                                                'reason': 'yearless date'})
    assert sent['known_failures'] == [{'path': 'events[0]', 'reason': 'old'},
                                      {'path': 'events[1].start_datetime', 'reason': 'yearless date'}]
    assert out['version'] == 2


def test_mcp_get_case_pages_labels(mcp_tools, monkeypatch):
    module, fake = mcp_tools
    monkeypatch.setattr(fake, 'case', lambda case_id, **p: copy.deepcopy(fake.cases[case_id]), raising=False)
    out = call_tool(module, 'get_case', {'case_id': 'c1', 'labels_from': 1, 'labels_limit': 1})
    assert (out['labels_total'], [lb['index'] for lb in out['labels']]) == (2, [1])


def test_mcp_generate_cases_writes_one_case_per_format_and_variant(mcp_tools, monkeypatch):
    module, fake = mcp_tools
    put = []
    monkeypatch.setattr(fake, 'put', lambda path, data: put.append((path, data)) or {'id': f'id{len(put)}'},
                        raising=False)
    out = call_tool(module, 'generate_cases', {
        'formats': ['jsonld', 'month_grid'], 'variants': ['long', 'yearless+24h'], 'slug': 'Tap Room',
        'ai': 'none', 'events': [{'title': 'Quiz', 'start': '2026-10-01T20:00'},
                                 {'title': 'Karaoke', 'start': '2026-10-01T21:00', 'weekly': ['TH']}]})
    assert out['made'] == [] and len(out['skipped']) == 4          # weekly events can't go in either format
    out = call_tool(module, 'generate_cases', {'formats': ['jsonld', 'html_list'], 'variants': ['long', 'noise'],
                                               'slug': 'Tap Room', 'ai': 'none'})
    assert [m['external_id'] for m in out['made']] == [
        'synth-tap-room-jsonld-long', 'synth-tap-room-jsonld-noise',
        'synth-tap-room-html_list-long', 'synth-tap-room-html_list-noise']
    path, body = put[0]
    assert path == 'cases/by-key/synth-tap-room-jsonld-long/'
    assert body['page']['url'] == 'https://tap-room.test/jsonld/long/' and body['page']['origin'] == 'synthetic'
    assert body['tags'] == ['synthetic', 'fmt:jsonld', 'variant:long']
    assert out['made'][0]['capture_found'] == 3 and out['made'][2]['capture_found'] == 0


def _site_case(**kw):
    from scraper.eval.synth import default_events, render_site
    captured = '2026-09-15T12:00:00+00:00'
    site = render_site(default_events(captured), slug='brew', venue='Brewpub', captured_at=captured, **kw)
    base = 'https://brew.eval.test'
    return Case(id='site-brew', url=base + '/', html=site['pages'][0]['html'], kind='site', captured_at=captured,
                seed_context={'location_title': 'Brewpub', 'timezone': 'America/New_York'}, events=site['labels'],
                site_pages=[{'url': base + p['path'], 'html': p['html'], 'status': 200,
                             'content_type': p.get('content_type', 'text/html')} for p in site['pages']],
                expect_fetched=[base + p for p in site['expect_fetched']])


def test_a_site_is_crawled_offline_and_scored_on_discovery():
    case = _site_case()
    result = run_case(case, ai=None)
    score = score_case(case, result)
    assert score['failures'] == [] and score['discovery']['missing'] == []
    assert 'https://brew.eval.test/events/trivia-night' in result.fetched
    assert {e['description'] for e in result.events} >= {'Five local comics.'}     # read from the event's page


def test_a_site_without_a_way_in_is_missing_its_pages():
    score = score_case(case := _site_case(nav=False), run_case(case, ai=None))
    assert score['recall'] == 0
    assert 'https://brew.eval.test/events' in score['discovery']['missing']


def test_site_cases_round_trip_on_disk_and_through_the_store(tmp_path):
    from scraper.eval.store import site_to_api
    case = _site_case(sitemap=True)
    case.ai_responses = [{'model': 'm', 'prompt_hash': 'h', 'response': {'events': []}}]
    case.save(tmp_path / case.id)
    back = Case.load(tmp_path / case.id)
    assert (back.kind, back.site_pages, back.expect_fetched, back.ai_responses) == (
        'site', case.site_pages, case.expect_fetched, case.ai_responses)
    body = site_to_api(back)
    assert body['slug'] == 'brew' and body['homepage'] == '/'
    assert {p['path'] for p in body['pages']} >= {'/', '/events', '/sitemap.xml', '/robots.txt'}
    bundle_case = {**api_case(back), 'kind': 'site', 'expect_fetched': back.expect_fetched,
                   'ai_responses': [{'id': 'a', **a} for a in back.ai_responses]}
    again = case_from_api(bundle_case, back.html, [{'url': p['url'], 'html': p['html'], 'status': 200,
                                                     'content_type': p['content_type']} for p in back.site_pages])
    assert (again.site_pages, again.expect_fetched, again.ai_responses) == (
        back.site_pages, back.expect_fetched, back.ai_responses)
