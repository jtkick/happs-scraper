#!/usr/bin/env python3
"""
Register venue websites in an area as backend crawl sources.

Usage:
    python tools/seed_sources.py --lat 39.10 --lon -84.51 --radius-km 5
    python tools/seed_sources.py --city Cincinnati --country US --radius-km 10
    python tools/seed_sources.py --url https://citycalendar.example --kind aggregator
    ... --dry-run            print what would be registered

Venues come from OpenStreetMap (scraper/seeds/overpass.py). Each website is
keyed by its registrable domain; a domain shared by several OSM venues (a
chain) is marked multi_location so no single venue's address is injected
into its events. Social/aggregator profile URLs are skipped. Each source's
timezone is looked up from its coordinates.
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.discovery.events_page import site_of

# Websites that are someone else's profile page, not the venue's own site.
SKIP_DOMAINS = {
    'facebook.com', 'instagram.com', 'twitter.com', 'x.com', 'linktr.ee', 'yelp.com',
    'tripadvisor.com', 'google.com', 'goo.gl', 'foursquare.com', 'tiktok.com',
    'wikipedia.org', 'opentable.com', 'toasttab.com', 'doordash.com', 'ubereats.com',
    'grubhub.com', 'squareup.com', 'square.site',
}


def timezone_for(lat, lon):
    if lat is None or lon is None:
        return None
    from timezonefinder import TimezoneFinder
    return TimezoneFinder().timezone_at(lat=float(lat), lng=float(lon))


def seeds_to_sources(seeds) -> list[dict]:
    by_domain: dict[str, list] = defaultdict(list)
    for seed in seeds:
        domain = site_of(seed.url)
        if domain and domain not in SKIP_DOMAINS:
            by_domain[domain].append(seed)

    sources = []
    for domain, group in sorted(by_domain.items()):
        first = group[0]
        sources.append({
            'domain':           domain,
            'homepage_url':     first.url,
            'osm_id':           f'{first.osm_type}/{first.osm_id}' if first.osm_id else None,
            'location_title':   first.location_title,
            'location_address': first.location_address,
            'location_lat':     first.location_lat,
            'location_lon':     first.location_lon,
            'venue_type':       first.venue_type,
            'timezone':         timezone_for(first.location_lat, first.location_lon),
            'multi_location':   len(group) > 1,
            'kind':             'venue',
        })
    return sources


def main():
    parser = argparse.ArgumentParser(description='Register crawl sources in the backend')
    area = parser.add_argument_group('OSM area')
    area.add_argument('--lat', type=float)
    area.add_argument('--lon', type=float)
    area.add_argument('--city')
    area.add_argument('--country')
    area.add_argument('--radius-km', type=float, default=10.0)
    parser.add_argument('--url', action='append', default=[],
                        help='Register a site directly (repeatable), e.g. a city calendar')
    parser.add_argument('--kind', choices=['venue', 'aggregator'], default='aggregator',
                        help='Kind for --url sources (default: aggregator)')
    parser.add_argument('--timezone', help='IANA zone for --url sources')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    sources = []
    if args.lat is not None or args.city:
        from scraper.seeds.overpass import SearchArea, query_area
        seeds = query_area(SearchArea(lat=args.lat, lon=args.lon, city=args.city,
                                      country=args.country, radius_km=args.radius_km))
        sources += seeds_to_sources(seeds)
    for url in args.url:
        sources.append({'domain': site_of(url), 'homepage_url': url, 'kind': args.kind,
                        'timezone': args.timezone})

    if not sources:
        parser.error('nothing to register: give --lat/--lon, --city, or --url')

    shared = sum(1 for s in sources if s.get('multi_location'))
    print(f'{len(sources)} sources ({shared} shared by several venues)')
    if args.dry_run:
        print(json.dumps(sources, indent=2, default=str))
        return

    from scrapy.utils.project import get_project_settings
    from scraper.sources.client import BackendClient
    client = BackendClient.from_settings(get_project_settings())
    result = client.upsert_sources(sources)
    if result is None:
        sys.exit('Backend rejected the sources — check HAPPS_API_BASE / HAPPS_SCRAPER_TOKEN')
    print(f"created {result['created']}, updated {result['updated']}")


if __name__ == '__main__':
    main()
