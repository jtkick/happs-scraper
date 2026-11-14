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

    def get(self, path, **params):
        if path.startswith('cases/') and path.count('/') == 2:
            return copy.deepcopy(self.cases[path.split('/')[1]])
        if path == 'schema/':
            return {'label_fields': ['title']}
        raise AssertionError(path)

    def html(self, page_id):
        return self.html_by_page[page_id]

    def load_case(self, case_id, version=None):
        return case_from_api(self.cases[case_id], self.html('p1'))

    def post(self, path, data=None, *, idempotent=True):
        self.writes.append((path, data))
        if path == 'runs/lease/':
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
    from tools.eval_worker import run_one
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
    from tools.eval_worker import run_one
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
