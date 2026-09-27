# Abuzz — Event Scraper (happs-scraper)

## Hard Rules
- Output only what is asked. If uncertain, omit — do not guess.
- Bullets over prose. Prefer deletion over verbosity.
- Surgical edits only; do not rewrite whole files.
- Tag names must exactly match the backend `REQUIRED_TAGS` fixture in `happs-backend/backend/events/management/commands/seed.py`.
- Never create a raw `Dio`/`http` client; all HTTP in spiders goes through `AuthProvider.client` (Flutter side) or Scrapy's request machinery (scraper side).
- `ROBOTSTXT_OBEY = True`. Do not disable politeness settings.

## Authority & Links
- Pipeline order: `scraper/pipelines.py` (100 Normalize → 200 Dedup → 300 APISubmit)
- Spider base: `scraper/spiders/base.py` (`BaseEventSpider`, `start_seeds`, context propagation)
- Extractor waterfall (base.py): `jsonld.py` → `inline_json.py` (always runs) → `opengraph.py` → selectors → `ai.py`; then unconditional passes: `recurrence.py`, `dates.py` (rdates/exdates), `tags.py`
- `inline_json.py` — extracts event fields from `var data = {}` / `__NEXT_DATA__` / etc.; emits `_schedule_text` (private, stripped before output) for JS pages where trafilatura returns nothing
- `dates.py` — parses multi-day, date-range, and exception patterns into `rdates`/`exdates`; run on trafilatura text → `_schedule_text` → description (first match wins)
- Cleaners: `scraper/cleaners/__init__.py` (register new cleaners here), `scraper/cleaners/title.py`
- Tag matcher: `scraper/extractors/tags.py` (`TAG_DEFINITIONS`, `_VENUE_TYPE_TAGS`, `_VENUE_NAME_TAGS`)
- OSM seeds: `scraper/seeds/overpass.py` (`VenueSeed`, `query_area`, `SearchArea`)
- Test fixtures: `tests/fixtures/<id>/` — `fixture.json` + `page.html` + `clean_text.txt`
- Unit tests: one file per module — `tests/test_<module>.py`. Known defects are recorded as `@pytest.mark.xfail(strict=True)` with the cause in `reason`; fixing one turns it into an XPASS failure, so drop the marker in the same change.
- `.env.example` — required env vars

## Setup / Test
- `python3 -m venv .venv && source .venv/bin/activate`
- `pip install -r requirements-dev.txt`
- `cp .env.example .env` then fill in `HAPPS_API_BASE` and `HAPPS_SCRAPER_TOKEN`

## Workflow
- Run a spider: `scrapy crawl <spider_name>`
- Run tests: `pytest`
- Run tests including AI extractor calls: `pytest --run-ai`
- Capture a new test fixture: `python tools/capture.py <url> [--venue NAME] [--address TEXT] [--lat F] [--lon F] [--ai]`
- Export fine-tuning dataset: `python tools/export_finetune.py --output dataset.jsonl`

## Stop Conditions
- Destructive ops (drop DB, force push, prod deploy) → stop and ask.
- Adding a tag name not in `REQUIRED_TAGS` → ask before proceeding.
- Disabling `ROBOTSTXT_OBEY` or AutoThrottle → refuse.
