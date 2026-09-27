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
    fingerprint         = scrapy.Field()   # sha256 dedup hash (set by pipeline)
    extraction_method   = scrapy.Field()   # 'jsonld' | 'opengraph' | 'selectors' | 'ai'

    # Set by FingerprintDedupPipeline when this is a repeat scrape of a
    # recurring event whose backend record already exists.
    is_recurring_update = scrapy.Field()   # bool
    backend_event_id    = scrapy.Field()   # UUID str of the existing backend event
