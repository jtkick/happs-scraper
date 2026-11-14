"""
Crawl a site case with the real generic spider, offline: every request the
crawl makes (robots.txt, sitemaps, pages, links off the site) is answered from
the case's pages by EvalSiteMiddleware, and anything else gets a 404. This
measures discovery and link-following, not just parsing.

The crawl runs in a subprocess (python -m scraper.eval.sites IN OUT) because
Scrapy's reactor can't be started twice in one process. The clock starts at
the case's captured_at and runs forward. Items go through every pipeline but
the backend submit; nothing is sent anywhere (HAPPS_API_BASE points nowhere).
AutoThrottle and the other politeness settings stay as configured.

AI: 'replay' answers from the case's recorded answers by prompt hash, 'live'
asks the model and returns the new answers, None never asks.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from scraper.eval.case import ROOT, Case

TIMEOUT = 600
_DEAD_API = 'http://127.0.0.1:9/api'


def page_key(url: str) -> str:
    """URLs that name the same page: no fragment, no trailing slash, query kept."""
    parts = urlsplit(url)
    path = parts.path.rstrip('/') or '/'
    return f'{parts.scheme}://{parts.netloc.lower()}{path}' + (f'?{parts.query}' if parts.query else '')


def run_site(case: Case, ai: Optional[str] = 'replay', *, api_key: str = '', model: Optional[str] = None):
    """Crawl the site case: a RunResult with events, dropped, fetched URLs and any new AI answers."""
    from scraper.eval.run import RunResult
    payload = {
        'homepage': case.url, 'captured_at': case.captured.isoformat(), 'seed_context': case.seed_context or {},
        'recipe': case.recipe or {}, 'pages': case.site_pages, 'ai': ai, 'model': model,
        'ai_responses': case.ai_responses or ([case.ai_response] if case.ai_response else []),
    }
    with tempfile.TemporaryDirectory() as tmp:
        inp, out = Path(tmp) / 'in.json', Path(tmp) / 'out.json'
        inp.write_text(json.dumps(payload))
        env = {**os.environ, 'SCRAPY_SETTINGS_MODULE': 'scraper.settings', 'PLAYWRIGHT_ENABLED': 'false',
               'SNAPSHOTS_ENABLED': 'false', 'HTTPCACHE_ENABLED': 'false', 'HAPPS_API_BASE': _DEAD_API,
               'HAPPS_SCRAPER_TOKEN': '', 'ANTHROPIC_API_KEY': api_key if ai == 'live' else ''}
        proc = subprocess.run([sys.executable, '-m', 'scraper.eval.sites', str(inp), str(out)], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=TIMEOUT)
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(f'Site crawl failed ({proc.returncode}): {proc.stderr.strip()[-2000:]}')
        data = json.loads(out.read_text())
    result = RunResult(events=data['events'], dropped=data['dropped'], strategy='crawl',
                       ai_used=data['ai_used'], ai_missing=data['ai_missing'])
    result.fetched = data['fetched']
    result.ai_responses = data['ai_responses']
    return result


def discovery(case: Case, fetched: list[str]) -> dict:
    """Which of the URLs a good crawl must fetch it did fetch."""
    got = {page_key(u) for u in fetched}
    expected = list(case.expect_fetched or [])
    return {'expected': expected, 'fetched': len(got),
            'missing': [u for u in expected if page_key(u) not in got]}


# ── In the subprocess ─────────────────────────────────────────────────────────

class EvalSiteMiddleware:
    """Answers every request from the site's pages (last downloader middleware, so robots.txt
    handling, redirects and the rest still run on what it returns)."""

    fetched: list[str] = []

    def __init__(self, pages: list[dict]):
        self.pages = {page_key(p['url']): p for p in pages}

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler.settings.get('EVAL_SITE')['pages'])

    def process_request(self, request, spider=None):
        from scrapy.http import HtmlResponse, TextResponse, XmlResponse
        EvalSiteMiddleware.fetched.append(request.url)
        page = self.pages.get(page_key(request.url))
        if page is None:
            return HtmlResponse(request.url, status=404, body=b'<h1>Not found</h1>', encoding='utf-8',
                                request=request)
        ctype = page.get('content_type') or 'text/html; charset=utf-8'
        cls = HtmlResponse if 'html' in ctype else XmlResponse if 'xml' in ctype else TextResponse
        return cls(request.url, status=page.get('status', 200), body=page['html'].encode('utf-8'), encoding='utf-8',
                   headers={'Content-Type': ctype}, request=request)


def _spider_class():
    from urllib.parse import urlparse

    from scraper.extractors import ai as ai_extractor
    from scraper.metakeys import CONTEXT
    from scraper.spiders.generic import GenericEventSpider

    class EvalSiteSpider(GenericEventSpider):
        name = 'eval_site'
        answers: dict = {}
        recorded: list = []
        ai_used = False
        ai_missing = False

        def _sources(self):
            site = self.settings.get('EVAL_SITE')
            recipe = site.get('recipe') or {}
            return [{'id': None, 'domain': urlparse(site['homepage']).hostname, 'homepage_url': site['homepage'],
                     'status': 'new', 'effective_recipe': recipe, 'recipe': recipe,
                     'context': site.get('seed_context') or {}}]

        def _ai_extractor(self, response):
            site = self.settings.get('EVAL_SITE')
            mode = site.get('ai')
            if not mode:
                return None
            venue = (response.meta.get(CONTEXT) or {}).get('location_title')
            cls = type(self)
            if mode == 'live':
                api_key = self.config.api_key
                if not api_key:
                    return None
                respond = ai_extractor.recorder(api_key, cls.recorded.append, site.get('model'))

                def counted(system, user, schema, label):
                    cls.ai_used = True
                    return respond(system, user, schema, label)
                return ai_extractor.extractor(api_key, venue=venue, respond=counted)

            def replay(system, user, schema, label):
                cls.ai_used = True
                answer = cls.answers.get(ai_extractor.prompt_hash(user))
                if answer is None:
                    cls.ai_missing = True
                    return None
                return answer
            return ai_extractor.extractor('', venue=venue, respond=replay)

    return EvalSiteSpider


def main(inp: str, out: str):
    from datetime import datetime

    import time_machine
    site = json.loads(Path(inp).read_text())
    with time_machine.travel(datetime.fromisoformat(site['captured_at']), tick=True):
        from scrapy import signals
        from scrapy.crawler import CrawlerProcess
        from scrapy.utils.project import get_project_settings

        from scraper.pipelines import _plain

        settings = get_project_settings()
        settings.set('ITEM_PIPELINES', {k: v for k, v in settings.getdict('ITEM_PIPELINES').items()
                                        if not k.endswith('APISubmitPipeline')})
        middlewares = dict(settings.getdict('DOWNLOADER_MIDDLEWARES'))
        middlewares['scraper.eval.sites.EvalSiteMiddleware'] = 1000
        settings.set('DOWNLOADER_MIDDLEWARES', middlewares)
        for key, value in (('SNAPSHOTS_ENABLED', False), ('HTTPCACHE_ENABLED', False),
                           ('PLAYWRIGHT_ENABLED', False), ('LOG_LEVEL', os.getenv('EVAL_SITE_LOG_LEVEL', 'WARNING')), ('HAPPS_API_BASE', _DEAD_API),
                           ('HAPPS_SCRAPER_TOKEN', ''), ('EVAL_SITE', site)):
            settings.set(key, value)

        spider_cls = _spider_class()
        spider_cls.answers = {a['prompt_hash']: a['response'] for a in site.get('ai_responses') or []
                              if a and a.get('response') is not None}
        events, dropped = [], []
        process = CrawlerProcess(settings, install_root_handler=False)
        crawler = process.create_crawler(spider_cls)
        # Signals hold receivers weakly, so these need names that outlive the crawl.
        def scraped(item, **kw):
            events.append(_plain(item))

        def item_dropped(item, exception, **kw):
            dropped.append({**_plain(item), 'drop_reason': str(exception).split(':', 1)[0]})
        crawler.signals.connect(scraped, signal=signals.item_scraped)
        crawler.signals.connect(item_dropped, signal=signals.item_dropped)
        process.crawl(crawler, url=site['homepage'])
        process.start()
    Path(out).write_text(json.dumps({
        'events': events, 'dropped': dropped, 'fetched': EvalSiteMiddleware.fetched,
        'ai_used': spider_cls.ai_used, 'ai_missing': spider_cls.ai_missing, 'ai_responses': spider_cls.recorded,
    }, default=str))


if __name__ == '__main__':
    # Run as the imported module, so the middleware Scrapy loads by name is this same one.
    from scraper.eval import sites
    sites.main(sys.argv[1], sys.argv[2])
