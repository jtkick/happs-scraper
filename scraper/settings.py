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

CONCURRENT_REQUESTS = 8
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

# ── Pipelines (ordered by priority) ──────────────────────────────────────────

ITEM_PIPELINES = {
    'scraper.pipelines.NormalizePipeline':      100,
    'scraper.pipelines.FingerprintDedupPipeline': 200,
    'scraper.pipelines.APISubmitPipeline':      300,
}

# ── App settings ──────────────────────────────────────────────────────────────

HAPPS_API_BASE    = os.getenv('HAPPS_API_BASE', 'http://triangulum.cc:46695/api/')
HAPPS_SCRAPER_TOKEN = os.getenv('HAPPS_SCRAPER_TOKEN', '')

ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
AI_EXTRACTION_ENABLED = bool(ANTHROPIC_API_KEY)

DEDUP_DB_PATH = os.getenv('DEDUP_DB_PATH', 'dedup.db')

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_LEVEL = 'INFO'
LOG_FORMAT = '%(asctime)s [%(name)s] %(levelname)s: %(message)s'

# ── Misc ──────────────────────────────────────────────────────────────────────

REQUEST_FINGERPRINTER_IMPLEMENTATION = '2.7'
TWISTED_REACTOR = 'twisted.internet.asyncioreactor.AsyncioSelectorReactor'
FEED_EXPORT_ENCODING = 'utf-8'
