"""
Request meta keys: what a request carries between the spider's callbacks,
the downloader middlewares and the run tracker. Every key the scraper sets
is here (scrapy-playwright's own `playwright*` keys are not).

Carried from page to page (GenericEventSpider._carry)
  RUN_KEY            which SourceRun the page belongs to (RunTracker.for_request)
  SOURCE_ID          the backend Source id, stamped on each item
  CONTEXT            ambient venue data that fills events' gaps
  RECIPE             the source's recipe for this crawl, with what it learns applied
Per request
  STAGE              'home' | 'listing' | 'detail' | 'sitemap'; errors are judged by it
  PLATFORM           the platform adapter a feed request is for
  PARTIAL            a detail page's event as its listing showed it
  LISTING_URL        the listing a detail page was reached from
  PAGE_URL           a detail page's URL as listed: its page-state key (redirects may change the response's)
  DETAIL_REASON      'required' | 'soft' (scraper/follow.py)
  REFETCH            why a known detail page is fetched again (page_state.refetch_reason)
  ROBOTS, ROOT       a sitemap request is for robots.txt, or for a root sitemap
Fetching (ConditionalFetchMiddleware, scraper/rendering.py)
  CACHEABLE          keep this listing page's validators (ETag / Last-Modified)
  CONDITIONAL        send them, so an unchanged page comes back 304
  RENDER_IF_CHANGED  a raw conditional check of a page that's rendered only if it changed
  RENDER_FAILED      the browser failed this page, so it's being fetched raw
Set on the response
  AI_RESPONSE        the model's exact answer for this page (for snapshots)
"""
RUN_KEY = 'run_key'
SOURCE_ID = 'source_id'
CONTEXT = 'context'
RECIPE = 'recipe'

STAGE = 'stage'
PLATFORM = 'platform'
PARTIAL = 'partial'
LISTING_URL = 'listing_url'
PAGE_URL = 'page_url'
DETAIL_REASON = 'detail_reason'
REFETCH = 'refetch'
ROBOTS = 'robots'
ROOT = 'root'

CACHEABLE = 'cacheable'
CONDITIONAL = 'conditional'
RENDER_IF_CHANGED = 'render_if_changed'
RENDER_FAILED = 'render_failed'

AI_RESPONSE = 'ai_response'

CARRIED = (RUN_KEY, SOURCE_ID, CONTEXT, RECIPE)
