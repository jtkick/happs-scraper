# Abuzz — Event Scraper (happs-scraper)

## Hard Rules
- Output only what is asked. If uncertain, omit — do not guess.
- Bullets over prose. Prefer deletion over verbosity.
- Surgical edits only; do not rewrite whole files.
- Tag names must exactly match the backend `REQUIRED_TAGS` fixture in `happs-backend/backend/events/management/commands/seed.py`.
- Never create a raw `Dio`/`http` client; all HTTP in spiders goes through `AuthProvider.client` (Flutter side) or Scrapy's request machinery (scraper side).
- robots.txt is obeyed by default; `ROBOTSTXT_OBEY=false` in `.env` turns it off for the crawl and for captures (`scraper/eval/capture.py`). Only the owner sets it; never flip it yourself. Do not disable the other politeness settings (AutoThrottle, `CONCURRENT_REQUESTS_PER_DOMAIN`).

## Authority & Links
- **Generic spider** (default for all venues): `scraper/spiders/generic.py` — sources come from the backend (`api/scraper/sources/due/`); discovery → listing → detail; learned recipe + run report sent at spider close, or each time a `keep_claiming` batch finishes (idle), after which the run is forgotten
- Detail pages (`scraper/follow.py`): followed when the listing lacks start/description (`required`) or the page probably says more (`soft`: cut-off description, no address, no end, unexplained multi-day span); soft follows leave `GENERIC_DETAIL_RESERVE` of the budget and the recipe learns `detail_useful`
- Rendering: `scraper/rendering.py` — with `PLAYWRIGHT_ENABLED` every home/listing/detail page is rendered (feeds and sitemaps raw); a failed render is refetched raw; conditional listing checks go raw first and render only if changed (Chromium aborts on 304)
- Page text: `scraper/text.py` — `main_text` (trafilatura; what dates and the model read, `ai.page_text` keeps tables) falls back to `full_text` = visible DOM text + embedded-JSON text when thin; `full_text` is also used for recurrence (strong phrases near the title, single-event pages only)
- Request meta keys: `scraper/metakeys.py` names and documents every key a request carries (add new ones there; no string keys in code). A 304 listing raises `middlewares.NotModified`
- Shared helpers: `scraper/util.py` (`parse_iso` keeps naive event times naive, `parse_utc` for timestamps, `now_iso`, `strip_tags`, `fold`); no scraper imports, so any module can use it. Fingerprint text normalization (`pipelines.fingerprint_text`) is separate: changing it changes every fingerprint
- No site-specific spiders: hand-tune a site with a locked recipe override on its backend Source (`override` + `recipe_locked`: `events_urls`, `item_css`, `detail_link_css`, `pagination_css`); events become items via `scraper/items.py` `event_item`; an item from a detail page carries its `listing_url`, which the backend uses to show curators the event on the listing it came from (it's left out of content hashes)
- Page → events: `scraper/extraction.py`: `parse_page` / `parse_feed` take one page start to finish and are the only entry points (the spider and `run_case` both call them, so fixtures test the crawl path; change parsing there, never in a caller), built on `extract_page` and `finalize`. Listings: ≥2 JSON-LD/microdata → ≥2 inline-JSON → ≥2 recipe → AI list (asked even when the single-event waterfall found a complete event; a listing that still comes out as one event takes no dates, recurrence or start from its page text). Details: JSON-LD → inline JSON → OpenGraph → AI. Ambient context never supplies title/start/end (`PER_EVENT_KEYS`)
- Discovery: `scraper/discovery/events_page.py` (homepage link scoring, scoring sitemap URLs), `scraper/discovery/links.py` (detail links by repeated DOM structure, pagination); sitemaps are read by `scraper/discovery/sitemap.py` `SitemapRead` (robots.txt roots, else `/sitemap.xml`; indexes and gzip), both to discover events pages and for `lastmod`
- Platform adapters: `scraper/platforms/` — contract in `base.py`, order in `__init__.py`. New platform = one file + tests in `tests/test_platforms.py`
- Learned recipes: `scraper/extractors/recipe.py` — AI suggests CSS, verified locally (≥80% agreement) before saving
- AI: `scraper/extractors/ai.py` — Haiku with structured outputs; `evidence` snippet must appear in page text or the event is dropped as `ai_unverified`; the crawl never calls the model on the event loop: callbacks that may reach it are async and use `extraction.parse_page_async` / `asyncio.to_thread` (tests drain them with `drain()`)
- Backend API client + run tracker: `scraper/sources/client.py`, `scraper/sources/tracker.py`
- Pipeline order: `scraper/pipelines.py` (100 Normalize → 150 Validate → 200 Fingerprint (in-run dedup) → 300 APISubmit (upsert)). Drops raise `DropItem('<reason_code>: …')`; the code is reported per run. `missing_start` = no start found at all (mostly pages that aren't events); `unparseable_start` = a start string dateparser couldn't read
- Dates: naive strings are read in the item's `timezone` (source zone, else `DEFAULT_EVENT_TIMEZONE`); yearless dates prefer the future
- Page state: `scraper/page_state.py` — per-page records kept by the backend (`api/scraper/sources/<id>/pages/`, sent back in the run report's `pages`): listing validators + fingerprints, detail pages' listing entry/hash, content hash, fingerprints, `fetched_at`, `EXTRACTION_VERSION`
- Skipping unchanged pages: a detail page is skipped (its fingerprints carried as still listed, `skipped_unchanged`) when its listing entry hashes the same, the sitemap `lastmod` (`scraper/discovery/sitemap.py`, fetched raw first; recipe learns `sitemap_urls`, `lastmod_trusted`) isn't newer, and it was parsed within `PAGE_MAX_AGE_DAYS` by the current version. Listing pages: `ConditionalFetchMiddleware` 304s carry the page's fingerprints, and its stale detail pages are still refreshed
- **Bump `EXTRACTION_VERSION` (`scraper/extraction.py`) whenever a change makes the same page parse differently**, or old parses are kept for up to `PAGE_MAX_AGE_DAYS`
- Cross-run identity, updates, health, disappearance: the backend (`happs-backend/backend/scraping/services.py`)
- `inline_json.py` — extracts event fields from `var data = {}` / `__NEXT_DATA__` / etc. (dates nested under `scheduling`/`schedule`/`dates`/`timing`/`config`, as Wix Events nests them, are lifted onto the event); emits `_schedule_text` (private, stripped before output) for JS pages where trafilatura returns nothing
- `dates.py` — parses multi-day, date-range, and exception patterns into `rdates`/`exdates`; page text is used only on single-event pages; `parse_date` reads one date (numeric = US M/D; a stated weekday picks the year, "Thursday 5/28", and one no year matches isn't a date). `find_start` reads one event's date + informal time ("January 9th | 6:30 doors, 7 show": the show time wins, a bare hour is evening unless the text says morning/brunch); `next_occurrence` is a weekly event's next start in its zone
- `recurrence.py` — JSON-LD schedule → `_schedule_text` → title/description (+ AI `evidence`, + a start with no date in it) → page text (strong phrases only); reads "until <date>", "for N weeks", day lists, and plural days followed by a time or dates ("Thursdays at 8", "Fridays Sep 18th – Oct 23rd"). `finalize` turns a recurring event's start–end span into `recurrence_until` and moves the end onto the first day, unless the span is shorter than one repeat
- Last-resort start (`extraction._fill_start`): an event with no start, or one with no date in it ("EVERY THU 8–11pm"), gets a dated one from its own text (`find_start`; page text only when it names at most one day), else a weekly one's next occurrence ("Thursdays at 8" seen on a Friday → next Thursday 8pm), its days from its schedule or, with nothing else, its title ("Tasting Tuesdays"). Never for a listing page that came out as one event (that's the page, not an event)
- Cleaners: `scraper/cleaners/__init__.py` (register new cleaners here), `scraper/cleaners/title.py`
- Tag matcher: `scraper/extractors/tags.py` (`TAG_DEFINITIONS`, `_VENUE_TYPE_TAGS`, `_VENUE_NAME_TAGS`)
- OSM seeds: `scraper/seeds/overpass.py` (`VenueSeed`, `query_area`, `SearchArea`)
- Cases (`scraper/eval/`): one saved page + what it should parse to. `case.py` format: `case.json` (url, kind, `captured_at`, `seed_context`, labelled `events`, `not_events`, `complete`, `known_failures`) + `page.html` + `parsed.json` (the scraper's output at capture, never edited) + `ai_response.json` (the model's answer, replayed offline). In a label a key asserts its value, a missing key isn't checked, `null` asserts empty
- `run.py` `run_case` parses a case as a crawl would (`extraction.parse_page`/`parse_feed` → `pipelines.dry_run`) with the clock frozen at `captured_at`; `compare.py` scores labels against it (paths like `events[2].end_datetime`, `extra[<title>]`)
- Reviewed cases live in `tests/fixtures/<id>/` (committed); unreviewed ones in `review/inbox/` (gitignored). `tests/test_extractors.py` runs every fixture; a mismatch listed in `known_failures` is expected, and one that starts passing fails (same rule as strict xfail), so remove it in the same change
- Review app: `tools/review_app/` (FastAPI), started by `tools/review.py`; sync from the backend in `tools/review_app/sync.py`
- Corrections: employees fix, confirm or reject scraped events in the backend's `/staff/` console (or Django admin). Each correction carries the whole event as they left it (`after`), what the scraper sent (`before`), the `changed` fields, the event's `fingerprint` and a pinned page. `correction_case` finds the event in the pinned page's parse by `source_fingerprint` (`scraper/pipelines.py`; title as a fallback) and labels it with `correction_label`, which leaves out recurrence on non-recurring events and a `url` that's only the page's own address. A correction whose pinned page is still `requested` waits up to `CAPTURE_WAIT` for it, then uses the live page
- Crawl snapshots: `scraper/snapshots.py` `SnapshotSampler` queues pages where AI ran, an event went to review or was dropped (not just past), or a known events page came back empty, plus `SNAPSHOT_SAMPLE_RATE` of the rest; uploaded to `api/scraper/snapshots/` with the run reports. Pages curators ask for (`?status=requested`) are captured by `fulfil_requests` (rendered with `scraper/eval/capture.py`, parsed like a crawl, the model asked live if there's a key) and uploaded with `request_id`; a page it can't fetch is patched `discarded` with the reason
- Unit tests: one file per module — `tests/test_<module>.py`. Known defects are recorded as `@pytest.mark.xfail(strict=True)` with the cause in `reason`; fixing one turns it into an XPASS failure, so drop the marker in the same change.
- `.env.example` — required env vars

## Setup / Test
- `python3 -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements-dev.txt`
- `cp .env.example .env` then fill in `HAPPS_API_BASE` and `HAPPS_SCRAPER_TOKEN`

## Worktrees
- Sessions often run in `.claude/worktrees/<name>/` on a `worktree-<name>` branch cut from `origin/main`
- No venv there; use the main checkout's `/home/jared/Development/happs-scraper/.venv/bin/python` (`-m pytest`, `-m scrapy`)
- `.worktreeinclude` copies in `.env`
- Land on `main`: commit, then `git fetch origin && git rebase origin/main && git push origin HEAD:main`

## Workflow
- Register sources: `python tools/seed_sources.py --lat F --lon F --radius-km N` (or `--city`, or `--url … --kind aggregator`)
- Crawl due sources: `scrapy crawl generic -a limit=N`; keep claiming batches: `-a keep_claiming=1 -a batch=N -a budget_minutes=M`; one source: `-a source=<domain>`; ad hoc (no run report, but events are still sent to the backend): `-a url=<url>`; force rediscovery (and ignore page state): `-a relearn=1`
- Crawler pool: `tools/worker.py` starts `keep_claiming` crawlers, one per `WORKER_SOURCES_PER_CRAWLER` due sources (`api/scraper/sources/due/count/`), up to `WORKER_MAX_CRAWLERS`; on the server `docker compose up -d --build` (`Dockerfile`, `compose.yaml`, ~1.5 GB per crawler). Several pools can run at once: the backend leases each source. Each poll the pool also captures up to `WORKER_CAPTURES_PER_TICK` requested pages, in its own process
- Diagnose missed-event reports: `python tools/process_reports.py [--save-cases]` (`--save-cases` puts each page in `review/inbox/`)
- Health summary (exit 1 on newly broken sources): `python tools/crawl_summary.py --hours N`
- Scheduled: `.github/workflows/scrape.yml` (hourly: missed-event reports → crawl due sources → health summary); drop its crawl step once the pool runs on the server (both can run at once: sources are leased)
- Run tests: `pytest`
- Run tests including AI extractor calls: `pytest --run-ai`
- Review parses and add test cases: `python tools/review.py` → http://127.0.0.1:8765 (capture a URL, correct the events, "Save as fixture"; "Re-run" parses with the code on disk). `python tools/review.py sync` pulls crawl snapshots, curators' page reviews and corrections, admins' corrections and missed reports into the inbox (needs `HAPPS_API_BASE` + `HAPPS_SCRAPER_TOKEN`). A page review (the backend's console checks a page at a time) becomes one case for the page (`source: page_review`): every event the curator kept, labelled as they left it on the parse matched by fingerprint, events read from their own detail pages by title only, the ones they added as missing, rejected titles in `not_events`, and `complete` when they said that's all of them. Corrections a page review covers aren't sent separately. A curator's "the page's list, not one event" (`listing`) leaves that title out of `not_events` (it's often the first real event's); a lone `listing` correction becomes an incomplete case seeded with today's parse
- Capture from the command line: `python tools/capture.py <url> [--kind detail] [--venue NAME] [--timezone ZONE] [--from-file PATH --captured-at ISO] [--no-render] [--no-ai] [--edit]` (rendered by default, as the crawl renders)
- Score the scraper on the fixtures: `python tools/evaluate.py [--ai live --model ID] [--json OUT] [--baseline FILE]`
- Export training data: `python tools/export_dataset.py --out-dir dataset` (train/eval split by site, plus `corrections.jsonl`)
- JS rendering: `pip install scrapy-playwright && playwright install chromium`, then `PLAYWRIGHT_ENABLED=true` (tuned with `PLAYWRIGHT_MAX_CONTEXTS`, `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT`, `PLAYWRIGHT_NAVIGATION_TIMEOUT_MS`). Without it, pages are fetched raw and JS-only sites are reported as `needs_js` in their runs

## Stop Conditions
- Destructive ops (drop DB, force push, prod deploy) → stop and ask.
- Adding a tag name not in `REQUIRED_TAGS` → ask before proceeding.
- Changing `ROBOTSTXT_OBEY`'s default or `.env` value → ask. Disabling AutoThrottle → refuse.
