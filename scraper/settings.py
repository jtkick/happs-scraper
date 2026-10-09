import os
from dotenv import load_dotenv

load_dotenv()

# ── Identity ──────────────────────────────────────────────────────────────────

BOT_NAME = 'happs-scraper'
SPIDER_MODULES = ['scraper.spiders']
NEWSPIDER_MODULE = 'scraper.spiders'

# ── Politeness ────────────────────────────────────────────────────────────────

ROBOTSTXT_OBEY = True

# Base delay between requests (seconds). AUTOTHROTTLE adjusts this dynamically.
DOWNLOAD_DELAY = 1.5
RANDOMIZE_DOWNLOAD_DELAY = True

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.5
AUTOTHROTTLE_MAX_DELAY = 15.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0   # one outstanding request per domain
AUTOTHROTTLE_DEBUG = False

CONCURRENT_REQUESTS = 16                # spread across many domains
CONCURRENT_REQUESTS_PER_DOMAIN = 1      # never hammer a single site

# ── Headers ───────────────────────────────────────────────────────────────────

# Rotated per-request by ConditionalFetchMiddleware; this is the default.
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

DEFAULT_REQUEST_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

# ── HTTP cache (enable during development to avoid redundant fetches) ─────────

HTTPCACHE_ENABLED = os.getenv('HTTPCACHE_ENABLED', 'false').lower() == 'true'
HTTPCACHE_EXPIRATION_SECS = 86400      # 24 h
HTTPCACHE_DIR = '.scrapy/httpcache'
HTTPCACHE_IGNORE_HTTP_CODES = [500, 502, 503, 504]
HTTPCACHE_STORAGE = 'scrapy.extensions.httpcache.FilesystemCacheStorage'

# ── Middlewares ───────────────────────────────────────────────────────────────

DOWNLOADER_MIDDLEWARES = {
    'scraper.middlewares.ConditionalFetchMiddleware': 543,
    'scraper.middlewares.RotatingUserAgentMiddleware': 400,
}

# ── JavaScript rendering ──────────────────────────────────────────────────────
# When enabled, every HTML page (home, listing, detail) is rendered in headless
# Chromium so text JavaScript draws is in the response; feeds and sitemaps are
# fetched raw (scraper/rendering.py). Requires `pip install scrapy-playwright`
# and `playwright install chromium`. Without it, pages are fetched raw and
# JS-only sites are reported as `needs_js` in their crawl runs.

PLAYWRIGHT_ENABLED = os.getenv('PLAYWRIGHT_ENABLED', 'false').lower() == 'true'
if PLAYWRIGHT_ENABLED:
    DOWNLOAD_HANDLERS = {
        'http':  'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler',
        'https': 'scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler',
    }
    PLAYWRIGHT_BROWSER_TYPE = 'chromium'
    PLAYWRIGHT_MAX_CONTEXTS = int(os.getenv('PLAYWRIGHT_MAX_CONTEXTS', '2'))
    PLAYWRIGHT_MAX_PAGES_PER_CONTEXT = int(os.getenv('PLAYWRIGHT_MAX_PAGES_PER_CONTEXT', '4'))
    PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = int(os.getenv('PLAYWRIGHT_NAVIGATION_TIMEOUT_MS', '30000'))
    PLAYWRIGHT_ABORT_REQUEST = 'scraper.rendering.abort_request'

# ── Generic spider budgets (per source, per run) ──────────────────────────────

GENERIC_MAX_LISTING_PAGES = 15   # events pages + their pagination
GENERIC_MAX_DETAIL_PAGES  = 60
GENERIC_DETAIL_RESERVE    = 15   # detail pages only required follows may use

# ── Pipelines (ordered by priority) ──────────────────────────────────────────

ITEM_PIPELINES = {
    'scraper.pipelines.NormalizePipeline':        100,
    'scraper.pipelines.ValidatePipeline':         150,
    'scraper.pipelines.FingerprintDedupPipeline': 200,
    'scraper.pipelines.APISubmitPipeline':        300,
}

# ── App settings ──────────────────────────────────────────────────────────────

HAPPS_API_BASE    = os.getenv('HAPPS_API_BASE', 'http://triangulum.cc:46695/api/')
HAPPS_SCRAPER_TOKEN = os.getenv('HAPPS_SCRAPER_TOKEN', '')

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
AI_EXTRACTION_ENABLED = bool(ANTHROPIC_API_KEY)

# A detail page nothing says has changed (scraper/page_state.py) is still
# refetched once it's this old, in case the change didn't show anywhere else.
PAGE_MAX_AGE_DAYS = float(os.getenv('PAGE_MAX_AGE_DAYS', '7'))

# Events scoring below this confidence are held for review in the backend.
REVIEW_THRESHOLD = float(os.getenv('REVIEW_THRESHOLD', '0.7'))

# IANA zone for naive page datetimes when the source has none (e.g. ad-hoc -a url=… runs).
DEFAULT_EVENT_TIMEZONE = os.getenv('DEFAULT_EVENT_TIMEZONE') or None

# Pages saved to the backend for review (scraper/snapshots.py): every page where
# AI ran, an event was held for review or dropped, or a known events page came
# back empty, plus this share of the rest. At most SNAPSHOT_MAX_PER_RUN per run.
SNAPSHOTS_ENABLED = os.getenv('SNAPSHOTS_ENABLED', 'true').lower() in ('1', 'true', 'yes')
SNAPSHOT_SAMPLE_RATE = float(os.getenv('SNAPSHOT_SAMPLE_RATE', '0.01'))
SNAPSHOT_MAX_PER_RUN = int(os.getenv('SNAPSHOT_MAX_PER_RUN', '50'))

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_LEVEL = 'INFO'
LOG_FORMAT = '%(asctime)s [%(name)s] %(levelname)s: %(message)s'

# ── Misc ──────────────────────────────────────────────────────────────────────

REQUEST_FINGERPRINTER_IMPLEMENTATION = '2.7'
TWISTED_REACTOR = 'twisted.internet.asyncioreactor.AsyncioSelectorReactor'
FEED_EXPORT_ENCODING = 'utf-8'
