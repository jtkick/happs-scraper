# Abuzz — Event Scraper (happs-scraper)

## Hard Rules
- Output only what is asked. If uncertain, omit — do not guess.
- Bullets over prose. Prefer deletion over verbosity.
- Surgical edits only; do not rewrite whole files.
- Tag names must exactly match the backend `REQUIRED_TAGS` fixture in `happs-backend/backend/events/management/commands/seed.py`.
- Never create a raw `Dio`/`http` client; all HTTP in spiders goes through `AuthProvider.client` (Flutter side) or Scrapy's request machinery (scraper side).
- `ROBOTSTXT_OBEY = True`. Do not disable politeness settings.

## Authority & Links
- **Generic spider** (default for all venues): `scraper/spiders/generic.py` — sources come from the backend (`api/scraper/sources/due/`); discovery → listing → detail; learned recipe + run report sent at spider close
- Site-specific spiders still subclass `scraper/spiders/base.py` (`BaseEventSpider`); use only for high-volume sites worth hand-tuning
- Page → events: `scraper/extraction.py` (`extract_page`, `finalize`). Listings: ≥2 JSON-LD/microdata → ≥2 inline-JSON → ≥2 recipe → AI list. Details: JSON-LD → inline JSON → OpenGraph → selectors → AI. Ambient context never supplies title/start/end (`PER_EVENT_KEYS`)
- Discovery: `scraper/discovery/events_page.py` (homepage link scoring, sitemap), `scraper/discovery/links.py` (detail links by repeated DOM structure, pagination)
- Platform adapters: `scraper/platforms/` — contract in `base.py`, order in `__init__.py`. New platform = one file + tests in `tests/test_platforms.py`
- Learned recipes: `scraper/extractors/recipe.py` — AI suggests CSS, verified locally (≥80% agreement) before saving
- AI: `scraper/extractors/ai.py` — Haiku with structured outputs; `evidence` snippet must appear in page text or the event is dropped as `ai_unverified`
- Backend API client + run tracker: `scraper/sources/client.py`, `scraper/sources/tracker.py`
- Pipeline order: `scraper/pipelines.py` (100 Normalize → 150 Validate → 200 Fingerprint (in-run dedup) → 300 APISubmit (upsert)). Drops raise `DropItem('<reason_code>: …')`; the code is reported per run
- Dates: naive strings are read in the item's `timezone` (source zone, else `DEFAULT_EVENT_TIMEZONE`); yearless dates prefer the future
- ETag cache: `scraper/middlewares.py` `ConditionalFetchMiddleware` — listing pages only; a 304 carries that page's remembered fingerprints forward as "still listed"
- Cross-run identity, updates, health, disappearance: the backend (`happs-backend/backend/scraping/services.py`)
- `inline_json.py` — extracts event fields from `var data = {}` / `__NEXT_DATA__` / etc.; emits `_schedule_text` (private, stripped before output) for JS pages where trafilatura returns nothing
- `dates.py` — parses multi-day, date-range, and exception patterns into `rdates`/`exdates`; page text is used only on single-event pages
- Cleaners: `scraper/cleaners/__init__.py` (register new cleaners here), `scraper/cleaners/title.py`
- Tag matcher: `scraper/extractors/tags.py` (`TAG_DEFINITIONS`, `_VENUE_TYPE_TAGS`, `_VENUE_NAME_TAGS`)
- OSM seeds: `scraper/seeds/overpass.py` (`VenueSeed`, `query_area`, `SearchArea`)
- Test fixtures: `tests/fixtures/<id>/` — `fixture.json` + `page.html` + `clean_text.txt`. `expected` (one event) or `expected_events` (listing; optional `min_recall`); `reviewed: false` fixtures are skipped
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
- Crawl due sources: `scrapy crawl generic -a limit=N`; one source: `-a source=<domain>`; ad hoc (nothing reported): `-a url=<url>`; force rediscovery: `-a relearn=1`
- Diagnose missed-event reports: `python tools/process_reports.py [--save-fixtures]`
- Health summary (exit 1 on newly broken sources): `python tools/crawl_summary.py --hours N`
- Scheduled: `.github/workflows/scrape.yml` (hourly: reports → crawl → summary)
- Run a site-specific spider: `scrapy crawl <spider_name>`
- Run tests: `pytest`
- Run tests including AI extractor calls: `pytest --run-ai`
- Capture a new test fixture: `python tools/capture.py <url> [--venue NAME] [--address TEXT] [--lat F] [--lon F] [--ai]`
- Export fine-tuning dataset: `python tools/export_finetune.py --output dataset.jsonl`
- JS rendering: `pip install scrapy-playwright && playwright install chromium`, then `PLAYWRIGHT_ENABLED=true`. Without it, JS-only sites are reported as `needs_js` in their runs

## Stop Conditions
- Destructive ops (drop DB, force push, prod deploy) → stop and ask.
- Adding a tag name not in `REQUIRED_TAGS` → ask before proceeding.
- Disabling `ROBOTSTXT_OBEY` or AutoThrottle → refuse.
