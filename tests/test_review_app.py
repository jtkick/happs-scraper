"""Tests for the review app (tools/review_app/) and its backend sync, against temp dirs."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip('fastapi')
from fastapi.testclient import TestClient  # noqa: E402

from scraper.eval import capture as capture_module  # noqa: E402
from scraper.eval.case import Case  # noqa: E402
from tools.review_app import app as app_module  # noqa: E402
from tools.review_app import sync as sync_module  # noqa: E402

LISTING = '''<html><head><script type="application/ld+json">[
 {"@type": "Event", "name": "Jazz Night", "startDate": "2027-04-01T19:00:00-04:00"},
 {"@type": "Event", "name": "Pub Quiz", "startDate": "2027-04-02T20:00:00-04:00"}
]</script></head><body><p>What's on</p></body></html>'''


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    inbox, fixtures = tmp_path / 'inbox', tmp_path / 'fixtures'
    inbox.mkdir(), fixtures.mkdir()
    monkeypatch.setattr(app_module, 'ROOTS', {'inbox': inbox, 'fixtures': fixtures})
    monkeypatch.setattr(app_module, 'INBOX_DIR', inbox)
    monkeypatch.setattr(app_module, 'FIXTURES_DIR', fixtures)
    monkeypatch.setattr(capture_module, 'INBOX_DIR', inbox)
    monkeypatch.setattr(capture_module, 'FIXTURES_DIR', fixtures)
    monkeypatch.setattr(sync_module, 'INBOX_DIR', inbox)
    monkeypatch.setattr(sync_module, 'FIXTURES_DIR', fixtures)
    monkeypatch.setattr(sync_module, 'LEDGER', tmp_path / 'synced.json')
    monkeypatch.setattr(app_module, '_reviewer', lambda: 'tester')
    return inbox, fixtures


@pytest.fixture
def client(dirs):
    return TestClient(app_module.app)


def _inbox_case(inbox, **kwargs) -> Case:
    case = capture_module.new_case('https://venue.test/events/', LISTING, ai=None,
                                   captured_at='2027-03-15T12:00:00+00:00', **kwargs)
    case.save(inbox / case.id)
    return case


def test_capture_from_pasted_html(client, dirs):
    r = client.post('/new', data={'url': 'https://venue.test/events/', 'html': LISTING, 'kind': 'listing',
                                  'timezone': 'America/New_York'}, follow_redirects=False)
    assert r.status_code == 303
    case = Case.load(dirs[0] / r.headers['location'].rsplit('/', 1)[1])
    assert [e['title'] for e in case.events] == ['Jazz Night', 'Pub Quiz']
    assert case.seed_context == {'timezone': 'America/New_York'}
    assert case.parsed['strategy'] == 'jsonld'


def test_case_state_and_pages_render(client, dirs):
    case = _inbox_case(dirs[0])
    assert client.get('/').status_code == 200
    assert client.get(f'/case/inbox/{case.id}').status_code == 200
    page = client.get(f'/case/inbox/{case.id}/page')
    assert '<script' not in page.text and '<base href="https://venue.test/events/"' in page.text
    assert 'sandbox' in page.headers['content-security-policy']
    state = client.get(f'/api/case/inbox/{case.id}').json()
    assert state['comparison']['failures'] == []
    assert state['current']['labels'][0]['title'] == 'Jazz Night'
    sources = client.get(f'/api/case/inbox/{case.id}/sources').json()
    assert len(sources['jsonld']) == 2


def test_correct_and_promote(client, dirs):
    inbox, fixtures = dirs
    case = _inbox_case(inbox)
    labels = {'events': [{'title': 'Jazz Night', 'start_datetime': '2027-04-01T20:00:00-04:00'}],
              'not_events': ['Pub Quiz'], 'complete': True}
    state = client.put(f'/api/case/inbox/{case.id}', json=labels).json()
    assert state['comparison']['failures'] == ['events[0].start_datetime', 'extra[Pub Quiz]']
    r = client.post(f'/api/case/inbox/{case.id}/promote', json=labels)
    assert r.json() == {'url': f'/case/fixtures/{case.id}'}
    saved = Case.load(fixtures / case.id)
    assert not (inbox / case.id).exists()
    assert saved.reviewer == 'tester' and saved.reviewed_at
    assert [k['path'] for k in saved.known_failures] == ['events[0].start_datetime', 'extra[Pub Quiz]']
    assert client.post(f'/api/case/fixtures/{case.id}/discard').status_code == 400


def test_fixtures_can_be_listed_and_edited(client, dirs):
    inbox, fixtures = dirs
    case = _inbox_case(inbox)
    client.post(f'/api/case/inbox/{case.id}/promote', json={})
    assert case.id in client.get('/?where=fixtures').text
    assert client.get(f'/case/fixtures/{case.id}').status_code == 200
    events = client.get(f'/api/case/fixtures/{case.id}').json()['case']['events']
    events[0]['title'] = 'Jazz Nights'
    state = client.put(f'/api/case/fixtures/{case.id}', json={'events': events}).json()
    assert state['comparison']['failures'] == ['events[0].title']
    saved = Case.load(fixtures / case.id)
    assert saved.events[0]['title'] == 'Jazz Nights'
    assert [k['path'] for k in saved.known_failures] == ['events[0].title']


def test_rerun_uses_unsaved_labels_without_saving(client, dirs):
    case = _inbox_case(dirs[0])
    state = client.post(f'/api/case/inbox/{case.id}/rerun',
                        json={'labels': {'events': [{'title': 'Brunch'}]}}).json()
    assert state['comparison']['failures'][0] == 'events[0]'
    assert Case.load(dirs[0] / case.id).events[0]['title'] == 'Jazz Night'


def test_discard(client, dirs):
    case = _inbox_case(dirs[0])
    assert client.post(f'/api/case/inbox/{case.id}/discard').json() == {'url': '/'}
    assert not (dirs[0] / case.id).exists()


# ── Sync ──────────────────────────────────────────────────────────────────────

class FakeBackend:
    def __init__(self, snapshots=(), corrections=(), html=LISTING):
        self._snapshots, self._corrections, self.html = list(snapshots), list(corrections), html
        self.updates = []

    def snapshots(self, status='pending'):
        return self._snapshots

    def snapshot_html(self, snapshot_id):
        return self.html

    def update_snapshot(self, snapshot_id, data):
        self.updates.append(('snapshot', snapshot_id, data))

    def corrections(self, status='pending'):
        return self._corrections

    def update_correction(self, correction_id, data):
        self.updates.append(('correction', correction_id, data))

    def reports(self):
        return []

    def close(self):
        pass


SNAPSHOT = {
    'id': 'snap-123456', 'url': 'https://venue.test/events/', 'captured_at': '2027-03-15T12:00:00+00:00',
    'reason': 'dropped', 'parsed': {'strategy': 'jsonld', 'events': [{'title': 'From the crawl'}], 'dropped': []},
    'ai_response': None, 'context': {'kind': 'listing', 'seed_context': {'timezone': 'America/New_York'}},
}


def _pull(monkeypatch, backend):
    monkeypatch.setenv('HAPPS_API_BASE', 'http://backend.test/api')
    monkeypatch.setenv('HAPPS_SCRAPER_TOKEN', 't')
    monkeypatch.setattr(sync_module, 'BackendClient', lambda base, token: backend)
    return sync_module.pull()


def test_sync_turns_a_snapshot_into_a_case(dirs, monkeypatch):
    backend = FakeBackend(snapshots=[SNAPSHOT])
    [case_id] = _pull(monkeypatch, backend)
    case = Case.load(dirs[0] / case_id)
    assert case.source == 'crawl' and case.source_ref == 'snap-123456'
    assert case.captured_at == SNAPSHOT['captured_at']
    assert case.parsed['events'] == [{'title': 'From the crawl'}]      # the crawl's own output kept
    assert [e['title'] for e in case.events] == ['Jazz Night', 'Pub Quiz']
    assert backend.updates == [('snapshot', 'snap-123456', {'status': 'claimed'})]
    assert _pull(monkeypatch, FakeBackend(snapshots=[SNAPSHOT])) == []    # already in the inbox


def test_sync_turns_an_admin_edit_into_one_label(dirs, monkeypatch):
    correction = {'id': 'corr-123456', 'kind': 'edit', 'user': 'jared', 'source_url': SNAPSHOT['url'],
                  'snapshot': SNAPSHOT, 'context': {},
                  'before': {'title': 'Jazz Night', 'start_datetime': '2027-04-01T23:00:00+00:00'},
                  'after': {'start_datetime': '2027-04-02T00:00:00+00:00'}}
    [case_id] = _pull(monkeypatch, FakeBackend(corrections=[correction]))
    case = Case.load(dirs[0] / case_id)
    assert case.source == 'correction' and case.complete is False
    [label] = case.events
    assert label['title'] == 'Jazz Night' and label['start_datetime'] == '2027-04-02T00:00:00+00:00'
    assert 'jared corrected start_datetime' in case.notes


def test_sync_turns_a_rejection_into_a_not_event(dirs, monkeypatch):
    correction = {'id': 'corr-654321', 'kind': 'reject', 'user': None, 'source_url': SNAPSHOT['url'],
                  'snapshot': SNAPSHOT, 'context': {}, 'before': {'title': 'Pub Quiz'},
                  'after': {'not_an_event': True}}
    [case_id] = _pull(monkeypatch, FakeBackend(corrections=[correction]))
    case = Case.load(dirs[0] / case_id)
    assert case.events == [] and case.not_events == ['Pub Quiz']


def test_sync_needs_backend_settings(monkeypatch):
    monkeypatch.delenv('HAPPS_API_BASE', raising=False)
    with pytest.raises(RuntimeError):
        sync_module.pull()
