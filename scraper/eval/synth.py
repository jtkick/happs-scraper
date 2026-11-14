"""
Synthetic test pages: a list of events rendered in a chosen page format and
style, with labels that are true by construction (they come from the same
events the page was written from). Used by the MCP server's generate_cases and
generate_site to test each extractor and date style on purpose.

An event: {title, start: 'YYYY-MM-DDTHH:MM' (venue-local), end?, description?,
price?: number, ticket_url?, url?, weekly?: ['TH', …]}. A weekly event is
written as "Every Thursday at 8:00 PM" with no date; its label starts on the
first such day after captured_at.

A variant is a '+'-joined set of flags:
  date style   long (default) "Wednesday, October 7, 2026" · short "Oct 7, 2026" ·
               numeric "Wed 10/7" · yearless "Wednesday, October 7" · iso "2026-10-07"
  time style   12h (default) "8:00 PM" · 24h "20:00"
  extras       noise   nav, cookie banner, and a footer saying "open daily" / "happy
                       hour every day", which must not make events repeat
               nolinks no per-event links, so url/ticket_url aren't asserted
"""
from __future__ import annotations
import html as html_lib
import json
import re
from calendar import Calendar
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

FORMATS = ('jsonld', 'microdata', 'html_list', 'html_table', 'month_grid', 'prose', 'detail_page')
DATE_STYLES = ('long', 'short', 'numeric', 'yearless', 'iso')
TIME_STYLES = ('12h', '24h')
EXTRAS = ('noise', 'nolinks')
DAY_CODES = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')
DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
# Formats that can say "every Thursday" in words.
RECURRING_FORMATS = ('html_list', 'prose', 'detail_page')


class Unsupported(ValueError):
    """This format can't express these events in this variant."""


def parse_variant(variant: str) -> dict:
    flags = {f for f in (variant or 'long').split('+') if f}
    unknown = flags - set(DATE_STYLES) - set(TIME_STYLES) - set(EXTRAS)
    if unknown:
        raise ValueError(f'Unknown variant flags {sorted(unknown)}; use {DATE_STYLES + TIME_STYLES + EXTRAS}.')
    dates = [f for f in flags if f in DATE_STYLES]
    times = [f for f in flags if f in TIME_STYLES]
    if len(dates) > 1 or len(times) > 1:
        raise ValueError('A variant has at most one date style and one time style.')
    return {'date': dates[0] if dates else 'long', 'time': times[0] if times else '12h',
            'noise': 'noise' in flags, 'links': 'nolinks' not in flags}


def _local(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        raise ValueError(f'Give event times as venue-local times without an offset: {value!r}')
    return dt


def _date_text(d: date, style: str) -> str:
    day = DAY_NAMES[d.weekday()]
    month = d.strftime('%B')
    if style == 'long':
        return f'{day}, {month} {d.day}, {d.year}'
    if style == 'short':
        return f'{d.strftime("%b")} {d.day}, {d.year}'
    if style == 'numeric':
        return f'{day[:3]} {d.month}/{d.day}'
    if style == 'yearless':
        return f'{day}, {month} {d.day}'
    return d.isoformat()


def _time_text(dt: datetime, style: str) -> str:
    if style == '24h':
        return dt.strftime('%H:%M')
    hour = dt.hour % 12 or 12
    return f'{hour}:{dt.minute:02d} {"AM" if dt.hour < 12 else "PM"}'


class _Event:
    def __init__(self, data: dict, index: int, tz: ZoneInfo, captured: datetime, base_url: str):
        if not data.get('title'):
            raise ValueError(f'Event {index} needs a title.')
        self.data = data
        self.title = str(data['title'])
        self.description = data.get('description')
        self.price = data.get('price')
        self.weekly = [d.upper() for d in data.get('weekly') or []]
        if any(d not in DAY_CODES for d in self.weekly):
            raise ValueError(f'Event {index}: weekly days are {DAY_CODES}.')
        self.start = _local(data['start'])
        self.end = _local(data['end']) if data.get('end') else None
        if self.weekly:
            self.start = _next_weekly(self.weekly, self.start.time(), captured.astimezone(tz).replace(tzinfo=None))
            if self.end:
                self.end = datetime.combine(self.start.date(), self.end.time())
        self.tz = tz
        slug = re.sub(r'[^a-z0-9]+', '-', self.title.lower()).strip('-') or f'event-{index}'
        self.url = data.get('url') or f'{base_url.rstrip("/")}/events/{slug}'
        self.ticket_url = data.get('ticket_url')

    def iso(self, dt: datetime) -> str:
        return dt.replace(tzinfo=self.tz).isoformat()

    def when(self, opts: dict) -> str:
        time_text = _time_text(self.start, opts['time'])
        if self.end and self.end.date() == self.start.date():
            time_text += f' – {_time_text(self.end, opts["time"])}'
        if self.weekly:
            days = ' and '.join(DAY_NAMES[DAY_CODES.index(d)] for d in self.weekly)
            return f'Every {days} at {time_text}'
        text = f'{_date_text(self.start.date(), opts["date"])} at {time_text}'
        if self.end and self.end.date() != self.start.date():
            text += f' until {_date_text(self.end.date(), opts["date"])} at {_time_text(self.end, opts["time"])}'
        return text

    def label(self, *, description: bool, price: bool, url: bool, ticket: bool, noise: bool, end: bool = True) -> dict:
        out: dict = {'title': self.title, 'start_datetime': self.iso(self.start)}
        if end and self.end:
            out['end_datetime'] = self.iso(self.end)
        if description and self.description:
            out['description'] = self.description
        if price and self.price is not None:
            out['ticket_price'] = float(self.price)
        if url:
            out['url'] = self.url
        if ticket and self.ticket_url:
            out['ticket_url'] = self.ticket_url
        if self.weekly:
            out['recurrence_freq'] = 'weekly'
            out['recurrence_byday'] = list(self.weekly)
        elif noise:
            out['recurrence_freq'] = 'none'
        return out


def _next_weekly(days: list[str], at, after: datetime) -> datetime:
    for offset in range(1, 8):
        d = after.date() + timedelta(days=offset)
        if DAY_CODES[d.weekday()] in days:
            return datetime.combine(d, at)
    raise AssertionError


def _e(text) -> str:
    return html_lib.escape(str(text))


def _page(title: str, body: str, venue: str, opts: dict, head: str = '') -> str:
    nav = cookie = footer = ''
    if opts['noise']:
        nav = '<nav><a href="/">Home</a> | <a href="/menu">Menu</a> | <a href="/events">Events</a> | <a href="/contact">Contact</a></nav>'
        cookie = '<div class="cookie-banner">We use cookies to improve your experience. <button>Accept</button></div>'
        footer = ('<footer><p>Open daily 11am – 2am</p><p>Happy hour every day 4–7pm</p>'
                  f'<p>© {_e(venue)}. All rights reserved.</p></footer>')
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{_e(title)}</title>{head}</head>'
            f'<body>{cookie}{nav}<main><h1>{_e(title)}</h1>{body}</main>{footer}</body></html>')


def render(events: list[dict], fmt: str, *, venue: str = 'The Test Venue', timezone: str = 'America/New_York',
           variant: str = 'long', captured_at: str = '2026-09-15T12:00:00+00:00',
           base_url: str = 'https://venue.test') -> tuple[str, list[dict], str]:
    """(html, labels, kind) for the events in this format and variant."""
    if fmt not in FORMATS:
        raise ValueError(f'Unknown format {fmt!r}; use {FORMATS}.')
    if not events:
        raise ValueError('Give at least one event.')
    opts = parse_variant(variant)
    tz = ZoneInfo(timezone)
    captured = datetime.fromisoformat(captured_at)
    evs = [_Event(e, i, tz, captured, base_url) for i, e in enumerate(events)]
    if any(e.weekly for e in evs) and fmt not in RECURRING_FORMATS:
        raise Unsupported(f'{fmt} has no way to say "every Thursday"; weekly events need {RECURRING_FORMATS}.')
    if fmt == 'detail_page':
        evs = evs[:1]
    html, labels = _RENDERERS[fmt](evs, venue, opts)
    return html, labels, 'detail' if fmt == 'detail_page' else 'listing'


def _jsonld(evs, venue, opts):
    items = []
    for e in evs:
        item = {'@context': 'https://schema.org', '@type': 'Event', 'name': e.title, 'startDate': e.iso(e.start),
                'location': {'@type': 'Place', 'name': venue}}
        if e.end:
            item['endDate'] = e.iso(e.end)
        if e.description:
            item['description'] = e.description
        if opts['links']:
            item['url'] = e.url
        if e.price is not None:
            item['offers'] = {'@type': 'Offer', 'price': e.price, 'priceCurrency': 'USD',
                              **({'url': e.ticket_url} if e.ticket_url and opts['links'] else {})}
        items.append(item)
    head = f'<script type="application/ld+json">{json.dumps(items)}</script>'
    body = ''.join(f'<article><h2>{_e(e.title)}</h2><p>{_e(e.when(opts))}</p></article>' for e in evs)
    labels = [e.label(description=True, price=True, url=opts['links'], ticket=opts['links'], noise=opts['noise'])
              for e in evs]
    return _page(f'Events at {venue}', body, venue, opts, head), labels


def _microdata(evs, venue, opts):
    parts = []
    for e in evs:
        bits = [f'<h2 itemprop="name">{_e(e.title)}</h2>',
                f'<time itemprop="startDate" datetime="{e.iso(e.start)}">{_e(e.when(opts))}</time>']
        if e.end:
            bits.append(f'<meta itemprop="endDate" content="{e.iso(e.end)}">')
        if e.description:
            bits.append(f'<p itemprop="description">{_e(e.description)}</p>')
        bits.append(f'<div itemprop="location" itemscope itemtype="https://schema.org/Place">'
                    f'<span itemprop="name">{_e(venue)}</span></div>')
        if opts['links']:
            bits.append(f'<a itemprop="url" href="{_e(e.url)}">Details</a>')
        parts.append(f'<div itemscope itemtype="https://schema.org/Event">{"".join(bits)}</div>')
    labels = [e.label(description=True, price=False, url=opts['links'], ticket=False, noise=opts['noise'])
              for e in evs]
    return _page(f'Events at {venue}', ''.join(parts), venue, opts), labels


def _html_list(evs, venue, opts):
    items = []
    for e in evs:
        title = f'<a href="{_e(e.url)}">{_e(e.title)}</a>' if opts['links'] else _e(e.title)
        bits = [f'<h3>{title}</h3>', f'<p class="when">{_e(e.when(opts))}</p>']
        if e.description:
            bits.append(f'<p>{_e(e.description)}</p>')
        if e.price is not None:
            bits.append(f'<p>Tickets ${e.price:g}</p>')
        if e.ticket_url and opts['links']:
            bits.append(f'<a href="{_e(e.ticket_url)}">Buy tickets</a>')
        items.append(f'<li class="event">{"".join(bits)}</li>')
    labels = [e.label(description=True, price=True, url=opts['links'], ticket=opts['links'], noise=opts['noise'])
              for e in evs]
    return _page(f'Upcoming events at {venue}', f'<ul class="events">{"".join(items)}</ul>', venue, opts), labels


def _html_table(evs, venue, opts):
    rows = []
    for e in evs:
        title = f'<a href="{_e(e.url)}">{_e(e.title)}</a>' if opts['links'] else _e(e.title)
        end = f' – {_time_text(e.end, opts["time"])}' if e.end else ''
        price = f'${e.price:g}' if e.price is not None else ''
        rows.append(f'<tr><td>{_e(_date_text(e.start.date(), opts["date"]))}</td>'
                    f'<td>{_e(_time_text(e.start, opts["time"]) + end)}</td><td>{title}</td><td>{price}</td></tr>')
    table = ('<table class="events"><thead><tr><th>Date</th><th>Time</th><th>Event</th><th>Price</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>')
    labels = [e.label(description=False, price=True, url=opts['links'], ticket=False, noise=opts['noise'])
              for e in evs if not e.end or e.end.date() == e.start.date()]
    if len(labels) != len(evs):
        raise Unsupported('html_table writes one day per row; multi-day events need another format.')
    return _page(f'Events at {venue}', table, venue, opts), labels


def _month_grid(evs, venue, opts):
    """A calendar table per month the events fall in, each event in its day's cell."""
    by_day: dict[date, list] = {}
    for e in evs:
        if e.end and e.end.date() != e.start.date():
            raise Unsupported('month_grid puts each event in one day; multi-day events need another format.')
        by_day.setdefault(e.start.date(), []).append(e)
    months = sorted({(d.year, d.month) for d in by_day})
    head = ''.join(f'<th>{n[:3]}</th>' for n in ('Sunday',) + DAY_NAMES[:6])
    grids = []
    for year, month in months:
        weeks = []
        for week in Calendar(firstweekday=6).monthdatescalendar(year, month):
            cells = []
            for d in week:
                if d.month != month:
                    cells.append('<td class="other"></td>')
                    continue
                entries = ''.join(
                    f'<div class="cal-event">{_e(_time_text(e.start, opts["time"]))} '
                    + (f'<a href="{_e(e.url)}">{_e(e.title)}</a>' if opts['links'] else _e(e.title)) + '</div>'
                    for e in by_day.get(d, []))
                cells.append(f'<td><span class="day">{d.day}</span>{entries}</td>')
            weeks.append(f'<tr>{"".join(cells)}</tr>')
        grids.append(f'<h2>{date(year, month, 1).strftime("%B %Y")}</h2><table class="calendar">'
                     f'<thead><tr>{head}</tr></thead><tbody>{"".join(weeks)}</tbody></table>')
    # A calendar cell shows the start time only, so ends aren't asserted.
    labels = [e.label(description=False, price=False, url=opts['links'], ticket=False, noise=opts['noise'], end=False)
              for e in evs]
    return _page(f'{venue} calendar', ''.join(grids), venue, opts), labels


def _prose(evs, venue, opts):
    paras = []
    for e in evs:
        text = f'Join us {e.when(opts)} for {e.title}.'
        if e.description:
            text += f' {e.description}'
        if e.price is not None:
            text += f' Tickets are ${e.price:g}.'
        paras.append(f'<p>{_e(text)}</p>')
    labels = [e.label(description=False, price=True, url=False, ticket=False, noise=opts['noise']) for e in evs]
    return _page(f"What's on at {venue}", ''.join(paras), venue, opts), labels


def _detail_page(evs, venue, opts):
    e = evs[0]
    # Real event pages say more than the event: without this a page can fall under the
    # scraper's minimum text for asking the model, which tests that limit instead.
    bits = [f'<p class="when">{_e(e.when(opts))}</p>',
            f'<p class="where">{_e(venue)}, 1200 Vine Street, Cincinnati, OH 45202</p>']
    if e.description:
        bits.append(f'<div class="description"><p>{_e(e.description)}</p></div>')
    if e.price is not None:
        bits.append(f'<p class="price">Tickets ${e.price:g}</p>')
    if e.ticket_url and opts['links']:
        bits.append(f'<a class="tickets" href="{_e(e.ticket_url)}">Buy tickets</a>')
    bits.append(f'<section class="about"><h2>About {_e(venue)}</h2><p>{_e(venue)} is a neighborhood bar and '
                'music room with a full kitchen, local beer on tap and a back patio. Doors open an hour before '
                'showtime; seating is first come, first served.</p></section>')
    label = e.label(description=True, price=True, url=False, ticket=opts['links'], noise=opts['noise'])
    return _page(e.title, ''.join(bits), venue, opts), [label]


_RENDERERS = {'jsonld': _jsonld, 'microdata': _microdata, 'html_list': _html_list, 'html_table': _html_table,
              'month_grid': _month_grid, 'prose': _prose, 'detail_page': _detail_page}


def default_events(captured_at: str = '2026-09-15T12:00:00+00:00') -> list[dict]:
    """A small mixed set two to three weeks after captured_at: evening, matinee with an end, priced, weekly."""
    base = datetime.fromisoformat(captured_at).date() + timedelta(days=14)
    day = lambda n, hh, mm=0: datetime.combine(base + timedelta(days=n), datetime.min.time()).replace(  # noqa: E731
        hour=hh, minute=mm).isoformat(timespec='minutes')
    return [
        {'title': 'Trivia Night', 'start': day(0, 20), 'description': 'Six rounds of pub trivia with prizes.'},
        {'title': 'Bluegrass Brunch', 'start': day(4, 11), 'end': day(4, 14),
         'description': 'Live bluegrass with a brunch menu.'},
        {'title': 'Comedy Showcase', 'start': day(6, 19, 30), 'price': 15,
         'ticket_url': 'https://tickets.venue.test/comedy', 'description': 'Five local comics.'},
    ]
