"""Tests for tools/worker.py — the autoscaling crawler pool."""
import signal

import pytest

from tools.worker import Pool, crawler_command, desired_crawlers


@pytest.mark.parametrize('due, expected', [(0, 0), (1, 1), (20, 1), (21, 2), (200, 4)])
def test_desired_crawlers(due, expected):
    assert desired_crawlers(due, max_crawlers=4, per_crawler=20) == expected


class FakeProcess:
    def __init__(self):
        self.done, self.signals = False, []

    def poll(self):
        return 0 if self.done else None

    def send_signal(self, sig):
        self.signals.append(sig)
        self.done = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.done = True


class FakeClient:
    def __init__(self, *counts):
        self.counts = list(counts)

    def due_count(self):
        return self.counts.pop(0)


def pool(*counts):
    return Pool(FakeClient(*counts), FakeProcess, max_crawlers=3, per_crawler=10)


def test_pool_grows_with_the_backlog_and_never_kills_to_shrink():
    p = pool(15, 45, 0)
    p.tick()
    assert len(p.crawlers) == 2
    p.tick()
    assert len(p.crawlers) == 3                      # capped at max_crawlers
    p.tick()
    assert len(p.crawlers) == 3 and not any(c.done for c in p.crawlers)


def test_finished_crawlers_are_replaced_while_work_remains():
    p = pool(25, 25)
    p.tick()
    p.crawlers[0].done = True
    p.tick()
    assert len(p.crawlers) == 3 and all(not c.done for c in p.crawlers)


def test_unreachable_backend_starts_nothing():
    p = pool(None)
    p.tick()
    assert p.crawlers == []


def test_stop_passes_sigterm_on():
    p = pool(30)
    p.tick()
    p.stop(timeout=1)
    assert all(c.signals == [signal.SIGTERM] for c in p.crawlers)


def test_crawler_command_keeps_claiming():
    command = crawler_command(batch=5, budget_minutes=20)
    assert command[1:5] == ['-m', 'scrapy', 'crawl', 'generic']
    assert 'keep_claiming=1' in command and 'batch=5' in command and 'budget_minutes=20' in command
