# Abuzz Scraper — Planned Work

## High priority

### Known defects
Each is pinned by a strict `xfail` test — fix the code and remove the marker together.

- **`event:*` OpenGraph tags are unreachable** (`scraper/extractors/opengraph.py`)
  extruct's `opengraph` syntax only keeps `og:*` properties, so the
  `event:start_time` / `event:end_time` / `event:location` branches never fire.
  Facebook-style event pages use exactly those. Parse them from the raw meta tags.
  — `tests/test_opengraph.py`


### Universal scraper follow-ups
- **Detail pages have no learned recipe.** Recipes cover listings only; a site whose events only exist on
  detail pages without structured data uses AI on every detail page, every run. Learn a detail recipe the
  same way (AI result → CSS → verify).
- **Validate the platform adapters against real sites.** WordPress Tribe, Squarespace and iCal parsing are
  written from their documented formats and unit-tested on synthetic payloads; capture a real fixture for
  each (`tools/capture.py`). Tockify/Timely/Eventbrite only locate the calendar page and rely on generic
  extraction.
- **Missed-event reports from the app.** Reports can only be filed in Django admin; a "report a missing
  event" action in the Flutter app would feed `MissedEventReport` directly.
- **`not_discovered` fix is a heuristic.** `process_reports.py` adds the reported page's parent path as a
  listing. If that parent isn't a listing, the report stays unresolved — check `diagnosis_detail` and set
  `Source.override.events_urls` by hand.

## Medium priority

### Geocoding fallback
Events extracted via AI or text selectors often have a location name but no lat/lon. Add a geocoding step in the normalize pipeline using the Nominatim API (free, no key required) to convert `location_address` → lat/lon when the extractor didn't provide coordinates.

### Image deduplication
If two spiders scrape the same event from different sources and both download the image, the backend stores two copies. Cross-source matching (`happs-backend/backend/scraping/services.py`) already attaches duplicates to one event, but events it misses still get their own image copy. Add a hash-based check in `_download_image` before saving.

## Lower priority

### Per-spider politeness overrides
`settings.py` has global auto-throttle settings. Some sites may need stricter limits (e.g., 5-second delays). Consider adding a per-spider `custom_settings` dict template to the base spider docstring.

### Recurring event end-date handling
When a scraped recurring event's pattern changes (e.g., a weekly Thursday event moves to Fridays), the new recurrence fingerprint creates a second event; the old one is only cancelled after it misses 2 healthy runs, so both show for a day or two. Consider a title + location fuzzy-match in the backend upsert when a new recurring fingerprint appears for a source.
