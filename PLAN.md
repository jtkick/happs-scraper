# Abuzz Scraper — Planned Work

## High priority

### Known defects
Each is pinned by a strict `xfail` test — fix the code and remove the marker together.

- **Written ordinals in recurrence text** (`scraper/extractors/recurrence.py`, `_from_text`)
  `rstrip('stndrh')` eats letters out of words, so "first tuesday" yields `fiTU`
  instead of `1TU`. Only numeric ordinals ("3rd") work. Match the ordinal against
  `_ORDINAL_MAP` before stripping. — `tests/test_recurrence.py`
- **Currency symbols in `ticket_price`** (`scraper/pipelines.py`, `NormalizePipeline`)
  Only commas are stripped before `float()`, so `"$25.00"` from a CSS selector
  raises and the price is silently set to None. — `tests/test_pipelines.py`
- **`event:*` OpenGraph tags are unreachable** (`scraper/extractors/opengraph.py`)
  extruct's `opengraph` syntax only keeps `og:*` properties, so the
  `event:start_time` / `event:end_time` / `event:location` branches never fire.
  Facebook-style event pages use exactly those. Parse them from the raw meta tags.
  — `tests/test_opengraph.py`
- **Context can supply a title** (`scraper/spiders/base.py`, `parse_event`)
  The "never return a title from `extract_page_context`" rule is documentation
  only. A mis-written spider labels every event with the venue name instead of
  failing loudly. — `tests/test_spider.py`
- **Naive datetimes resolve to the scraper machine's timezone**
  (`scraper/pipelines.py`, `_parse_date`) A page that prints "June 5 at 7pm" with
  no offset is read as 7pm *local to the runner*, so the same page yields
  different UTC times on a dev laptop and in CI. Needs a per-spider or
  per-venue timezone in the context.


### Site-specific spiders
The base infrastructure is complete but no actual spiders exist yet. The immediate next step is writing spiders for the target area's venues and event aggregators. For each site:

1. Determine page structure (listing → detail, or venue → listing → detail)
2. Check for JSON-LD — if present, a spider may be 3–5 lines
3. Identify `listing_link_css` and whether `is_intermediate_page` is needed
4. Add `extract_page_context` for shared venue data

Common target types to cover first:
- Local venue websites (bars, clubs, theatres)
- City event calendars
- Eventbrite city pages (`eventbrite.com/d/<city>/`)
- Meetup.com city pages

### Scheduled runs
No scheduling exists. Options:
- **GitHub Actions cron**: add a `.github/workflows/scrape.yml` that triggers `scrapy crawl` on a schedule against the live server
- **Cron on the server**: simple `crontab` entry calling the spider via the venv
- **Celery Beat**: heavier but enables per-spider scheduling and monitoring

Recommended: GitHub Actions cron for simplicity (same pattern as the CI/CD release workflow in the Flutter repo).

### Overpass-driven discovery
`scraper/seeds/overpass.py` is built but not yet connected to any spider. Need a runner script or spider that:
1. Calls `query_area()` with the target city/radius
2. Creates a `start_urls` list from the returned `VenueSeed.url` values
3. Passes `seed.as_context()` as the initial `meta['context']` for each request

This would let the scraper self-discover new venues without needing to maintain a manual URL list.

## Medium priority

### requirements.txt and .env.example
These files need to be written. The venv was created manually during development. Add:
- `scrapy`
- `extruct`
- `trafilatura`
- `dateparser`
- `anthropic`
- `python-dotenv`
- `requests`

### Geocoding fallback
Events extracted via AI or text selectors often have a location name but no lat/lon. Add a geocoding step in the normalize pipeline using the Nominatim API (free, no key required) to convert `location_address` → lat/lon when the extractor didn't provide coordinates.

### Image deduplication
If two spiders scrape the same event from different sources and both download the image, the backend stores two copies. Add a hash-based check in `ScrapedEventView` before saving the image.

### Error alerting
Spider failures are logged but there is no alerting. If a spider crashes silently (e.g., a site blocks the scraper), no one knows. Options:
- Write error counts to a file and alert if non-zero
- Send a Slack/email summary after each run

## Lower priority

### Per-spider politeness overrides
`settings.py` has global auto-throttle settings. Some sites may need stricter limits (e.g., 5-second delays). Consider adding a per-spider `custom_settings` dict template to the base spider docstring.

### Scraped event review UI
Currently all scraped events are published immediately. If a review queue is added to the backend (see backend PLAN.md), the scraper should set `status='pending_review'` for low-confidence extractions (e.g., extraction_method = 'ai' with partial fields).

### Multi-city support
The scraper currently has no concept of geography in its URL seeds. To run against multiple cities, the runner would need to call `query_area()` for each city and either run separate spiders or pass the city as a spider argument.

### Recurring event end-date handling
When a scraped recurring event's pattern changes (e.g., a weekly Thursday event moves to Fridays), the scraper would see a new recurrence fingerprint and create a second event rather than updating the existing one. Consider adding a title + location fuzzy-match check when no recurring fingerprint is found, to catch pattern changes on known events.
