"""
The event item and the one list of its fields: what a spider fills
(EVENT_FIELDS), what goes to the backend (PAYLOAD_FIELDS), and the defaults
for recurrence and list fields. Add a field here, then in EventItem.
"""
import copy

import scrapy


class EventItem(scrapy.Item):
    # ── Core fields (sent to the API) ─────────────────────────────────────────
    title           = scrapy.Field()
    description     = scrapy.Field()
    start_datetime  = scrapy.Field()   # ISO-8601 string
    end_datetime    = scrapy.Field()   # ISO-8601 string or None
    location_title  = scrapy.Field()
    location_address = scrapy.Field()
    location_lat    = scrapy.Field()
    location_lon    = scrapy.Field()
    ticket_price    = scrapy.Field()   # float (USD) or None
    ticket_url      = scrapy.Field()
    url             = scrapy.Field()   # event's own website
    image_url       = scrapy.Field()   # remote image to download server-side
    tag_names       = scrapy.Field()   # list[str] matched against Tag.name

    # ── Recurrence fields (match backend Event model) ─────────────────────────
    recurrence_freq       = scrapy.Field()  # 'none'|'daily'|'weekly'|'monthly'|'yearly'
    recurrence_interval   = scrapy.Field()  # int, default 1
    recurrence_byday      = scrapy.Field()  # list[str] e.g. ["MO","TH"] or ["3TU"]
    recurrence_month_mode = scrapy.Field()  # 'day' | 'weekday'
    recurrence_until      = scrapy.Field()  # 'YYYY-MM-DD' or None
    recurrence_count      = scrapy.Field()  # int or None

    # ── Explicit occurrence overrides ────────────────────────────────────────
    rdates  = scrapy.Field()   # list[str]  ISO-8601 starts of additional occurrences
    exdates = scrapy.Field()   # list[dict] [{"datetime": str, "reason": str}]

    # ── Scraper metadata (not forwarded to API) ───────────────────────────────
    source_url          = scrapy.Field()   # listing/detail page that was scraped
    listing_url         = scrapy.Field()   # the listing a detail page was reached from, if it was
    fingerprint         = scrapy.Field()   # '<source>:<sha256>' — backend upsert key (set by pipeline)
    extraction_method   = scrapy.Field()   # 'jsonld' | 'inline_json' | 'opengraph' | 'recipe' | 'platform:<name>' | 'ai'
    source_id           = scrapy.Field()   # backend Source UUID, when crawled from one
    timezone            = scrapy.Field()   # IANA zone for naive page datetimes, e.g. 'America/New_York'
    confidence          = scrapy.Field()   # 0–1, set by ValidatePipeline
    review_required     = scrapy.Field()   # bool, set by ValidatePipeline
    evidence            = scrapy.Field()   # verbatim date snippet (AI extractions)
    drop_reason         = scrapy.Field()   # set when an extractor already knows the item is bad
    ingest_status       = scrapy.Field()   # 'created' | 'updated' | 'unchanged' | 'failed' (APISubmitPipeline)


# A recurrence field left unset means this (the backend Event model's defaults).
RECURRENCE_DEFAULTS = {
    'recurrence_freq': 'none', 'recurrence_interval': 1, 'recurrence_byday': [],
    'recurrence_month_mode': 'day', 'recurrence_until': None, 'recurrence_count': None,
}
LIST_FIELDS = ('tag_names', 'rdates', 'exdates')

# The fields a spider fills from an extracted event; the rest are set by the pipelines.
EVENT_FIELDS = (
    'title', 'description', 'start_datetime', 'end_datetime',
    'location_title', 'location_address', 'location_lat', 'location_lon',
    'ticket_price', 'ticket_url', 'url', 'image_url', 'tag_names',
    *RECURRENCE_DEFAULTS,
    'rdates', 'exdates', 'timezone', 'evidence', 'drop_reason',
)

# Sent to the backend's upsert (APISubmitPipeline). `url` falls back to source_url,
# and `fingerprint` goes as source_fingerprint.
PAYLOAD_FIELDS = (
    'title', 'description', 'start_datetime', 'end_datetime',
    'location_title', 'location_address', 'location_lat', 'location_lon',
    'ticket_price', 'ticket_url', 'url', 'image_url', 'tag_names',
    'source_id', 'source_url', 'listing_url', 'extraction_method', 'confidence', 'review_required',
    *RECURRENCE_DEFAULTS, 'rdates', 'exdates',
)


def default(name: str):
    """A field's value when unset: its recurrence default, [] for a list field, else None."""
    return [] if name in LIST_FIELDS else copy.copy(RECURRENCE_DEFAULTS.get(name))


def event_item(data: dict, *, source_url: str, source_id=None, listing_url=None) -> EventItem:
    """An extracted event as an item; private (`_`) and unknown keys are left behind."""
    item = EventItem(source_url=source_url, source_id=source_id, listing_url=listing_url or None,
                     extraction_method=data.get('extraction_method', 'unknown'))
    for name in EVENT_FIELDS:
        item[name] = data.get(name)
    return item
