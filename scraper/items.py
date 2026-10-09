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
    fingerprint         = scrapy.Field()   # '<source>:<sha256>' — backend upsert key (set by pipeline)
    extraction_method   = scrapy.Field()   # 'jsonld' | 'inline_json' | 'opengraph' | 'recipe' | 'platform:<name>' | 'ai'
    source_id           = scrapy.Field()   # backend Source UUID, when crawled from one
    timezone            = scrapy.Field()   # IANA zone for naive page datetimes, e.g. 'America/New_York'
    confidence          = scrapy.Field()   # 0–1, set by ValidatePipeline
    review_required     = scrapy.Field()   # bool, set by ValidatePipeline
    evidence            = scrapy.Field()   # verbatim date snippet (AI extractions)
    drop_reason         = scrapy.Field()   # set when an extractor already knows the item is bad
    ingest_status       = scrapy.Field()   # 'created' | 'updated' | 'unchanged' | 'failed' (APISubmitPipeline)


# The fields a spider fills from an extracted event; the rest are set by the pipelines.
EVENT_FIELDS = (
    'title', 'description', 'start_datetime', 'end_datetime',
    'location_title', 'location_address', 'location_lat', 'location_lon',
    'ticket_price', 'ticket_url', 'url', 'image_url', 'tag_names',
    'recurrence_freq', 'recurrence_interval', 'recurrence_byday',
    'recurrence_month_mode', 'recurrence_until', 'recurrence_count',
    'rdates', 'exdates', 'timezone', 'evidence', 'drop_reason',
)


def event_item(data: dict, *, source_url: str, source_id=None) -> EventItem:
    """An extracted event as an item; private (`_`) and unknown keys are left behind."""
    item = EventItem(source_url=source_url, source_id=source_id,
                     extraction_method=data.get('extraction_method', 'unknown'))
    for name in EVENT_FIELDS:
        item[name] = data.get(name)
    return item
