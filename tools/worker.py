#!/usr/bin/env python3
"""
Crawler pool: keeps enough crawlers running for the sources that are due.

Usage:
    python tools/worker.py [--max-crawlers 4] [--sources-per-crawler 20] [--batch 10]
                           [--budget-minutes 30] [--poll-seconds 60] [--captures-per-tick 5]

Every poll it asks the backend how many sources are due (nothing is leased)
and starts crawlers — `scrapy crawl generic -a keep_claiming=1 …`, each
claiming batches until none are due or its budget is spent — until there are
desired_crawlers() of them. Running crawlers are never stopped to shrink the
pool; they finish on their own. SIGTERM / SIGINT is passed on so each one
finishes its in-flight pages and sends its reports. Several pools (machines)
can run at once: the backend leases each source to one crawler.

Each poll it also captures up to --captures-per-tick pages curators asked
for (scraper/snapshots.py fulfil_requests), in this process, before scaling.

Defaults come from WORKER_MAX_CRAWLERS, WORKER_SOURCES_PER_CRAWLER,
WORKER_BATCH, WORKER_BUDGET_MINUTES, WORKER_POLL_SECONDS and
WORKER_CAPTURES_PER_TICK (see compose.yaml).
"""
from __future__ import annotations
import argparse
import logging
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger('worker')


def desired_crawlers(due: int, max_crawlers: int, per_crawler: int) -> int:
    """How many crawlers the backlog calls for: one per `per_crawler` due sources, at most `max_crawlers`."""
    return min(max_crawlers, math.ceil(due / per_crawler)) if due > 0 else 0


class Pool:

    def __init__(self, client, spawn: Callable[[], subprocess.Popen], max_crawlers: int,
                 per_crawler: int, capture: Callable[[], int] | None = None):
        self.client = client
        self.spawn = spawn
        self.max_crawlers = max_crawlers
        self.per_crawler = per_crawler
        self.capture = capture
        self.crawlers: list = []

    def tick(self) -> None:
        self.crawlers = [c for c in self.crawlers if c.poll() is None]
        if self.capture is not None:
            try:
                self.capture()
            except Exception:
                logger.exception("Capturing requested pages failed")
        due = self.client.due_count()
        if due is None:
            logger.warning("Backend unreachable; %d crawlers running", len(self.crawlers))
            return
        start = desired_crawlers(due, self.max_crawlers, self.per_crawler) - len(self.crawlers)
        if start > 0:
            logger.info("%d sources due, %d crawlers running: starting %d", due, len(self.crawlers), start)
            self.crawlers += [self.spawn() for _ in range(start)]

    def stop(self, timeout: float) -> None:
        for crawler in self.crawlers:
            if crawler.poll() is None:
                crawler.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + timeout
        for crawler in self.crawlers:
            try:
                crawler.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                crawler.kill()


def crawler_command(batch: int, budget_minutes: float) -> list[str]:
    return [sys.executable, '-m', 'scrapy', 'crawl', 'generic', '-a', 'keep_claiming=1',
            '-a', f'batch={batch}', '-a', f'budget_minutes={budget_minutes}']


def main():
    env = os.environ.get
    parser = argparse.ArgumentParser(description='Run an autoscaling pool of crawlers')
    parser.add_argument('--max-crawlers', type=int, default=int(env('WORKER_MAX_CRAWLERS', 4)))
    parser.add_argument('--sources-per-crawler', type=int,
                        default=int(env('WORKER_SOURCES_PER_CRAWLER', 20)))
    parser.add_argument('--batch', type=int, default=int(env('WORKER_BATCH', 10)))
    parser.add_argument('--budget-minutes', type=float, default=float(env('WORKER_BUDGET_MINUTES', 30)))
    parser.add_argument('--poll-seconds', type=float, default=float(env('WORKER_POLL_SECONDS', 60)))
    parser.add_argument('--stop-timeout', type=float, default=float(env('WORKER_STOP_TIMEOUT', 100)))
    parser.add_argument('--captures-per-tick', type=int, default=int(env('WORKER_CAPTURES_PER_TICK', 5)))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [worker] %(levelname)s: %(message)s')

    from scrapy.utils.project import get_project_settings
    from scraper.snapshots import fulfil_requests
    from scraper.sources.client import BackendClient
    settings = get_project_settings()
    client = BackendClient.from_settings(settings)
    command = crawler_command(args.batch, args.budget_minutes)
    api_key = settings.get('ANTHROPIC_API_KEY', '')
    pool = Pool(client, lambda: subprocess.Popen(command, cwd=ROOT), args.max_crawlers,
                args.sources_per_crawler,
                capture=lambda: fulfil_requests(client, api_key=api_key, limit=args.captures_per_tick))

    stopping = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.append(True))
    logger.info("Up to %d crawlers, one per %d due sources", args.max_crawlers, args.sources_per_crawler)
    while not stopping:
        pool.tick()
        deadline = time.monotonic() + args.poll_seconds
        while not stopping and time.monotonic() < deadline:
            time.sleep(1)
    logger.info("Stopping %d crawlers", len(pool.crawlers))
    pool.stop(args.stop_timeout)
    client.close()


if __name__ == '__main__':
    main()
