"""
OpenStreetMap Overpass API seed generator.

Queries OSM for venues and businesses in a given area that have a website
tag, then returns them as seed records ready for the spider to crawl.

Each seed record contains everything the context system needs:
  url             — the venue's website (becomes a start_url)
  location_title  — OSM name tag
  location_address — assembled from addr:* tags
  location_lat    — from the OSM node/way centroid
  location_lon    — ditto

Usage:
  from scraper.seeds.overpass import query_area, SearchArea

  seeds = query_area(SearchArea(lat=38.9, lon=-77.0, radius_km=10))
  for seed in seeds:
      print(seed['url'], seed['location_title'])
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── Search area descriptor ────────────────────────────────────────────────────

@dataclass
class SearchArea:
    """
    Defines the geographic area to seed from.  Provide one of:

    Circle (most common):
      lat, lon, radius_km

    Bounding box:
      bbox = (south_lat, west_lon, north_lat, east_lon)

    City name (resolved via Nominatim before querying Overpass):
      city, country, radius_km
    """
    lat:        Optional[float] = None
    lon:        Optional[float] = None
    radius_km:  float           = 10.0
    bbox:       Optional[tuple] = None          # (S, W, N, E)
    city:       Optional[str]   = None
    country:    Optional[str]   = None
    # OSM tags that identify event-relevant venues.  Extend as needed.
    amenity_types: list[str] = field(default_factory=lambda: [
        'bar', 'nightclub', 'pub', 'restaurant', 'cafe',
        'theatre', 'cinema', 'arts_centre', 'community_centre',
        'music_venue', 'events_venue', 'social_club',
        'concert_hall', 'stadium', 'arena',
    ])
    tourism_types: list[str] = field(default_factory=lambda: [
        'attraction', 'museum', 'gallery', 'theme_park',
    ])
    leisure_types: list[str] = field(default_factory=lambda: [
        'sports_hall', 'stadium', 'horse_riding', 'golf_course',
    ])


@dataclass
class VenueSeed:
    url:              str
    location_title:   Optional[str] = None
    location_address: Optional[str] = None
    location_lat:     Optional[float] = None
    location_lon:     Optional[float] = None
    osm_id:           Optional[str]  = None
    osm_type:         Optional[str]  = None   # 'node' | 'way' | 'relation'
    venue_type:       Optional[str]  = None   # OSM amenity/tourism/leisure value, e.g. 'bar'

    def as_context(self) -> dict:
        """Return fields suitable for use as spider request context."""
        return {k: v for k, v in {
            'location_title':   self.location_title,
            'location_address': self.location_address,
            'location_lat':     self.location_lat,
            'location_lon':     self.location_lon,
            'venue_type':       self.venue_type,
        }.items() if v is not None}


# ── Public API ────────────────────────────────────────────────────────────────

OVERPASS_ENDPOINT = 'https://overpass-api.de/api/interpreter'


def query_area(area: SearchArea, timeout: int = 30) -> list[VenueSeed]:
    """
    Query Overpass for venues with website tags in the given area.
    Returns a list of VenueSeed objects, deduplicated by URL.
    """
    bbox = _resolve_bbox(area)
    if bbox is None:
        logger.error("Could not resolve a bounding box for the search area")
        return []

    query = _build_query(area, bbox)
    logger.info("Querying Overpass API for venues in bbox %s", bbox)

    try:
        resp = requests.post(
            OVERPASS_ENDPOINT,
            data={'data': query},
            timeout=timeout + 5,
            headers={'User-Agent': 'happs-scraper/1.0'},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error("Overpass query failed: %s", exc)
        return []

    seeds = _parse_elements(data.get('elements', []))
    logger.info("Overpass returned %d venues with websites", len(seeds))
    return seeds


# ── Internal helpers ──────────────────────────────────────────────────────────

def _resolve_bbox(area: SearchArea) -> Optional[tuple]:
    """Return (south, west, north, east) for the area, resolving city names."""
    if area.bbox:
        return tuple(area.bbox)

    lat, lon = area.lat, area.lon

    # Resolve city name via Nominatim
    if lat is None and area.city:
        lat, lon = _geocode_city(area.city, area.country)
        if lat is None:
            return None

    if lat is None:
        return None

    # Convert radius to rough degree offset (1 deg lat ≈ 111 km)
    delta = area.radius_km / 111.0
    # Longitude delta shrinks towards the poles
    import math
    delta_lon = area.radius_km / (111.0 * math.cos(math.radians(lat)))

    return (lat - delta, lon - delta_lon, lat + delta, lon + delta_lon)


def _geocode_city(city: str, country: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    query = city if not country else f"{city}, {country}"
    try:
        resp = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': query, 'format': 'json', 'limit': 1},
            headers={'User-Agent': 'happs-scraper/1.0'},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]['lat']), float(results[0]['lon'])
    except Exception as exc:
        logger.error("Nominatim city lookup failed for '%s': %s", query, exc)
    return None, None


def _build_query(area: SearchArea, bbox: tuple) -> str:
    """Build an Overpass QL query for event-relevant venues with websites."""
    s, w, n, e = bbox
    bbox_str = f"{s},{w},{n},{e}"
    timeout  = 60

    amenity_filter  = '|'.join(area.amenity_types)
    tourism_filter  = '|'.join(area.tourism_types)
    leisure_filter  = '|'.join(area.leisure_types)

    # We want nodes/ways/relations that match any of our venue categories AND
    # have either a `website`, `contact:website`, or `url` tag.
    def venue_block(element: str) -> str:
        return '\n'.join([
            f'  {element}[amenity~"{amenity_filter}"][~"^(website|contact:website|url)$"~"."]({bbox_str});',
            f'  {element}[tourism~"{tourism_filter}"][~"^(website|contact:website|url)$"~"."]({bbox_str});',
            f'  {element}[leisure~"{leisure_filter}"][~"^(website|contact:website|url)$"~"."]({bbox_str});',
        ])

    return f"""
[out:json][timeout:{timeout}];
(
{venue_block("node")}
{venue_block("way")}
{venue_block("relation")}
);
out center body;
""".strip()


def _parse_elements(elements: list) -> list[VenueSeed]:
    """Convert raw Overpass elements to VenueSeed objects, dedup by URL."""
    seen_urls: set[str] = set()
    seeds: list[VenueSeed] = []

    for el in elements:
        tags = el.get('tags', {})
        url  = (tags.get('website')
                or tags.get('contact:website')
                or tags.get('url', ''))
        url  = url.strip().rstrip('/')
        if not url or not url.startswith('http'):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        lat, lon = _centroid(el)
        seeds.append(VenueSeed(
            url              = url,
            location_title   = tags.get('name'),
            location_address = _build_address(tags),
            location_lat     = lat,
            location_lon     = lon,
            osm_id           = str(el.get('id')),
            osm_type         = el.get('type'),
            venue_type       = tags.get('amenity') or tags.get('tourism') or tags.get('leisure'),
        ))

    return seeds


def _centroid(el: dict) -> tuple[Optional[float], Optional[float]]:
    """Extract lat/lon from a node, or the centroid of a way/relation."""
    if el.get('type') == 'node':
        return el.get('lat'), el.get('lon')
    center = el.get('center', {})
    return center.get('lat'), center.get('lon')


def _build_address(tags: dict) -> Optional[str]:
    """Assemble a human-readable address from OSM addr:* tags."""
    parts = filter(None, [
        tags.get('addr:housenumber', '') + ' ' + tags.get('addr:street', ''),
        tags.get('addr:city'),
        tags.get('addr:state'),
        tags.get('addr:postcode'),
    ])
    result = ', '.join(p.strip() for p in parts if p.strip())
    return result or None
