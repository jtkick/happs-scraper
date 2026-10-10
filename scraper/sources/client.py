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

    def due_count(self) -> Optional[int]:
        """How many sources are due (nothing is leased); None when the backend can't be reached."""
        data = self._json('GET', '/scraper/sources/due/count/')
        return data.get('due') if data else None

    def pages(self, source_id: str) -> list[dict]:
        """What earlier crawls knew about each of the source's pages (scraper/page_state.py)."""
        return self._json('GET', f'/scraper/sources/{source_id}/pages/') or []

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

    def reports(self) -> list[dict]:
        """Every report still worth a look: open or diagnosed."""
        return [r for status in ('open', 'diagnosed')
                for r in self._json('GET', '/scraper/reports/', params={'status': status}) or []]

    def summary(self, hours: int = 24) -> Optional[dict]:
        return self._json('GET', '/scraper/summary/', params={'hours': hours})

    # ── Review feeds (scraper/snapshots.py, tools/review.py sync) ──────────────

    def upload_snapshot(self, snapshot: dict) -> Optional[dict]:
        return self._json('POST', '/scraper/snapshots/', json=snapshot)

    def snapshots(self, status: str = 'pending') -> list[dict]:
        return self._json('GET', '/scraper/snapshots/', params={'status': status, 'limit': 200}) or []

    def snapshot_html(self, snapshot_id: str) -> Optional[str]:
        try:
            resp = self.session.get(f'{self.base}/scraper/snapshots/{snapshot_id}/html/',
                                    timeout=self.timeout)
        except requests.RequestException as exc:
            logger.error("Backend snapshot %s failed: %s", snapshot_id, exc)
            return None
        return resp.text if resp.ok else None

    def update_snapshot(self, snapshot_id: str, data: dict) -> Optional[dict]:
        return self._json('PATCH', f'/scraper/snapshots/{snapshot_id}/', json=data)

    def corrections(self, status: str = 'pending') -> list[dict]:
        return self._json('GET', '/scraper/corrections/', params={'status': status}) or []

    def update_correction(self, correction_id: str, data: dict) -> Optional[dict]:
        return self._json('PATCH', f'/scraper/corrections/{correction_id}/', json=data)

    def page_reviews(self, status: str = 'pending') -> list[dict]:
        return self._json('GET', '/scraper/page-reviews/', params={'status': status}) or []

    def update_page_review(self, review_id: str, data: dict) -> Optional[dict]:
        return self._json('PATCH', f'/scraper/page-reviews/{review_id}/', json=data)

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
