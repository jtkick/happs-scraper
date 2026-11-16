# Scraper Findings

Gaps found by parsing real pages by hand and comparing with what a crawl gives the same page
(the platform adapter or `extraction.parse_page` → `finalize` → Normalize/Validate/Fingerprint).
Each item names the page it was seen on. Move an item into `PLAN.md` or fix it, then delete it here.

## Open

### Recurrence: a list of ordinals keeps only the last
- Seen: mellotonebeer.com/calendar — "every 1st and 3rd Tuesday" → `['3TU']`; "Every 2nd & 4th Tuesday" → `['4TU']`
- Should be: `['1TU', '3TU']`, `['2TU', '4TU']`
- Where: `scraper/extractors/recurrence.py` — the ordinal + weekday pattern (`_ORDINAL` … `_WEEKDAY_MAP`) matches one ordinal; extend it to "1st and 3rd", "2nd & 4th", "first, third and fifth"
- Note: changes these events' fingerprints (the signature includes `byday`); bump `EXTRACTION_VERSION`

### A listing's repeats of a recurring series are dropped, with their exceptions
- Seen: mellotonebeer.com/calendar (Squarespace) lists each occurrence as its own entry. `finalize` reads the series' recurrence, all occurrences share one fingerprint, and every one after the first is dropped as `duplicate_in_run`
- Lost:
  - extra dates: MelloLaughs on Tue Nov 10 (a 2nd Tuesday; the rule is 1st and 3rd)
  - skipped dates: Last Call Trivia has no Thu Nov 26 (Thanksgiving) on a calendar that otherwise lists every Thursday through Dec 17
- Should be: merge a series' entries into one event:
  - listed dates the rule doesn't produce → `rdates`
  - dates the rule produces between the first and last listed entry that the listing doesn't have → `exdates`
  - Only for listings that clearly list every occurrence (e.g. ≥3 consecutive ones present), or the far end of a short calendar would read as cancellations
- Where: `scraper/pipelines.py` `FingerprintDedupPipeline` drops them before anything can merge them; the merge belongs earlier, per page (`extraction.finalize_page` or the platform/listing path), before items are built

### A pub's venue tags put "Happy Hour" on every event
- Seen: mellotonebeer.com (OSM `pub`) — trivia, comedy, music and dance nights all tagged `21+` and `Happy Hour`
- Scale (site capture, 2026-10-28): `Happy Hour` is on 353 of 572 kept events, every event of all 13 bar/pub sources (revelotr.com 63/63, tinroofcincinnati.com 62/62, …), more than any real tag (`Music` 131)
- vintageonrace.com's "Fright Night Kickoff", whose page says "All ages", goes out `21+`/`Happy Hour`; nothing in `tags.py` reads "all ages"
- Should be: `Happy Hour` only when the event is one (the text matcher already has phrases for it). `21+` as a venue default is defensible for a bar; for a brewery or pub that admits all ages it's also wrong. An event that says "all ages" / "family" overrides the venue default. (Venue *names* tagging `21+`: see "Venue-name tags match inside other words" and, for myfountainsquare.com, "A venue inside a bigger place claims the whole site")
- Where: `scraper/extractors/tags.py` `_VENUE_TYPE_TAGS` (`bar`, `pub`): drop `Happy Hour`; consider dropping `21+` for `pub`/`brewery`

### Squarespace: the description's times lose to the calendar's placeholder end
- Seen: mellotonebeer.com/calendar — Cincy Latin Dance Night's entry is 8:00–9:00 PM (Squarespace's default one-hour span); its description says "8PM-11PM in Undertone by mellotone". The crawl keeps 9 PM, and with no location on the entry it falls back to the source's "Mellotone Beer Project"
- Should be: when an entry's end is exactly an hour after its start and the description states a time range starting at the same time, take the description's end. A description naming a room or venue ("in Undertone") could fill an empty `location_title` before the source context does
- Where: `scraper/platforms/squarespace.py` `_event`, or `extraction.finalize` (where page/context data meets the text)

### Squarespace: ticket links in the description are lost
- Seen: mellotonebeer.com/calendar — TATTOO'S + TAROT's excerpt links "purchase TICkets here." to Eventbrite; the adapter `strip_tags` the excerpt, so `ticket_url` stays empty
- Should be: a link in the excerpt/body whose text says tickets (or to a ticketing host: eventbrite, ticketweb, dice, etix…) → `ticket_url`; RSVP/register wording → `rsvp_url`
- Where: `scraper/platforms/squarespace.py` `_event`; a shared "links in the description" helper would serve the other adapters too

### Squarespace: the excerpt is used even when the body says more
- Seen: mellotonebeer.com/calendar — Last Call Trivia's body adds "1st, 2nd, and 3rd place winners receive a mellotone beer project gift card"; MelloLaughs' body gives the door time in its own wording
- Should be: use the body when it's longer and contains the excerpt (or the excerpt is empty)
- Where: `scraper/platforms/squarespace.py` `_event` (`excerpt` or `body`)

### A venue inside a bigger place claims the whole site
- Seen: washingtonpark.org — seeded from OSM `way/549807161`, "The Porch" (`amenity=bar`), whose website is `washingtonpark.org/porch`. The domain became that one bar's source, so every park event (concerts, a flea market, a kids' festival) got `location_title` "The Porch", no address, and the bar's `21+`/`Happy Hour`. Fixed by hand in the local DB only (Washington Park, 1230 Elm Street, `venue_type` `park`)
- Also (site capture, 2026-10-28): myfountainsquare.com is seeded from OSM's "Fountain Bar", so every Fountain Square event (Winterhaus skating, workshops) gets the bar's `21+` from its name. "bar" is a whole word there, so whole-word matching wouldn't help; the seed is the wrong venue
- Also (site capture): esquiretheatre.com is seeded from OSM's "Back Alley Bar" (`amenity=bar`, the cinema's bar), so every Esquire Theatre film screening goes out at "Back Alley Bar", tagged `21+` and `Happy Hour` (all 38 on the Timely month page, a cinema that admits all ages)
- And the reverse (site capture): OSM's Great American Ball Park has website `http://cincinnati.reds.mlb.com/cin/ballpark` (which now redirects into mlb.com), so the whole of mlb.com is keyed to the ballpark: discovery picks `mlb.com/events` ("MLB Events: Draft, All-Star Game, World Series", parsed as one event on Nov 11 at the ballpark) and the old `cincinnati.reds.mlb.com` schedule. A shared platform domain (mlb.com, facebook.com, linktr.ee, square.site, toasttab.com…) can't be one venue's source; the path is the venue's site
- Should be: a website with a path below the root (`/porch`) is a page on someone else's site: don't key the domain to that venue. Either register the domain without venue context, or prefer an OSM element on the same domain whose website is the root (here the park itself, `leisure=park`)
- Where: `tools/seed_sources.py` (`by_domain`, `site_of`), `scraper/seeds/overpass.py`

### Opening hours slip through as events
- Seen: washingtonpark.org/events — "2026 Fall Porch Hours" / "Fall Porch Hours" (every day 3–9 PM) and "Porch is Open" (winter hours) are the bar's hours, listed in the calendar like events. Also myfountainsquare.com/events — "2026 Winterhaus Hours"
- Also (site capture, 2026-10-28): police-museum.org's Google Calendar feed — its Tue/Thu/Sat 10 AM–4 PM opening-hours series, retitled "Museum is Closed until Further Notice!", go out as three weekly events (the `ical` adapter, so the model never judges them)
- `_NON_EVENT_TITLE` only catches titles that start with "(opening|business|store|kitchen|office) hours" and closures (`^(?:closed|we are closed|temporarily closed)\b`: "Museum is Closed…" doesn't start with it)
- Should be: also "<anything> Hours" when the entry repeats daily or near-daily with no other content, "<place> is open", and "closed" anywhere in a short title ("<place> is closed", "closed until further notice", "closed for the holiday")
- Where: `scraper/pipelines.py` `_NON_EVENT_TITLE`

### The page text the model reads drops most of a dated list
- Seen: washingtonpark.org/events — `main_text(tables=True)` (1,287 chars) keeps 2 of the 17 dated "Events This Week" entries and none of the month grid; `full_text` has all 17. 1,287 chars isn't `THIN_TEXT`, so there's no fallback, and the AI would see two events
- Also (site capture, 2026-10-28):
  - motrpub.com/shows — `main_text` is the 778-character "MOTR has been presenting live, original music…" blurb; the 43 dated show entries ("STENOSIS, GAR, CRAIG'S LIMIT / Sunday, November 1, 2026 / 8:00PM | 21+ / More Info") are only in `full_text`. The model answers `{"events": []}`; the crawl then follows the "More Info" links as missed entries (25 of 43 within the detail budget)
  - cincyshakes.com/on-stage — `main_text` keeps only the hero ("Now Playing / October 9 - 31, 2026 / By: Michael Carleton, James FitzGerald, and John K. Alvarez / December 11 - 27, 2026 / …subscriptions…") and drops the "Current Season" list with every show's title. The model names the two events "Now Playing" and "By: Michael Carleton, …", and misses The Merry Wives of Windsor (Nov 13 – Dec 5) and the rest
  - omalleyscincy.com/events/ — 289 characters without the weekly specials board (see "Standing specials")
  - findlaymarket.org/events — `main_text` is the 878-character intro ("Findlay Market hosts more than a dozen events each year… Check out our upcoming events below"); the list below it ("Oct / 31 / October 31, 2026 / Halloween Bites and Delights Tasting Tour", "Trick-or-Treat", "Nov / 1 / Howl-O-Ween Dog Costume Contest", "Nov / 7 / Plants to Plates Vegan Tasting Tour"…) is only in `full_text`. The model returns `{"events": []}`
  - ludlowgaragecincinnati.com/category/shows — `main_text` (7,629 characters) keeps each show's details ("Show Time: 7:30pm / Doors Open: 6:00pm / Ticket Price: $20-$35… / More Details »") but not its title and date ("CANCELED: BLKBOK / Fri / Oct / 30", "Funky Dineva Live / Fri / Nov / 6"); the model has nothing to name or date, and answers `[]` (51 dates in `full_text`, none in `main_text`). See also "Calendar-table venues get nothing"
  - artacademy.edu/events/ — 435 characters of intro; the dated gallery events are only in `full_text`. nkcac.org/events/ — `main_text` is the cookie-consent text (the page has no upcoming events, so nothing is lost there)
  - thecarnegie.com/whats-on/ (all three pages) — `main_text` is the cookie-consent banner, 526 characters ("We use cookies and similar technology…", "Opt-Out Preference Signal Honored…"); `full_text` has the twelve dated shows ("CARRIE: THE MUSICAL / October 23 – November 1, 2026 / Theatre / Buy Tickets", COME FROM AWAY, THE MUSIC MAN…). The model gets only the banner, every page drops as one `missing_start` "What's On Archive" event, and the Carnegie goes out with nothing
- Should be: fall back (or add `full_text`) when `full_text` has many more dates than `main_text`, not only when `main_text` is short. Cookie/consent banners (`#onetrust…`, `[class*=cookie]`, `[id*=consent]`, `role=dialog` with "cookies") should never be the main block
- Where: `scraper/text.py` `main_text` / `ai.page_text`

### FullCalendar feeds aren't read, and their times claim UTC
- Seen: washingtonpark.org/events — the month grid is FullCalendar fed by `/wp-json/v1/events/getEvents?start=&end=` (from `fullCalendar({events: "<url>"})` in the theme JS): plain `[{title, start, end, url, image_url, series_title}]`, every event with its own date and detail link. Its times say `+00:00` but are local (`11:00+00:00` shows as "11a" on the page)
- Should be: a platform adapter: find a FullCalendar `events:` URL (or `eventSources`) in the scripts, request it for the next couple of months, map the standard fields. Read the times as local when the page's rendered grid shows the same clock times as the feed
- Where: new `scraper/platforms/fullcalendar.py` (+ `tests/test_platforms.py`)

### Venue-name tags match inside other words
- Seen while checking the tags above: `_VENUE_NAME_TAGS` substrings tag any event at "Bee's Barbecue" (a source in the DB), "Cincinnati Public Library" or "Barnes & Noble" `21+`
- Should be: match whole words ("bar", "pub", "tap"), not substrings
- Where: `scraper/extractors/tags.py` `_VENUE_NAME_TAGS` and its matcher

### A festival page's lineup across several venues
- Seen: washingtonpark.org/events/2026-fright-fest — one calendar entry (Oct 23, 7–11 PM) for a three-night festival at four venues (Washington Park, Memorial Hall, Ziegler Park, Imagination Alley); only Oct 23 is in the park. Read as one event with the source's venue, the other nights' text would fold into it or become park events
- Should be: lineup lines naming another venue ("October 24th, 7p | Ziegler Park showing …") aren't this source's events; the calendar entry's own date picks its line
- Where: AI extraction prompt (`scraper/extractors/ai.py`) / `dates.extract` on single-event pages

### A page that looks like events by its URL can hold none: private-events (booking) pages
- The rule: an `/events` URL or an "Events" nav link says only that a page *might* list events. Whether it lists public events is decided by the page's content. A page selling private events (book your party here) has to be recognised as that, and the source treated as having no public events
- Done when, for sundryandvice.com:
  - the crawl doesn't keep `/events` in the recipe's `events_urls`
  - the model isn't asked about the page
  - the first run reports the source as having no public events (straight to `no_events`, not after 3 empty runs)
  - no `empty` snapshots of it pile up for review
  - the source is still rechecked now and then, so a venue that starts listing events is picked up
- Tests: a unit test of the page check on this page's saved HTML (private → not an events page) plus a real events page that also mentions private bookings (not flagged); a spider test that such a page gives the "no public events" outcome. A parse fixture with `events: []` alone wouldn't prove it: the parse already finds nothing
- Seen: sundryandvice.com/events (flagged by the owner as a known trap). The nav's only event link, "Events" → `/events`, scores 7 in `score_link` (path `events` +3, anchor +3, nav +1). The page is an inquiry for hosting parties: "We like to think we're pretty good at hosting events. Anything from birthday parties to themed pet adoption events. Get in contact with our event coordinator…", plus "*we will not accept any event plans with dates after June 2026". No public events anywhere on the site (home, about, gallery checked, rendered)
- Also (site capture, 2026-10-28): queencityexchange.com/events (Squarespace, found in its sitemap) — "Let us host your next party! Birthdays, engagements, retirements… No reservation fees!" and a reservation form; no dated entries. The model returns nothing, so the same `empty` path
- What a crawl does: learns `/events` as `events_urls`, asks the model on it every run (a listing always goes to AI, `extraction._extract_steps`; no dates-present check), uploads `empty` snapshots, and only after `EMPTY_RUNS_UNTIL_NO_EVENTS` (3) runs does the backend set the source `no_events`. No bad events: the one date ("June 2026") is in the past, and nothing else has a start
- `_NEGATIVE` (private, book, booking, reservation, hire…) only looks at the link's URL and text, never the page it leads to
- How to tell: booking/inquiry language (hosting events, event coordinator, get in contact, inquire, capacity, packages, private, rental, birthday parties, buyouts) and no upcoming dated entries. Venues often list public events *and* offer private bookings on one page, so booking words alone can't be enough: the absence of upcoming dated events is what decides
- Where: `scraper/discovery/events_page.py` (a page-level check beside `score_link`), the spider's listing callback before `parse_page_async`, the run report's outcome, and the backend's run handling (`scraping/services.py`, `EMPTY_RUNS_UNTIL_NO_EVENTS`) for the immediate `no_events`

### "Nothing at this time" reads as a broken source once a venue has had events
- Seen: afterlifebecurious.com/events-calendar says "Nothing at this time."; the homepage's events section says "No items found." Discovery finds the right page (`/events-calendar`, 5.5); the page is just empty right now. The Archives tabs show the bar does hold events (Halloween, Blok Party, New Years)
- What the backend does with zero events (`scraping/services.py` `_judge`): no baseline → `empty` (3 in a row → `no_events`, recheck in 30 days); a baseline → `failed`: `relearn_requested` (the recipe is thrown away), exponential backoff, and after 3 → `broken`, listed as newly broken by `tools/crawl_summary.py`. So a venue between events looks like a broken scraper
- Should be: recognise an explicit empty state on a known events page ("nothing at this time", "no upcoming events", "no items found", "check back soon", "no events scheduled") and report it (e.g. `empty_state: true` in the run report) as healthy-but-empty: keep the recipe, crawl at the normal interval, no failure count, no `broken`
- Where: the spider's listing callback / run tracker (`scraper/sources/tracker.py`), the run report, `_judge` in the backend

### Standing specials outside the events page are never read (needs a decision)
- Seen: afterlifebecurious.com — the homepage FAQ: "Happy Hour? Every weekday from 4-6PM. Get an additional $1 off if you show your work badge." I entered it as a weekly event (Tue–Fri 4–6 PM, since the bar is closed Mondays); the crawl reads only events pages, so it never sees it. The homepage is fetched anyway for discovery
- Also: omalleyscincy.com/events/ *is* the events page, and it's a weekly board: "MONDAY SERVICE INDUSTRY NIGHT / TUESDAY KARAOKE / WEDNESDAY TRIVIA / …" with drink prices, "All specials run til 9pm". `main_text` is 289 characters without the board (it's in `full_text`), so the model sees none of it and the page drops as one `missing_start` event. Karaoke and Trivia are tags in `REQUIRED_TAGS`
- Decision for the owner: are a venue's standing specials (happy hour, weekly trivia or DJ night stated in its hours/FAQ) events in the app? `Happy Hour` is in `REQUIRED_TAGS`, which suggests yes
- If yes: read recurring specials from the homepage's hours/FAQ text when the source has no events page or it's empty, and intersect "every weekday" with the opening hours: `recurrence.extract` reads "Every weekday" as `MO–FR`, but this bar is closed Mondays
- Not to trust alone: the site's meta description says "live DJ nights every Saturday", which nothing on the site backs up and which has no time; a claim only in `<meta>` shouldn't become an event

### Squarespace: every page is taken for an events collection, so discovery never runs
- Seen: thepenthausotr.com and redtreecoffeeandart.com homepages. `Squarespace.detect` matches because every Squarespace page's script manifest names `"squarespace-events-collection"` (and its `events-collection-…js`), not because the page lists events. `parse_home` hands the homepage to the adapter, `?format=json` has no `upcoming`/`items`, `parse` returns `[]` (not `None`), and the run ends with 0 events. Redtree's nav links `/calendar` (a real events collection, `typeName` `events-stacked`), which is never fetched
- Should be: detect only a page that renders the list (`class="eventlist…"`, or the JSON's `collection.typeName` starting `events`); drop the bare `events-collection` test. Optionally, a platform feed that comes back empty on the homepage falls through to `find_events_pages`
- Where: `scraper/platforms/squarespace.py` `detect`; `scraper/spiders/generic.py` `parse_home` / `parse_platform` for the fallback

### Squarespace event pages: the Previous/Next pager stays in the text the model reads
- Seen: redtreecoffeeandart.com/calendar/*, deeperrootscoffee.com/events-calendar/* — the post pager ("Previous / March 7 / Kaleidoscope / Next / June 13 / Beyond Color") ends the page text
- Fixed (2026-11-15): `finalize`'s explicit-dates pass reads the text without it (`extraction._without_pager`), so its dates no longer become rdates
- Left: `text.main_text` still keeps it, so the model sees the neighbours' titles and dates. Dropping `.item-pagination` there changes every Squarespace page's prompt, so recorded answers stop replaying; do it with a live re-record. A yearless rdate still goes to the next future occurrence instead of the event's own year (`dates._best_year`)
- Where: `scraper/text.py`, `scraper/extractors/dates.py` `_parse_date_list` / `_best_year`

### Events-page discovery: venue-rental pages outrank the shows
- Seen: cincyshakes.com — `find_events_pages` keeps the top 3: Special Events, Event Rentals (`/explore/event-rentals`) and Annual Events (`/support-us/annual-events`), all 5.5. The shows are at `/on-stage` (nav "Buy Tickets", 4.0), never fetched. Special Events parses as one event, "Cincinnati Shakespeare Company", the page itself; Event Rentals drops its one
- Also taken as events pages (each parses as the page itself and drops `missing_start`, using up a listing slot): findlaykitchen.org/host-an-event, myfountainsquare.com/host-an-event, noliakitchen.com/restaurantbuyout, otrfunplex.com/corporate-team-building and /event-space-cincinnati, flightclubdartsusa.com group-events page, tinroofcincinnati.com/group-parties…, and a job ad, thepitchcincy.com/saleseventcoordinator ("Now hiring Sales & Event Coordinator!"). Event-space venues with no public events go the same way: dreespavilion.com "Schedule a Tour" → `/schedule-tour/` (4.0: `schedule` is in `_STRONG` and `_ANCHOR_RE`), belleventcentre.com "Upcoming Events" → `/events/packages/` (7.0) plus `/events/upgrades/` and `/events/corporate/`, immersivegamebox.com "CORPORATE EVENTS" → `/party-packages`, and tomswatchbar.com a press release ("World Cup kicks off in California: tickets and more", 4.0 for "tickets"), whose past World Cup matches drop as `past_event`
- Should be: `rental(s)`, `venue-hire`, `private-events`, `host-an-event`, `buyout`, `corporate`, `group-*`, `event-space`, `hiring`/`coordinator`, `packages`/`upgrades`/`tour`, `press`/`news` and `support-us`/`donate`/`gala` paths are negatives, and "schedule a tour" isn't a schedule (`_NEGATIVE` has `hire` and `private` but not `rental`); theatre words `on-stage`, `onstage`, `season`, `productions`, `now-playing` are strong. A nav link whose children are shows (`/on-stage/<show>` links on the homepage) is a strong sign too
- Where: `scraper/discovery/events_page.py` `_STRONG`, `_ANCHOR_RE`, `_NEGATIVE`, `find_events_pages`
- Related: "private-events (booking) pages" above is the page-content check for when such a page is the only events link; this one is the link scoring that should rank them below the real list

### Detail-link groups: "Add to calendar" .ics links win, and pagination follows a staging host
- Seen: cincinnatiarts.org/events — `links._detail_links` picks the repeated `/events/ical/<slug>/<id>` "Add to calendar" links (each a `text/calendar` download, which Playwright aborts with "Download is starting") over the `/events/detail/<slug>` links. The crawl gets away with it only because no event claims an ical link, so the missed-entry follow (`parse_listing`, `unclaimed`) is switched off. The listing's real missed entries are never followed
- Should be: leave out links to `.ics`/`webcal:`/`/ical/`/`?ical=`/Google Calendar "add" URLs when grouping; better, read them as a feed (each VEVENT is an event with exact times)
- Seen: artacademy.edu/events/ — the listing's "next" link is `https://stg.artacademy.usdphosting.com/events/page/2/`, the site's staging host, which doesn't resolve. `_next_page` only checks `same_site` for anchors matched by text/class, not for `rel="next"`
- Should be: apply `same_site` to the `rel=next` link too, and if it's off-site, try the same path on the listing's own host
- Where: `scraper/discovery/links.py` `_detail_links`, `_next_page`

### Wix event lists stop at the first page ("Load More")
- Seen: jappsotr.com/upcoming-events, nicelifeotr.com/upcoming-events, rosedaleotr.com/upcomingevents (same owner, same Wix Events widget). Each renders 18 events and a `data-hook="load-more-button"`; the inline JSON and the links hold only those 18, so later events (into December and beyond) never reach the crawl
- Should be: click the load-more button while rendering until it's gone (bounded), or read the Wix Events API the widget calls for the full list
- Where: `scraper/rendering.py` (a page action for `[data-hook="load-more-button"]`), or a Wix adapter in `scraper/platforms/`

### Event pages taken for listings when the homepage is the list
- Seen: vintageonrace.com — the homepage shows three event teasers and the site has no index (`/events` is a 404). `find_events_pages` returns the three event pages themselves (`/events/2026/10/fright-night-kickoff` …: "events" segment +3), so each is parsed as a listing (no page-text dates or recurrence for a listing that comes out as one event), and on each the iCal adapter takes the event's "Add to calendar" `.ics` as the site's feed. Weekly events ("Thursday Night Football", "Girls Night Out Wednesdays") lose their recurrence
- Should be: when the candidates are siblings under one parent that isn't itself linked (or 404s), treat the homepage as the listing and follow those links as detail pages. A single-VEVENT `.ics` linked from an event page is that event's calendar file, not a site feed (also americansignmuseum.org: the iCal adapter takes `/event/spooky-tours-2/ical/`, an "add to calendar" link, as the site's feed, and it 404s)
- Where: `scraper/discovery/events_page.py` `find_events_pages` / `_drop_nested`; `scraper/platforms/ical.py` `feed_urls` (`subscribe` links)

### Detail-page titles: a site tagline that isn't the site's name stays
- Fixed (2026-11-15): `cleaners.title.strip_site_suffix` strips trailing segments naming the site's domain ("| Fountain Square" on myfountainsquare.com, "— Redtree Coffee", "| Cincinnati Arts"), a date ("| February 26th") or a call to action ("| Book Your Experience Today", "| Official Ticket Source"); a detail page titled "Page not found" keeps its listing entry (`extraction.NOT_FOUND_TITLE`), and such titles drop as `not_an_event`; an OpenGraph-only title sharing no words with the listing entry's yields to it
- Left: a suffix that names neither the domain nor fits those patterns ("Seasons 52 | Wine Bar & Grill - Seasonal Restaurant…" on seasons52.com keeps the tagline). Stripping what the site's pages' titles have in common needs the listing's own title passed down
- Where: `scraper/cleaners/title.py`

### JSON-LD written in UTC clock time with the local offset
- Seen (site capture, 2026-10-28): cliftonculturalarts.org/ccac-events/* — JSON-LD `startDate` "2026-11-13T04:00:00-05:00" for "Artist Collaborators' Breakfast", whose page says "Friday, Nov 13 / Time / 9:00 am - 11:00 am": the site writes the UTC clock time with the local offset, so every event is five hours early (8 events). "2nd Sunday Showtime" (Nov 8, 2–3 PM) goes out at 9 AM
- Fixed (2026-11-15): JSON-LD on another day than both the page text and the listing entry say loses to the entry (vintageonrace.com's start the day it was fetched); an offset from the other side of a clock change keeps its clock time (`pipelines._without_stale_offset`)
- Left: the page's stated time isn't in `main_text` on cliftonculturalarts.org (it's in the event sidebar), so a check "the text states one start time and the structured one differs by whole hours: trust the text" needs the sidebar's text. A structured `endDate` at exactly local midnight also means the day, not a time
- Where: `scraper/extraction.py` `finalize`, `scraper/text.py`

### "<date>, <time> – <date>, <time>" is read as two dates, not a range
- Seen: igbysbar.com/events/unholy-spirits-halloween-party (Wix) prints "Oct 31, 2026, 4:00 PM – Nov 01, 2026, 2:00 AM"; `dates.extract` on that line returns `start 2026-10-31T00:00, rdates ['2026-11-01T00:00']`, and on the whole text "DJ from 10pm - 2am" gives an end before the start
- Fixed (2026-11-15): `finalize` drops explicit dates inside the event's own start–end run (`extraction._other_days`), so overnight events no longer get a second night and a play's themed nights aren't extra occurrences; one read without a time takes the start's time
- Left: `dates.extract` should read the line as one start–end range, and "10pm - 2am" as an end the next day, so a page with no structured dates gets the end right
- Where: `scraper/extractors/dates.py` `_parse_date_list`

### Eventbrite "Multiple dates" series become one event spanning years
- Seen: bloomotr.com links eventbrite.com/e/bloom-otr-sunday-drag-brunches-tickets-488987092557 (`"isSeries":true`, `"seriesId":"488987092557"`, "Multiple dates"). JSON-LD gives the series' span, so the crawl sends one non-recurring event from 2023-01-01 11 AM to 2026-12-27 1:30 PM, nearly four years long; nothing in its text matches a recurrence pattern ("Sunday Fudays", "Sunday Drag Brunches" are singular)
- Should be: for a series page, read its occurrences (Eventbrite's series endpoint / the page's embedded child events) into a weekly rule with rdates/exdates, or at least drop past occurrences and keep the next one. An event whose span is weeks long, with a weekday in its title and a start and end on that same weekday, is a weekly series, not a multi-week event
- Where: `scraper/platforms/eventbrite.py` (no series handling at all), `scraper/extractors/recurrence.py` (singular weekday in a title + long span)

### Wix Events: the event's slug isn't used, so repeated titles lose their links
- Seen: jappsotr.com/upcoming-events — the Wix inline JSON has a `slug` per event (`live-music-2026-11-07-19-00`, …) and the page links each as `/events/<slug>`. `inline_json` doesn't read `slug`, so `url` comes only from `event_links.attach` matching titles; the 7 "Live Music" nights all go out with no `url`, because their title is also the start of "Live Music by the Burning Caravan" and the nearest link is ambiguous. (11 of 18 events get a link)
- Should be: for Wix Events, `url` = the listing's `/events/<slug>` when that link is on the page (or the JSON's own `eventPageUrl`); prefer it over title matching
- Where: `scraper/extractors/inline_json.py` (the Wix `scheduling` lifting already lives there)

### Month-grid calendars: the model's day is off by one, and the detail page doesn't correct it
- Seen: cincinnatiarts.org/calendar — the page text is a month grid ("…29Thursday\n30Friday\nGil Shaham Plays Tchaikovsky\n11:00am…"). The model dates the 11 AM concert 2026-10-29 (it's Friday the 30th); its `evidence` ("Gil Shaham Plays Tchaikovsky\n11:00am") holds no date, so nothing checks the day
- The detail page (/events/detail/gil-shaham-plays-tchaikovsky) lists "Performance Times: October 30, 2026 11:00AM / October 31, 2026 7:30PM". With the listing partial supplying a start, the model isn't asked and `finalize`'s explicit-dates pass only adds `rdates` (Oct 31 at 00:00, losing 7:30 PM). The event goes out on the wrong day
- Worse (site capture, 2026-10-28): cincyplay.com/calendar. `full_text` has the grid's heading ("Calendar / October 2026 / October 2026November 2026…"); `ai.page_text` (`main_text`) starts at "Sun Mon … Thu 1 - 7:30 P.M. HEIST - 7:30 P.M. TALK …" with no month at all. The model places the grid in November: "Thu 1" becomes Sunday Nov 1, so all 24 kept events are a month late (HEIST, which closed Oct 11, goes out Nov 1–11; GET HAPPY's real Oct 17–31 run, including this week's shows, goes out Nov 17 – Dec 1). The other 26 entries drop as `ai_unverified` only because their evidence ("Thu 1 - 7:30 P.M. TALK") skips the HEIST line between them; the evidence check never compares the weekday with the date
- Also Timely (`platforms.widgets.Timely` renders the iframe's month view and leaves it to the model): esquiretheatre.com's `calendar.time.ly/e46vxi0c/month` reads "October 2026 … | Sunday | Monday | … 3 1 | 3 2 | 4 3 | … ESQ- CENTRAL STATION (1998) 7:30pm ESQ- Michael Mann's MANHUNTER…": each cell's event count is glued to its day number ("3 1" = three events on the 1st), the cells run together, and the heading says October over November's first week. The model dates 38 films; 13 have no time (00:00) and the day of others can't be checked. Worth checking whether the widget's own events request (not in the saved HTML) can be read instead, as for FullCalendar
- Should be: (1) for grid text, keep the grid's month/year heading in the page text, ask the model to quote the day number in `evidence` and check it (a weekday in the evidence must match the date's weekday), or split the grid into "date: entries" lines before asking; (2) on a detail page, a dated performance list overrides the partial's start when the partial's date isn't among its dates, and each listed date keeps its own time
- Where: `scraper/extractors/ai.py` (prompt/evidence check), `scraper/extraction.py` `finalize` (partial vs page dates), `scraper/extractors/dates.py` `_parse_date_list` (times per date)

### Real events dropped as `ai_unverified`: evidence is only checked against `main_text`
- Seen: ghost-baby.com (calendar) — `main_text` (trafilatura) runs the grid together without spaces ("…28Alex Merk Trio7:00 PM - 8:00 PMAlex Merk Trio9:00 PM…"); the model's evidence "Alex Merk Trio 7:00 PM - 8:00 PM" is right but doesn't match, so 4 real sets are dropped. motrpub.com/shows/motr-open-mic-w-* — the evidence is the title, which is in `full_text` (the page heading) but not `main_text`: 3 open mics dropped
- Checked: matching against `fold(full_text)` as well keeps all 7. Others still fail, and some of those are real too: ensemblecincinnati.org/calendar is a month grid ("28 / 7:30 PM / Mrs. Mannerly / ASL … 29 / 7:30 PM / Mrs. Mannerly … 31 / 2:00 PM / Mrs. Mannerly"); the model's evidence "October 28 7:30 PM Mrs. Mannerly" is a fair reading of it but not verbatim, so all five performances (Oct 28–31) drop. Same for a grid day number glued onto an entry on mrpitifulsotr.com ("DJ Patrick", that night)
- Following doesn't rescue them: unverified entries are still followed (they're in `events`; the pipeline drops them later), and `drop_reason` travels in the partial. `finalize` fills every key the page lacks from the partial, `drop_reason` included, so the detail page's event is dropped whatever it finds. CAC's "Indigenous Peoples' Day Convergence" page has a complete JSON-LD Event and still drops `ai_unverified`; the five Mrs. Mannerly ticket pages, the Cincinnati Zoo's "Jack O'Lantern Glow" and DJ Patrick go the same way
- Should be (following): never carry `drop_reason` (or `evidence`) in a partial; a detail page that yields its own event decides. For grids, see "Month-grid calendars": ask for evidence as "day number + time + title" and check it against the grid's cells
- Should be: accept evidence found in the folded main text or the folded full text
- Where: `scraper/extractors/ai.py` where `haystack = fold(text)` is built; `scraper/extraction.py` `finalize` (the partial loop) or `spiders/generic.py` `_emit_listing_events` (strip `drop_reason` before using an entry as a partial)

### Wix CMS items: the date field is missed, so time-only starts are dated tomorrow
- Seen: studiokroner.com/event/erika-nj-allen — each CMS item has `"eventTitle"`, `"startDate1":"2026-10-07"`, `"endDate1"`, and time-only `"startTime":"18:00:00.000"`/`"endTime"`. `inline_json` reads the times but not `startDate1` (custom field names get a numeric suffix), so the Opening Reception (Oct 7, 6–9 pm), Artist Talk (Oct 31, 1–2 pm) and Closing Reception (Nov 1, 1–3 pm) all go out on 2026-10-29, the day after capture; the past opening isn't dropped
- Also: the collection's schema (`{"displayName":"Start Time","type":"time",…}` field definitions) is taken for an event whose title is a dict's repr; it's only dropped because its start (another dict) doesn't parse (`unparseable_start`)
- Should be: match date/time keys ignoring a trailing digit (`startDate1`), and combine a date-only field with a time-only field from the same item; a time-only start with no date anywhere in the item is not an event start. Skip objects whose values are field definitions (`displayName` + `type`), and require string titles
- Where: `scraper/extractors/inline_json.py`

### Undated listing entries are dated "today" (by the model, and by Normalize)
- Seen (site capture of 2026-10-28, 356 model events with a start): 57 have `evidence` with no date in it, and 28 of those are dated exactly the capture day: all nine deeperrootscoffee.com events-calendar entries (DRC Day, Farm to Cup, Pilates at the Roastery…: the summary block shows no dates), redtreecoffeeandart.com, motrpub.com, ghost-baby.com
- Not only "today": oddfellowsbar.com/events and /cincinnati-events (Squarespace) still show events from February 2024 with no dates on the list. The model's `evidence` is just the title ("Detroit Techno Militia & friends") and it invents future dates: Dec 5, Oct 31, Nov 7 8 PM… (6 events per page). The event pages' JSON-LD says 2024-02-17, 2024-02-13, 2024-02-06, so they drop as `past_event` when followed; with an end time on the list they wouldn't have been followed, and the invented dates would ship
- Month-only: highgrainbrewing.com/events describes annual events ("Every June / Anniversary Party", "Every September, at both taprooms / Oktoberfest", "Every October / Ohio Fresh Hop Festival", "Every spring / Dank Fest"). The model makes each the 1st of the next such month (2027-06-01, 2027-09-01, 2027-10-01, 2027-03-01 at midnight), so four invented dates ship
- Or it fills the field with junk: musicartdance.org/events — seven undated entries come back with `start_datetime: "+"` (GasLight Opera, OLEA Ensemble, Red Door Quartet…); cincinnatizoo.org's "Breakfast with Santa" with `"{FUTURE_DATE_NOT_PROVIDED}"`. They drop as `unparseable_start`, and since the start isn't empty, `follow.detail_reason` doesn't treat them as `required`: the event page that has the date is followed only if budget allows a `soft` follow
- When the model does the right thing and returns a bare time (10 cincinnatiarts.org/events cards, "Event Starts 7:30 PM" → `"19:30"`), `finalize` keeps "19:30" and `NormalizePipeline._parse_date` hands it to dateparser (`PREFER_DATES_FROM: future`), which makes it the next 7:30 PM: David Sedaris goes out 2026-10-28 7:30 PM (its page says "Date Nov 21, 2026"), the 2 PM and 1 PM shows go out tomorrow
- Effect: wrong dates that look plausible, and because the listing entry now has a start, the detail page (which has the right date) can't correct it: the start is not `required` to follow, and on the detail page the partial's start blocks the page's own date (see "Month-grid calendars" above)
- Fixed (2026-11-15): a model start whose evidence has no date (nor a weekday or relative day) is dropped when it's a placeholder: the capture day, or the 1st of a month at midnight (highgrainbrewing.com's "Every June"); `ai._check_date`
- Left: other dateless starts (oddfellowsbar.com's invented Dec 5, Oct 31…), junk starts ("+") and bare times ("19:30" dated by Normalize)
- Should be: a model start whose `evidence` has no date (`dates.find_dates`, weekday or relative words) keeps only its time: the date is unknown, so the entry is followed as `required` and the detail page supplies it. Tell the model to leave the date out rather than assume one. Normalize must never date a start that has no date in it: treat it as missing (`missing_start` unless a detail page fills it)
- Where: `scraper/extractors/ai.py` (after the evidence check; prompt), `scraper/follow.py` `detail_reason`, `scraper/pipelines.py` `NormalizePipeline._parse_date`

### A guessed link's detail page is thrown away when the listing title is wrong
- Seen: cincyshakes.com/on-stage — the listing event "Now Playing" (Oct 9–31; a section heading taken as the title, see "The page text the model reads drops most of a dated list" above) gets the guessed link `/on-stage/dracula-a-comedy-of-terrors-2026`. That page's JSON-LD is a complete TheaterEvent, "Dracula: A Comedy of Terrors", Oct 7 7:30 PM → Oct 31. `extraction._same_event` keeps a guessed page's events only when the title shares half the listing title's words, so it discards the page and `finalize`s the listing entry alone: the crawl sends "Now Playing", 2026-10-09 00:00 → 10-31. Same for "By: Michael Carleton, James FitzGerald, and John K. Alvarez" → `/on-stage/every-christmas-2026` (Every Christmas Story Ever Told)
- Should be: also accept the page's event when its dates agree with the entry's (same start day, or the entry's span inside the page's), and then take the page's title. Only fall back to the entry when neither title nor dates match
- Where: `scraper/extraction.py` `_same_event`, `scraper/discovery/event_links.py` `same_event`

### An event at another venue gets the source's address and map pin
- Seen (site capture, 2026-10-28): cincinnatiarts.org is seeded as Music Hall (1241 Elm Street). Its events at the "Aronoff Center, Procter & Gamble Hall", "Aronoff Center - Jarson-Kaplan Theater" and "Weston Art Gallery" (all 650 Walnut St) come out with `location_address` "1241 Elm Street" and Music Hall's lat/lon (21 events). myy.org (seeded as Central Parkway YMCA, 1105 Elm Street) — events at Clippard Family, Powel Crosley Jr., Blue Ash and Highland County YMCAs get 1105 Elm Street; "Firecracker 5K" keeps its own "801 Cochran Ave., Ft. Thomas, KY" but still gets Central Parkway's lat/lon (14 of 15 events on /july-events/ are pinned in OTR)
- Also esquiretheatre.com's Timely calendar (`calendar.time.ly/e46vxi0c/month`), shared by two cinemas: titles carry the house code ("ESQ- THE MATRIX (1999)", "KWD- THE METROPOLITAN OPERA: COSÌ FAN TUTTE"), and the Kenwood Theatre's screenings ("KWD-") get the Esquire source's venue and pin like the rest
- `extraction.finalize` fills each missing key from the source context on its own (`PER_EVENT_KEYS` holds only title/start/end), so a `location_title` from the page is paired with the context's address and coordinates
- Should be: fill `location_address`/`location_lat`/`location_lon` from context only when the event has no `location_title` or its title names the context venue (fuzzy: "Nice Life OTR" ≈ "Nice Life", "Japp's - Since 1879" ≈ "Japp's" are the same place and should keep the fill). Coordinates should never be filled when the event has its own address. An event at another venue with no address goes out without coordinates (or is geocoded), not at the source
- Where: `scraper/extraction.py` `finalize` (the context loop), `PER_EVENT_KEYS`

### A finished weekly schedule, and a weekday that doesn't fit the model's year
- Seen: myy.org/july-events/ — "Workout on the Green at Washington Park — Wednesdays through July 8" follows the weekday rule and starts 2026-10-29, months after its stated end. friescafeclifton.com/calendar-of-events.html (a stale page, "© 2013") says only "Friday, April 26th - Lisa De Novo Duo"; the model answers 2027-04-26, a Monday
- Fixed (2026-11-15): yearless dates on a dated post take the post's year (`ai._check_date`: the page's latest published/modified date, when its text names no later year, the evidence has a yearless date and any weekday in it fits), so myy.org's July events drop as past
- Left: a weekly schedule with an end ("through July 8") that's already behind us is over; check the model's start against a weekday in its `evidence`, and when the nearest matching year is past, the event is past
- Where: `scraper/extractors/ai.py`, `dates._best_year` for the non-AI path

### Sitemap discovery returns the same URL several times, and event pages instead of their index
- Seen (site capture, 2026-10-28): purplepaisleyartisanshop.com (WordPress, no events link on the homepage). Its sitemap index includes `image-sitemap-1.xml`, which lists each page once per image (the homepage 95 times). `events_urls_from_sitemap` sorts and slices without de-duplicating, so all three picks are `/events-2/soulcollage/` (brewdcincy.com: `/events/` three times): one class page fetched as the site's only listing, and learned three times as `events_urls`
- Underneath: the sitemap has dozens of `/events-2/<class>/` pages (each 1.5) but not `/events-2/` itself, and `_drop_nested` only collapses a child into a parent that's also in the list. Same shape as vintageonrace.com ("Event pages taken for listings when the homepage is the list")
- Should be: de-duplicate `locs` (or the scored list) before slicing. When several picks are siblings under one parent path, try the parent (`/events-2/`) as the listing and fall back to the siblings as detail pages
- Where: `scraper/discovery/events_page.py` `events_urls_from_sitemap`, `_drop_nested`; `scraper/discovery/sitemap.py` `SitemapRead.add` (could skip image/video sitemaps for discovery)

### A listing entry's own ticket link isn't taken from the HTML
- Seen (site capture, 2026-10-28): woodwardtheater.com/shows — every entry has its own "Buy Tickets" link (`https://www.cincyticket.com/eventperformances.asp?evt=7894`, …). The model only gets the page text, so it can't give them
- Fixed (2026-11-15): a model's `ticket_url`/`rsvp_url` that isn't an href on the page, or is the page itself, is dropped (`ai.extract_many`); an event page's own ticket link (`extraction.ticket_link`: says "Tickets", leaves the site or names tickets in its path, outside header/nav/footer, not a promotion for another event) fills `ticket_url`, and replaces a listing entry's link back into the site
- Left: on a listing, within an entry's block (the element holding its detail link), an anchor saying tickets or going to a ticketing site is that entry's `ticket_url`, so entries that aren't followed get one too
- Where: the listing's per-entry link matching (`scraper/discovery/event_links.py`)

### A homepage that lists the events is never read when discovery finds any other page
- Seen (site capture, 2026-10-28): braxtonbrewing.com (Shopify). The homepage has the site's only event list: nine dated entries with Facebook event links ("Bark at the Brew / Factory 52 / Thu, October 29, 2026 at 8:00pm", "Trunk or Treat", "The Witching Hour", trivia nights…). Its "Events" nav link is `store.braxtonbrewing.com/pages/private-events` (rightly scored down by `private`), so `find_events_pages` finds nothing and the spider reads the sitemaps. `events_urls_from_sitemap` picks `/pages/events` ("Book Your Next Event at The Barrel House…", a booking page) and `/collections/holiday-events` (a shop collection); both drop as `missing_start`, and the crawl ends with 0 events. The homepage fallback in `_discovered` only runs when the sitemaps and Claude both come up empty. Parsing the homepage as the listing gives all nine (case `review/inbox/braxtonbrewing-com-every-wednesday-starting-9-23-at-8pm`)
- Also there: each entry's link is to facebook.com/events/…, so no event gets a `url` (off-site links aren't detail links), and the locations are Braxton's taprooms ("Covington", "Union", "Factory 52"), which get the Barrel House's address (see "An event at another venue gets the source's address and map pin")
- Should be: when the homepage itself has several dated entries (`dates.find_dates` on its text, or repeated blocks with dates), parse it as a listing alongside whatever discovery picks, and keep whichever yields events in the recipe. A page found by discovery that comes back with no events shouldn't end the run while the homepage has dates
- Where: `scraper/spiders/generic.py` `parse_home` / `_discovered`; `scraper/discovery/events_page.py`

### Listing entries that share one detail page: all but the first are lost
- Seen (site capture, 2026-10-28): sunsetplayers.org/product-category/current-season/ (WooCommerce). The model returns each performance as its own event ("House at Pooh Corner" Dec 4, 5, 6, 11, 12, 13 from "December 4, 5, 6, 11, 12, and 13"; 18 for three shows), all with the show's one product URL. Each is `soft` (`follow.detail_reason`: no end), so `_emit_listing_events` yields a detail request per entry with that entry as the partial and `continue`s past `build_item`. The six requests are the same URL and `_request` doesn't set `dont_filter`, so Scrapy's dupefilter drops five without a callback or errback: the crawl sends Dec 4 only, 3 events where there are 18. Each dropped request still counts in `run.scheduled_details`, using up the detail budget. Across the capture: 23 entries (sunsetplayers.org 15, esquiretheatre.com 4, cincinnatiarts.org 4; platform feeds excluded)
- And the one that survives loses its time: the product page lists "December 4 @ 7PM … December 12 @ 2PM", but the partial's date-only start (Dec 4 00:00) means the model isn't asked and the explicit-dates pass doesn't replace it
- Should be: group the listing's entries by detail URL and follow each page once, carrying all its entries; on the page, match each entry to its own date there (taking the page's times) and emit the entries the page doesn't mention from the listing. A partial start with no time shouldn't block a time the page gives for that same day
- Where: `scraper/spiders/generic.py` `_emit_listing_events` / `parse_event`, `scraper/extraction.py` `finalize` (partial vs page)

### "Every night" / "daily" anywhere in the text makes an event repeat daily
- Seen (site capture, 2026-10-28):
  - smokejustis.com/events/* (Squarespace event pages) — the site footer says "Our kitchen closes 1 hour before closing every night." `finalize` finds no recurrence in the event's own title/description, tries `full_text` (`rec_from_page`), and the strong pattern `every\s+(day|night)` matches the footer: "Stephanie Coffey" (Thu Oct 29, 7–10 PM), "Linus Tate", "Lee Dixon & Friends" all go out `daily`
  - cliftonculturalarts.org — "Functional Breathing Workshop" (Sat Nov 7, one session) is `daily` because its description says "You'll leave with your baseline score, a short daily practice and specific tools…"
- Should be: whole-page text is a weak source: a schedule phrase there counts only when it's in the same block as the event's title/date (not the footer, hours or nav). In a description, "daily" as an adjective of something else ("a daily practice", "daily specials") isn't the event's schedule; a daily event also needs no explicit single date ("Thursday, October 29, 2026") on its own page
- Where: `scraper/extractors/recurrence.py` (the `nightly|daily|every (day|night)` pattern, `_LEAD`), `scraper/extraction.py` `finalize` (`rec_from_page`)

### OSM's multi-valued address tags go into the address as written
- Seen (site capture, 2026-10-28, 3 of 569 OSM venues within 10 km): `addr:housenumber=3515;3516` → "3515;3516 Edwards Road, Cincinnati, OH, 45208" (Arthur's Cafe), "5945;5959 Kellogg Avenue" (Dead Low Brewing), and "3702;2900 Drake Avenue;Wasson Road" (The Establishment, a corner building with two streets). Every event from these sources carries that address, and geocoding or map links with it fail or land oddly
- Should be: split `;`-separated `addr:*` values and use the first housenumber with the first street (or the range "3515–3516 Edwards Road" when the street is one)
- Where: `scraper/seeds/overpass.py` (the addr:* assembly, ~line 242)

### Weekly events start on a day their own rule excludes (the model's "next occurrence" is guessed)
- Seen (site capture, 2026-10-28; 13 weekly events on 7 sites start on a weekday not in their `byday`): arcadelegacyohio.com/events — evidence "Every Wednesday" → the model's start is 2026-10-30 (a Friday), "Every Thursday at 7pm" → Oct 31 (a Saturday), "Every Thursday at 7 & 7:30pm" → Oct 31; the capture day was Wed Oct 28. `recurrence.extract` then reads `weekly WE`/`TH` from the evidence, so each series starts on the wrong day. Also motrpub.com "Kick Out The Jams Karaoke Mondays" (starts a Saturday), cliftonculturalarts.org's summer concerts (`WE`, starts a Thursday), bloomotr.com's drag show (`TH FR SA`, starts a Sunday)
- The prompt asks the model to work out "its next occurrence on or after today's date"; date arithmetic is where it slips, and `dates.next_occurrence(byday, clock, tz)` already does it exactly
- Should be: when the evidence is a weekly schedule (no explicit date) and a `byday` was read, compute the start with `dates.next_occurrence` and keep only the model's clock time; whenever a weekly start's weekday isn't in its `byday`, move it to the next day that is
- Where: `scraper/extraction.py` `finalize` (after recurrence is read), `scraper/extractors/ai.py` prompt

### Calendar-table venues get nothing: the cells' contents are dropped and their links don't look like events
- Seen (site capture, 2026-10-28): ludlowgaragecincinnati.com/calendar (and `?mon=11&yr=2026`, `?mon=12&yr=2026`), a WordPress month table: `<td class="cell"><div class="date">7</div><div class="band"><a href="/vanilla-fudge-2/" title="Vanilla Fudge - Click for more information!"><img …>Vanilla Fudge</a></div></td>`. `main_text(tables=True)` (trafilatura) renders the table as "| SUN | MON | … 1 | 2 | 3 …" with each cell's day number only, and drops the "«October 2026»" heading: the model gets an empty grid and answers `{"events": []}` on all three months. `full_text` has every show ("7 / Vanilla Fudge / 8 / Melissa Manchester / … 18 / King's X …", 25+ in October)
- The missed-entry follow can't help: the 42 show links are root-level post slugs (`/vanilla-fudge-2/`, `/demola/`), not under `/calendar` and without an event word, so `links._detail_links` rejects the group (`looks_like_event`, `MIN_EVENTISH`) and finds none. The crawl gets 0 events from one of the city's main music venues
- Should be: render calendar tables as "<month year> <day>: <entries>" lines (or fall back to `full_text`, see "The page text the model reads drops most of a dated list"). In link grouping, a group of links sitting in cells beside day numbers (a `<td>`/grid cell holding a 1–31 number) is a calendar's entries, whatever their paths look like
- Where: `scraper/text.py` `main_text` (table rendering), `scraper/discovery/links.py` `_detail_links` / `looks_like_event`
- Cases: `review/inbox/ludlowgaragecincinnati-com-*`

### Image galleries are followed as event pages (WordPress upload paths look dated)
- Seen (site capture, 2026-10-28): musemtlookout.com/events/ is a photo grid of past flyers linking to the full-size images (`/wp-content/uploads/2019/04/38799389_…_n.jpg`). The model finds no events, so the missed-entry follow takes the repeated links: `links.looks_like_event` matches `_EVENT_PATH`'s `/20\d\d[/-]\d\d` on the upload folder, the group passes `MIN_EVENTISH`, and the crawl fetches 12 JPEGs as detail pages (nothing comes of them). Same on thegrowlerhouse.com: 7 logos and photos under `/events/assets/*.jpg`, "eventish" because they sit under the listing path. 19 image fetches in the capture, each using the detail budget
- Should be: skip links to files (`.jpg/.jpeg/.png/.gif/.webp/.pdf`, `/wp-content/uploads/`) when grouping detail links. The flyers themselves are the events here; reading them would need the images (out of scope for links)
- Where: `scraper/discovery/links.py` `_detail_links` / `looks_like_event`

### A series' yearless end date is put in next year, so finished series run another year
- Seen (site capture, 2026-10-28): thecarnegiecenter.org/programming-events — "Kids Chess Club: Summer Edition", evidence "Every Wednesday, June-August, 1-3pm", goes out `weekly WE` from 2026-06-03 with no end: a summer club that ended in August repeats every Wednesday forever (and its June start keeps it from dropping as past)
- `recurrence.extract` on variants (run 2026-10-29):
  - "Every Wednesday until August 26" → until **2027**-08-26; "Wednesdays June 3 – August 26" → 2027-08-26; "Fridays Sep 18 – Oct 23 at 7pm" → **2027**-10-23. A yearless end is taken as its next future date, not the one after the series' start, so a series that ended last week runs on for a year
  - "Every Wednesday, June 3 - August 26, 1-3pm" and "Every Wednesday, June-August" → no end at all; "Wednesdays through July 8" → no recurrence
- Should be: a yearless `until` takes the first such date on or after the series' own start (from the same text, or the event's start); a month-only end ("June-August", "through August") ends on that month's last day; then a series whose end is past is past
- Where: `scraper/extractors/recurrence.py` (`_UNTIL`, `_RUN_END`, the until date's year), `scraper/extractors/dates.py` year choice

### Team schedules: home games get the wrong stadium, and kickoffs not yet set get placeholder times
- Seen (site capture, 2026-10-28): gobearcats.com (seeded as OSM "UC Baseball Stadium") — home football games ("Cincinnati vs. Utah") are at Nippert Stadium but go out at "Cincinnati, Ohio" with the baseball stadium's pin. Some JSON-LD starts are odd clock times ("2026-11-28T16:16:00Z", 11:16 AM; "16:14", "16:15") that look like the site's placeholder for a kickoff not yet set
- Fixed (2026-11-15): on a listing with "<team> vs. …" entries, "<team> at …" ones are road games and drop as `away_game` (`extraction._away_games`)
- Left: a road game listed without a "vs." sibling; the placeholder kickoff times ("time TBA": keep the date, drop the time); the home venue
- Where: `scraper/extraction.py` `finalize_page`, `finalize` (context fill)

### Wix third-party calendar apps never render, so their pages read as empty
- Seen (site capture, 2026-10-29): mariemontplayers.com/calendar-1 (a community theatre, "Arsenic and Old Lace", "A Tuna Christmas"… in its nav). The page's Wix app list names "Google Calendar" / "Google Event Calendar", but the rendered HTML (Playwright, `networkidle`) has no `<iframe>` at all and no calendar ID: the app's widget mounts later (likely when scrolled into view). The text is nav and footer only (3,611 characters), the page drops as one `missing_start` "Calendar | Mariemont Players", and the theatre gets 0 events. `platforms.widgets` only finds iframes already in the HTML (Timely, Tockify…), so it can't help
- Not checked: what the widget would load (its Google Calendar's public `.ics` would be the ideal feed)
- Should be: before reading a rendered page, scroll it to the bottom and wait, so lazily mounted widgets appear; then let the widget detectors see their iframes (a Google Calendar embed `calendar.google.com/calendar/embed?src=<id>` maps straight to the `ical` adapter's `…/ical/<id>/public/basic.ics`)
- Where: `scraper/rendering.py` (Playwright page methods), `scraper/platforms/widgets.py`

### Google Calendar embeds with a base64 calendar ID give a dead feed URL
- Seen (site capture, 2026-10-29): stowehousecincy.org (Harriet Beecher Stowe House) embeds `calendar.google.com/calendar/embed?…&src=aGJzaG91c2UxODUyQGdtYWlsLmNvbQ&…`. Google's newer embed code writes `src` base64-encoded (it decodes to `hbshouse1852@gmail.com`). `ical._google_feeds` puts it into `…/calendar/ical/aGJzaG91c2UxODUyQGdtYWlsLmNvbQ/public/basic.ics`, which is a 404; `…/ical/hbshouse1852%40gmail.com/public/basic.ics` returns 200
- Should be: when a `src` has no `@` and base64-decodes to something with one (`…@gmail.com`, `…@group.calendar.google.com`), use the decoded ID
- Where: `scraper/platforms/ical.py` `_google_feeds` (+ a test in `tests/test_platforms.py`)
