"""
Learned per-site CSS recipes — "learn once with AI, reuse for free".

A recipe is JSON stored on the backend Source (editable in Django admin):

  {
    "item_css": "div.event-card",             # one node per event
    "fields": {                               # CSS relative to the item node
      "title": "h3",                          #   no pseudo-element → all text
      "date":  "time::attr(datetime)",        #   ::text / ::attr(x) allowed
      "time":  ".time",
      "description": ".blurb",
      "link":  "a",                           #   defaults to ::attr(href)
      "image": "img",                         #   defaults to ::attr(src)
      "price": ".price"
    },
    "detail_link_css": "div.event-card a::attr(href)",   # optional
    "pagination_css":  "a.next::attr(href)"              # optional
  }

learn() asks Claude for a recipe that reproduces events the AI already
extracted from this page, then *verifies* it locally; only a recipe that
agrees with ≥ ACCEPT of those events is kept.
"""
from __future__ import annotations
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

ACCEPT = 0.8
MAX_OVERSHOOT = 2.0        # a recipe may not find more than 2× the AI's events
MAX_SKELETON_CHARS = 30_000
_DEFAULT_ATTR = {'link': 'href', 'image': 'src'}
_DROP_TAGS = ('script', 'style', 'noscript', 'svg', 'iframe', 'head', 'template', 'link', 'meta')


def extract(response, recipe: dict) -> list[dict]:
    item_css = recipe.get('item_css')
    if not item_css:
        return []
    fields = recipe.get('fields') or {}
    events = []
    try:
        nodes = response.css(item_css)
    except Exception as exc:          # a bad hand-edited selector must not crash the crawl
        logger.warning("Bad recipe item_css %r: %s", item_css, exc)
        return []
    for node in nodes:
        values = {name: _select(node, css, name) for name, css in fields.items() if css}
        title = values.get('title')
        date = ' '.join(filter(None, (values.get('date'), values.get('time'))))
        if not title or not date:
            continue
        events.append({k: v for k, v in {
            'title':          title,
            'start_datetime': date,
            'description':    values.get('description'),
            'url':            response.urljoin(values['link']) if values.get('link') else None,
            'image_url':      response.urljoin(values['image']) if values.get('image') else None,
            'ticket_price':   values.get('price'),
            'extraction_method': 'recipe',
        }.items() if v})
    return events


def learn(response, ai_events: list[dict], api_key: str) -> Optional[dict]:
    """Ask Claude for a recipe matching `ai_events`; return it only if it verifies."""
    verified = [e for e in ai_events if 'drop_reason' not in e and e.get('title')]
    if not api_key or len(verified) < 2:
        return None
    from scraper.extractors import ai
    candidate = ai.suggest_recipe(skeleton(response), verified, response.url, api_key)
    if not candidate:
        return None
    recipe = {k: v for k, v in candidate.items() if v}
    recipe['fields'] = {k: v for k, v in (candidate.get('fields') or {}).items() if v}
    score = agreement(extract(response, recipe), verified)
    if score < ACCEPT:
        logger.info("Recipe for %s rejected (agreement %.0f%%)", response.url, score * 100)
        return None
    logger.info("Recipe for %s learned (agreement %.0f%%)", response.url, score * 100)
    return recipe


def agreement(found: list[dict], expected: list[dict]) -> float:
    """Share of expected events the recipe reproduced (title + same day)."""
    if not expected or not found or len(found) > MAX_OVERSHOOT * len(expected) + 2:
        return 0.0
    matched = 0
    for exp in expected:
        exp_title, exp_day = _norm(exp.get('title')), _day(exp.get('start_datetime'))
        for got in found:
            got_title = _norm(got.get('title'))
            if not got_title or not (got_title in exp_title or exp_title in got_title):
                continue
            if exp_day and _day(got.get('start_datetime')) not in (exp_day, None):
                continue
            matched += 1
            break
    return matched / len(expected)


def skeleton(response) -> str:
    """Page structure for the model: tags, ids, classes, short text; no scripts."""
    from lxml import html as lxml_html
    root = lxml_html.fromstring(response.text)
    for bad in root.xpath('|'.join(f'//{t}' for t in _DROP_TAGS)):
        bad.drop_tree()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in list(el.attrib):
            if attr not in ('class', 'id', 'href', 'datetime', 'itemprop'):
                del el.attrib[attr]
        if el.text and len(el.text.strip()) > 60:
            el.text = el.text.strip()[:60] + '…'
        if el.tail and len(el.tail.strip()) > 60:
            el.tail = el.tail.strip()[:60] + '…'
    body = root.find('body')
    text = lxml_html.tostring(body if body is not None else root, encoding='unicode')
    return re.sub(r'\s+', ' ', text)[:MAX_SKELETON_CHARS]


def _select(node, css: str, name: str) -> Optional[str]:
    try:
        if '::' in css:
            parts = [p.strip() for p in node.css(css).getall() if p.strip()]
            value = ' '.join(parts)
        elif name in _DEFAULT_ATTR:
            sel = node.css(css)
            value = sel.attrib.get(_DEFAULT_ATTR[name]) or sel.attrib.get('data-src') if sel else None
        else:
            sel = node.css(css)
            value = sel[0].xpath('string()').get() if sel else None
    except Exception:
        return None
    return re.sub(r'\s+', ' ', value).strip() if value else None


def _norm(text) -> str:
    return re.sub(r'[^\w]+', ' ', str(text or '').lower()).strip()


def _day(value) -> Optional[str]:
    if not value:
        return None
    m = re.match(r'\d{4}-\d{2}-\d{2}', str(value))
    if m:
        return m.group()
    import dateparser
    parsed = dateparser.parse(str(value), settings={'PREFER_DATES_FROM': 'future'})
    return parsed.date().isoformat() if parsed else None
