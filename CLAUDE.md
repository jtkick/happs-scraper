# Abuzz — Event Scraper (happs-scraper)

## Hard Rules
- Output only what is asked. If uncertain, omit — do not guess.
- Bullets over prose. Prefer deletion over verbosity.
- Surgical edits only; do not rewrite whole files.
- Tag names must exactly match the backend `REQUIRED_TAGS` fixture in `happs-backend/backend/events/management/commands/seed.py`.
- Never create a raw `Dio`/`http` client; all HTTP in spiders goes through `AuthProvider.client` (Flutter side) or Scrapy's request machinery (scraper side).
- `ROBOTSTXT_OBEY = True`. Do not disable politeness settings.

## Authority & Links
- **Generic spider** (default for all venues): `scraper/spiders/generic.py` — sources come from the backend (`api/scraper/sources/due/`); discovery → listing → detail; learned recipe + run report sent at spider close, or each time a `keep_claiming` batch finishes (idle), after which the run is forgotten
- Detail pages: followed when the listing lacks start/description (`required`) or the page probably says more (`soft`: cut-off description, no address, no end, unexplained multi-day span); soft follows leave `GENERIC_DETAIL_RESERVE` of the budget and the recipe learns `detail_useful`
- Rendering: `scraper/rendering.py` — with `PLAYWRIGHT_ENABLED` every home/listing/detail page is rendered (feeds and sitemaps raw); a failed render is refetched raw; conditional listing checks go raw first and render only if changed (Chromium aborts on 304)
- Page text: `scraper/text.py` `full_text` = visible DOM text + embedded-JSON text; used for recurrence (strong phrases near the title, single-event pages only) and when trafilatura comes back thin
- No site-specific spiders: hand-tune a site with a locked recipe override on its backend Source (`override` + `recipe_locked`: `events_urls`, `item_css`, `detail_link_css`, `pagination_css`); events become items via `scraper/items.py` `event_item`
- Page → events: `scraper/extraction.py` (`extract_page`, `finalize`). Listings: ≥2 JSON-LD/microdata → ≥2 inline-JSON → ≥2 recipe → AI list. Details: JSON-LD → inline JSON → OpenGraph → AI. Ambient context never supplies title/start/end (`PER_EVENT_KEYS`)
- Discovery: `scraper/discovery/events_page.py` (homepage link scoring, sitemap), `scraper/discovery/links.py` (detail links by repeated DOM structure, pagination)
- Platform adapters: `scraper/platforms/` — contract in `base.py`, order in `__init__.py`. New platform = one file + tests in `tests/test_platforms.py`
- Learned recipes: `scraper/extractors/recipe.py` — AI suggests CSS, verified locally (≥80% agreement) before saving
- AI: `scraper/extractors/ai.py` — Haiku with structured outputs; `evidence` snippet must appear in page text or the event is dropped as `ai_unverified`
- Backend API client + run tracker: `scraper/sources/client.py`, `scraper/sources/tracker.py`
- Pipeline order: `scraper/pipelines.py` (100 Normalize → 150 Validate → 200 Fingerprint (in-run dedup) → 300 APISubmit (upsert)). Drops raise `DropItem('<reason_code>: …')`; the code is reported per run
- Dates: naive strings are read in the item's `timezone` (source zone, else `DEFAULT_EVENT_TIMEZONE`); yearless dates prefer the future
- Page state: `scraper/page_state.py` — per-page records kept by the backend (`api/scraper/sources/<id>/pages/`, sent back in the run report's `pages`): listing validators + fingerprints, detail pages' listing entry/hash, content hash, fingerprints, `fetched_at`, `EXTRACTION_VERSION`
- Skipping unchanged pages: a detail page is skipped (its fingerprints carried as still listed, `skipped_unchanged`) when its listing entry hashes the same, the sitemap `lastmod` (`scraper/discovery/sitemap.py`, fetched raw first; recipe learns `sitemap_urls`, `lastmod_trusted`) isn't newer, and it was parsed within `PAGE_MAX_AGE_DAYS` by the current version. Listing pages: `ConditionalFetchMiddleware` 304s carry the page's fingerprints, and its stale detail pages are still refreshed
- **Bump `EXTRACTION_VERSION` (`scraper/extraction.py`) whenever a change makes the same page parse differently**, or old parses are kept for up to `PAGE_MAX_AGE_DAYS`
- Cross-run identity, updates, health, disappearance: the backend (`happs-backend/backend/scraping/services.py`)
- `inline_json.py` — extracts event fields from `var data = {}` / `__NEXT_DATA__` / etc.; emits `_schedule_text` (private, stripped before output) for JS pages where trafilatura returns nothing
- `dates.py` — parses multi-day, date-range, and exception patterns into `rdates`/`exdates`; page text is used only on single-event pages; `parse_date` reads one date (numeric = US M/D)
- `recurrence.py` — JSON-LD schedule → `_schedule_text` → title/description → page text (strong phrases only); reads "until <date>", "for N weeks" and day lists. `finalize` turns a recurring event's start–end span into `recurrence_until` and moves the end onto the first day, unless the span is shorter than one repeat
- Cleaners: `scraper/cleaners/__init__.py` (register new cleaners here), `scraper/cleaners/title.py`
- Tag matcher: `scraper/extractors/tags.py` (`TAG_DEFINITIONS`, `_VENUE_TYPE_TAGS`, `_VENUE_NAME_TAGS`)
- OSM seeds: `scraper/seeds/overpass.py` (`VenueSeed`, `query_area`, `SearchArea`)
- Cases (`scraper/eval/`): one saved page + what it should parse to. `case.py` format: `case.json` (url, kind, `captured_at`, `seed_context`, labelled `events`, `not_events`, `complete`, `known_failures`) + `page.html` + `parsed.json` (the scraper's output at capture, never edited) + `ai_response.json` (the model's answer, replayed offline). In a label a key asserts its value, a missing key isn't checked, `null` asserts empty
- `run.py` `run_case` parses a case as a crawl would (`extract_page` → `finalize_page` → `pipelines.dry_run`) with the clock frozen at `captured_at`; `compare.py` scores labels against it (paths like `events[2].end_datetime`, `extra[<title>]`)
- Reviewed cases live in `tests/fixtures/<id>/` (committed); unreviewed ones in `review/inbox/` (gitignored). `tests/test_extractors.py` runs every fixture; a mismatch listed in `known_failures` is expected, and one that starts passing fails (same rule as strict xfail), so remove it in the same change
- Review app: `tools/review_app/` (FastAPI), started by `tools/review.py`; sync from the backend in `tools/review_app/sync.py`
- Crawl snapshots: `scraper/snapshots.py` `SnapshotSampler` queues pages where AI ran, an event went to review or was dropped (not just past), or a known events page came back empty, plus `SNAPSHOT_SAMPLE_RATE` of the rest; uploaded to `api/scraper/snapshots/` with the run reports
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
- Crawler pool: `tools/worker.py` starts `keep_claiming` crawlers, one per `WORKER_SOURCES_PER_CRAWLER` due sources (`api/scraper/sources/due/count/`), up to `WORKER_MAX_CRAWLERS`; on the server `docker compose up -d --build` (`Dockerfile`, `compose.yaml`, ~1.5 GB per crawler). Several pools can run at once: the backend leases each source
- Diagnose missed-event reports: `python tools/process_reports.py [--save-cases]` (`--save-cases` puts each page in `review/inbox/`)
- Health summary (exit 1 on newly broken sources): `python tools/crawl_summary.py --hours N`
- Scheduled: `.github/workflows/scrape.yml` (hourly: missed-event reports → crawl due sources → health summary); drop its crawl step once the pool runs on the server (both can run at once: sources are leased)
- Run tests: `pytest`
- Run tests including AI extractor calls: `pytest --run-ai`
- Review parses and add test cases: `python tools/review.py` → http://127.0.0.1:8765 (capture a URL, correct the events, "Save as fixture"; "Re-run" parses with the code on disk). `python tools/review.py sync` pulls crawl snapshots, admin corrections and missed reports into the inbox (needs `HAPPS_API_BASE` + `HAPPS_SCRAPER_TOKEN`)
- Capture from the command line: `python tools/capture.py <url> [--kind detail] [--venue NAME] [--timezone ZONE] [--from-file PATH --captured-at ISO] [--no-render] [--no-ai] [--edit]` (rendered by default, as the crawl renders)
- Score the scraper on the fixtures: `python tools/evaluate.py [--ai live --model ID] [--json OUT] [--baseline FILE]`
- Export training data: `python tools/export_dataset.py --out-dir dataset` (train/eval split by site, plus `corrections.jsonl`)
- JS rendering: `pip install scrapy-playwright && playwright install chromium`, then `PLAYWRIGHT_ENABLED=true` (tuned with `PLAYWRIGHT_MAX_CONTEXTS`, `PLAYWRIGHT_MAX_PAGES_PER_CONTEXT`, `PLAYWRIGHT_NAVIGATION_TIMEOUT_MS`). Without it, pages are fetched raw and JS-only sites are reported as `needs_js` in their runs

## Stop Conditions
- Destructive ops (drop DB, force push, prod deploy) → stop and ask.
- Adding a tag name not in `REQUIRED_TAGS` → ask before proceeding.
- Disabling `ROBOTSTXT_OBEY` or AutoThrottle → refuse.
