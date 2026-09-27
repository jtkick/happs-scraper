"""
Thin client for the backend's scraper API (api/scraper/…, Token auth).

All crawl state — sources, learned recipes, run history, missed-event
reports — lives in the backend so any runner (laptop, CI, server) can crawl.
"""
from __future__ import annotations
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class BackendClient:

    def __init__(self, api_base: str, token: str, timeout: float = 30):
        self.base = api_base.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Token {token}',
            'Content-Type': 'application/json',
        })

    @classmethod
    def from_settings(cls, settings) -> 'BackendClient':
        return cls(settings.get('HAPPS_API_BASE', ''), settings.get('HAPPS_SCRAPER_TOKEN', ''))

    def close(self):
        self.session.close()

    # ── Sources ───────────────────────────────────────────────────────────────

    def due_sources(self, limit: int) -> list[dict]:
        return self._json('GET', '/scraper/sources/due/', params={'limit': limit}) or []

    def source_by_domain(self, domain: str) -> Optional[dict]:
        return self._json('GET', '/scraper/sources/', params={'domain': domain})

    def update_source(self, source_id: str, data: dict) -> Optional[dict]:
        return self._json('PATCH', f'/scraper/sources/{source_id}/', json=data)

    def upsert_sources(self, seeds: list[dict]) -> Optional[dict]:
        return self._json('POST', '/scraper/sources/bulk/', json=seeds)

    # ── Events & runs ─────────────────────────────────────────────────────────

    def ingest(self, payload: dict) -> tuple[int, dict]:
        """Upsert one event. Returns (status code, body); status 0 on network error."""
        try:
            resp = self.session.post(f'{self.base}/scraper/events/', json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            return 0, {'detail': str(exc)}
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {'detail': resp.text[:200]}

    def report_run(self, report: dict) -> Optional[dict]:
        return self._json('POST', '/scraper/runs/', json=report)

    # ── Missed-event reports ──────────────────────────────────────────────────

    def open_reports(self) -> list[dict]:
        return self._json('GET', '/scraper/reports/', params={'status': 'open'}) or []

    def update_report(self, report_id: str, data: dict) -> Optional[dict]:
        return self._json('PATCH', f'/scraper/reports/{report_id}/', json=data)

    def summary(self, hours: int = 24) -> Optional[dict]:
        return self._json('GET', '/scraper/summary/', params={'hours': hours})

    # ── Internals ─────────────────────────────────────────────────────────────

    def _json(self, method: str, path: str, **kwargs):
        try:
            resp = self.session.request(method, f'{self.base}{path}', timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            logger.error("Backend %s %s failed: %s", method, path, exc)
            return None
        if resp.status_code == 404:
            return None
        if not resp.ok:
            logger.error("Backend %s %s → %s: %s", method, path, resp.status_code, resp.text[:200])
            return None
        return resp.json()
