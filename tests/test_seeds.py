"""
Tests for the Overpass seed generator.

Only the offline parts are covered: response parsing, address assembly, bbox
maths and the context handed to spiders. Nothing here touches the network.
"""
import pytest

from scraper.seeds.overpass import (
    SearchArea,
    VenueSeed,
    _build_address,
    _build_query,
    _centroid,
    _parse_elements,
    _resolve_bbox,
)


def _node(osm_id, **tags):
    return {'type': 'node', 'id': osm_id, 'lat': 39.29, 'lon': -76.61, 'tags': tags}


# ── Element parsing ───────────────────────────────────────────────────────────

def test_full_node_becomes_a_seed():
    [seed] = _parse_elements([_node(
        1, name='The Pub', amenity='pub', website='https://pub.test/',
        **{'addr:housenumber': '12', 'addr:street': 'Dock St',
           'addr:city': 'Baltimore', 'addr:state': 'MD', 'addr:postcode': '21201'},
    )])
    assert seed.url == 'https://pub.test'
    assert seed.location_title == 'The Pub'
    assert seed.location_address == '12 Dock St, Baltimore, MD, 21201'
    assert (seed.location_lat, seed.location_lon) == (39.29, -76.61)
    assert seed.venue_type == 'pub'
    assert (seed.osm_type, seed.osm_id) == ('node', '1')


@pytest.mark.parametrize('tag', ['website', 'contact:website', 'url'])
def test_all_website_tag_spellings(tag):
    [seed] = _parse_elements([_node(1, name='V', amenity='bar', **{tag: 'https://v.test'})])
    assert seed.url == 'https://v.test'


def test_website_tag_precedence():
    [seed] = _parse_elements([_node(
        1, name='V', amenity='bar',
        website='https://first.test', **{'contact:website': 'https://second.test'},
    )])
    assert seed.url == 'https://first.test'


def test_trailing_slash_is_normalised_for_dedup():
    seeds = _parse_elements([
        _node(1, name='A', amenity='bar', website='https://v.test/'),
        _node(2, name='B', amenity='pub', website='https://v.test'),
    ])
    assert len(seeds) == 1


@pytest.mark.parametrize('tags, reason', [
    ({'name': 'No site', 'amenity': 'bar'},                            'no website tag'),
    ({'name': 'Bad', 'amenity': 'bar', 'website': 'ftp://nope.test'},  'non-http scheme'),
    ({'name': 'Blank', 'amenity': 'bar', 'website': '   '},            'empty website'),
])
def test_unusable_elements_are_skipped(tags, reason):
    assert _parse_elements([_node(1, **tags)]) == [], reason


@pytest.mark.parametrize('tags, expected', [
    ({'amenity': 'bar'},      'bar'),
    ({'tourism': 'museum'},   'museum'),
    ({'leisure': 'stadium'},  'stadium'),
    ({'amenity': 'bar', 'tourism': 'attraction'}, 'bar'),   # amenity wins
])
def test_venue_type_source_precedence(tags, expected):
    [seed] = _parse_elements([_node(1, name='V', website='https://v.test', **tags)])
    assert seed.venue_type == expected


def test_way_uses_its_centroid():
    [seed] = _parse_elements([{
        'type': 'way', 'id': 2, 'center': {'lat': 39.3, 'lon': -76.6},
        'tags': {'name': 'Theatre', 'amenity': 'theatre', 'website': 'https://t.test'},
    }])
    assert (seed.location_lat, seed.location_lon) == (39.3, -76.6)


def test_element_without_coordinates_is_still_seeded():
    [seed] = _parse_elements([
        {'type': 'way', 'id': 3, 'tags': {'name': 'X', 'amenity': 'bar', 'website': 'https://x.test'}},
    ])
    assert seed.location_lat is None


# ── Address assembly ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('tags, expected', [
    ({'addr:housenumber': '12', 'addr:street': 'Dock St'},  '12 Dock St'),
    ({'addr:street': 'Dock St'},                            'Dock St'),
    ({'addr:city': 'Baltimore'},                            'Baltimore'),
    ({'name': 'No address here'},                           None),
    ({},                                                    None),
])
def test_build_address(tags, expected):
    assert _build_address(tags) == expected


# ── Centroid ──────────────────────────────────────────────────────────────────

def test_centroid_of_node():
    assert _centroid({'type': 'node', 'lat': 1.0, 'lon': 2.0}) == (1.0, 2.0)


def test_centroid_of_relation_without_center():
    assert _centroid({'type': 'relation'}) == (None, None)


# ── Spider context ────────────────────────────────────────────────────────────

def test_as_context_matches_the_spider_context_keys():
    seed = VenueSeed(url='https://v.test', location_title='The Pub',
                     location_address='12 Dock St', location_lat=39.29,
                     location_lon=-76.61, venue_type='pub')
    assert seed.as_context() == {
        'location_title': 'The Pub',
        'location_address': '12 Dock St',
        'location_lat': 39.29,
        'location_lon': -76.61,
        'venue_type': 'pub',
    }


def test_as_context_omits_unknown_fields():
    """Empty keys must not override data a page already extracted."""
    assert VenueSeed(url='https://v.test').as_context() == {}


def test_as_context_excludes_osm_bookkeeping():
    seed = VenueSeed(url='https://v.test', osm_id='1', osm_type='node')
    assert 'osm_id' not in seed.as_context()


# ── Bounding box ──────────────────────────────────────────────────────────────

def test_explicit_bbox_is_passed_through():
    assert _resolve_bbox(SearchArea(bbox=(1, 2, 3, 4))) == (1, 2, 3, 4)


def test_radius_converted_to_degrees():
    south, west, north, east = _resolve_bbox(SearchArea(lat=39.0, lon=-76.0, radius_km=11.1))
    assert north - south == pytest.approx(0.2, abs=1e-3)
    # Longitude degrees are narrower away from the equator.
    assert (east - west) > (north - south)


def test_bbox_is_none_without_location():
    assert _resolve_bbox(SearchArea()) is None


# ── Query construction ────────────────────────────────────────────────────────

def test_query_covers_nodes_ways_and_relations():
    query = _build_query(SearchArea(), (1, 2, 3, 4))
    for element in ('node[', 'way[', 'relation['):
        assert element in query


def test_query_requires_a_website_tag():
    query = _build_query(SearchArea(), (1, 2, 3, 4))
    assert '^(website|contact:website|url)$' in query


def test_query_includes_the_bbox_and_venue_types():
    query = _build_query(SearchArea(amenity_types=['bar', 'pub']), (1, 2, 3, 4))
    assert '(1,2,3,4)' in query
    assert 'amenity~"bar|pub"' in query
