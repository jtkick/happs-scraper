"""
AI-powered event extractor — last resort when structured data is absent.

Strategy:
  1. Strip HTML to clean prose with trafilatura (reduces tokens ~50x).
  2. Send only the first 4 000 characters to Claude Haiku.
  3. Ask for a JSON object matching EventItem fields.
  4. Return the parsed dict, or None on any failure.

Disable entirely by leaving ANTHROPIC_API_KEY blank in .env.
"""

from __future__ import annotations
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_PROMPT = """\
Extract event information from the text below.
Return ONLY a valid JSON object with these keys (use null for missing fields):

{
  "title": string,
  "description": string,
  "start_datetime": "ISO-8601 datetime string with timezone, e.g. 2025-06-15T19:00:00-05:00",
  "end_datetime": "ISO-8601 or null",
  "location_title": string or null,
  "location_address": string or null,
  "ticket_price": number (USD) or null,
  "ticket_url": string or null,
  "url": string or null,
  "image_url": string or null
}

Text:
"""


def extract(html: str, base_url: str, api_key: str) -> Optional[dict]:
    """
    Strip the HTML to text, then ask Claude Haiku to extract event fields.
    Returns a dict of extracted fields, or None if extraction failed.
    """
    try:
        import trafilatura
    except ImportError:
        logger.warning("trafilatura not installed — skipping AI extraction")
        return None

    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    if not text or len(text) < 200:
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping AI extraction")
        return None

    client = anthropic.Anthropic(api_key=api_key)

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": _PROMPT + text[:4000],
            }],
        )
    except Exception as exc:
        logger.warning("Claude API call failed for %s: %s", base_url, exc)
        return None

    raw = message.content[0].text.strip()

    # Strip markdown code fences if the model added them.
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("AI returned invalid JSON for %s: %s", base_url, exc)
        return None

    return data if isinstance(data, dict) else None
