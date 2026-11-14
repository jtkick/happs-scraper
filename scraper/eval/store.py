"""
The backend's eval store (api/eval/, happs-backend/backend/evaluation/): a
shared home for cases, so an MCP client, the worker and the golden tests use
the same data. Converts between it and Case (case.py).

Auth: HAPPS_EVAL_TOKEN (a user token from `manage.py eval_agent`), else
HAPPS_SCRAPER_TOKEN. Both go as `Authorization: Token <key>`.
"""
from __future__ import annotations
import os
import uuid
from typing import Optional

import requests

from scraper.eval.case import Case

PAGE_LIMIT = 200


class StoreError(Exception):
    """The store refused a request: `status` and its {path, code, message} `errors`."""

    def __init__(self, status: int, errors: list, body: Optional[dict] = None):
        self.status, self.errors, self.body = status, errors, body or {}
        super().__init__('; '.join(f"{e.get('path') or '-'}: {e.get('message')}" for e in errors) or f'HTTP {status}')


class Store:
    def __init__(self, api_base: str, token: str, timeout: float = 60):
        if not api_base or not token:
            raise StoreError(0, [{'path': '', 'code': 'config',
                                  'message': 'Set HAPPS_API_BASE and HAPPS_EVAL_TOKEN (or HAPPS_SCRAPER_TOKEN).'}])
        self.base = api_base.rstrip('/') + '/eval'
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Token {token}'})

    @classmethod
    def from_env(cls) -> 'Store':
        from dotenv import load_dotenv
        load_dotenv()
        return cls(os.getenv('HAPPS_API_BASE', ''),
                   os.getenv('HAPPS_EVAL_TOKEN') or os.getenv('HAPPS_SCRAPER_TOKEN', ''))

    def request(self, method: str, path: str, *, json=None, params=None, idempotent: bool = False,
                raw: bool = False):
        headers = {'Idempotency-Key': str(uuid.uuid4())} if idempotent else {}
        url = path if path.startswith('http') else f'{self.base}/{path.lstrip("/")}'
        for attempt in range(3):
            try:
                resp = self.session.request(method, url, json=json, params=params, headers=headers,
                                            timeout=self.timeout)
                break
            except requests.ConnectionError:
                if attempt == 2 or (method not in ('GET', 'PUT') and not idempotent):
                    raise
        if resp.status_code == 204:
            return None
        if not resp.ok:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            errors = data.get('errors') or [{'path': '', 'code': str(resp.status_code), 'message': resp.text[:300]}]
            raise StoreError(resp.status_code, errors, data)
        return resp.text if raw else resp.json()

    def get(self, path, **params):
        return self.request('GET', path, params=params or None)

    def post(self, path, data=None, *, idempotent=True):
        return self.request('POST', path, json=data or {}, idempotent=idempotent)

    def patch(self, path, data):
        return self.request('PATCH', path, json=data)

    def put(self, path, data):
        return self.request('PUT', path, json=data)

    def delete(self, path, **params):
        return self.request('DELETE', path, params=params or None)

    def pages(self, path, **params):
        """Every result of a paged list."""
        params.setdefault('limit', PAGE_LIMIT)
        data = self.get(path, **params)
        while True:
            yield from data['results']
            if not data.get('next'):
                return
            data = self.request('GET', data['next'])

    def html(self, page_id: str) -> str:
        return self.request('GET', f'pages/{page_id}/html/', raw=True)

    def load_case(self, case_id: str, version: Optional[int] = None) -> Case:
        """The stored case as a Case (at `version`, if given), with its page and AI answer: one request."""
        params = {'version': version} if version is not None else {}
        data = self.get(f'cases/{case_id}/bundle/', **params)
        return case_from_api(data['case'], data['html'])

    # Shorthands, so callers don't build paths.

    def schema(self) -> dict:
        return self.get('schema/')

    def case(self, case_id: str, **params) -> dict:
        return self.get(f'cases/{case_id}/', **params)

    def find_cases(self, **params) -> dict:
        return self.get('cases/', **params)

    def patch_case(self, case_id: str, version: int, changes: dict) -> dict:
        return self.patch(f'cases/{case_id}/', {**changes, 'version': version})

    def set_labels(self, case_id: str, version: int, labels: list) -> dict:
        return self.put(f'cases/{case_id}/labels/', {'version': version, 'labels': labels})

    def review(self, case_id: str, version: int) -> dict:
        return self.post(f'cases/{case_id}/review/', {'version': version}, idempotent=False)

    def run(self, run_id: str) -> dict:
        return self.get(f'runs/{run_id}/')

    def run_cases(self, run_id: str, **params) -> list:
        return self.get(f'runs/{run_id}/cases/', **params)

    def queue_run(self, **body) -> dict:
        return self.post('runs/', body)

    def lease(self, worker: str, run_id: Optional[str] = None) -> Optional[dict]:
        path = f'runs/{run_id}/lease/' if run_id else 'runs/lease/'
        return self.post(path, {'worker': worker}, idempotent=False)


def case_from_api(data: dict, html: str) -> Case:
    ai = data.get('ai_response')
    return Case(
        id=data.get('external_id') or data['id'], url=data['page']['url'], html=html, kind=data['kind'],
        captured_at=data['page']['captured_at'], source=data.get('source') or 'manual',
        source_ref=data.get('source_ref') or None, seed_context=data.get('context') or {},
        partial=data.get('partial'), recipe=data.get('recipe') or {}, platform=data.get('platform') or None,
        events=[lb['fields'] for lb in data.get('labels', [])], not_events=data.get('not_events') or [],
        complete=data.get('complete', True), known_failures=data.get('known_failures') or [],
        notes=data.get('notes') or '', reviewer=data.get('reviewer') or None,
        reviewed_at=data.get('reviewed_at'), parsed=data.get('parsed'),
        ai_response={k: ai.get(k) for k in ('model', 'prompt_hash', 'response')} if ai and 'response' in ai else None,
    )


def case_to_api(case: Case, *, tags: list[str] = (), reviewed: bool = False) -> dict:
    """The case as a PUT cases/by-key/<case.id>/ body."""
    data = {
        'page': {'url': case.url, 'html': case.html, 'captured_at': case.captured_at,
                 'origin': 'synthetic' if _is_test_url(case.url) else 'captured'},
        'kind': case.kind, 'context': case.seed_context or {}, 'partial': case.partial,
        'recipe': case.recipe or {}, 'platform': case.platform or '', 'complete': case.complete,
        'not_events': case.not_events, 'known_failures': case.known_failures, 'notes': case.notes,
        'source': case.source, 'source_ref': case.source_ref or '', 'parsed': case.parsed,
        'labels': case.events, 'tags': list(tags),
    }
    if case.ai_response is not None:
        data['ai_response'] = {**case.ai_response, 'origin': 'import'}
    if reviewed:
        data['status'] = 'reviewed'
        data['reviewer'] = case.reviewer or ''
    return data


def _is_test_url(url: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or '').endswith('.test')
