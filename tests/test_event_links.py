"""Tests for scraper/discovery/event_links.py — a listing event's own page, from its entry."""
from scrapy.http import HtmlResponse

from scraper import extraction
from scraper.discovery import event_links
from scraper.extractors.ai import AIResult

LISTING = 'https://venue.test/whats-on'


def attach(body: str, *titles, detail_links=()):
    events = [{'title': t, 'start_datetime': '2026-11-01T20:00'} for t in titles]
    event_links.attach(f'<html><body>{body}</body></html>', LISTING, events, detail_links)
    return events


def entry(title, href, link_text='More info', blurb='A night of music.'):
    return f'<div class="item"><h3>{title}</h3><p>{blurb}</p><a href="{href}">{link_text}</a></div>'


def test_each_entry_gets_its_more_info_link():
    body = entry('Jazz Night', '/jazz') + entry('Quiz Night', '/quiz') + entry('Open Mic', '/mic')
    events = attach(body, 'Jazz Night', 'Quiz Night', 'Open Mic')
    assert [e['url'] for e in events] == ['https://venue.test/jazz', 'https://venue.test/quiz',
                                          'https://venue.test/mic']
    assert all(e[event_links.GUESSED] for e in events)


def test_a_linked_title_is_its_link():
    body = '<li><a href="/e/jazz">Jazz Night</a> Nov 1</li><li><a href="/e/quiz">Quiz Night</a> Nov 2</li>'
    events = attach(body, 'Jazz Night', 'Quiz Night')
    assert [e['url'] for e in events] == ['https://venue.test/e/jazz', 'https://venue.test/e/quiz']


def test_titles_match_despite_case_and_curly_quotes():
    [event] = attach(entry('JAZZ AT ROSIE’S', '/jazz'), "Jazz at Rosie's")
    assert event['url'] == 'https://venue.test/jazz'


def test_a_link_from_the_next_entry_is_not_taken():
    body = ('<div><h3>Jazz Night</h3><p>No page for this one.</p></div>'
            '<div><h3>Quiz Night</h3><a href="/quiz">More info</a></div>')
    jazz, quiz = attach(body, 'Jazz Night', 'Quiz Night')
    assert 'url' not in jazz and quiz['url'] == 'https://venue.test/quiz'


def test_unhelpful_links_are_left_alone():
    body = ('<header><a href="/jazz">Jazz Night</a></header>'
            + entry('Jazz Night', 'https://tickets.test/jazz', 'Tickets')
            + entry('Quiz Night', '/menu.pdf') + entry('Open Mic', '/about', 'Contact us'))
    events = attach(body, 'Jazz Night', 'Quiz Night', 'Open Mic')
    assert not any(e.get('url') for e in events)


def test_a_link_the_parser_found_is_kept():
    events = [{'title': 'Jazz Night', 'url': 'https://venue.test/from-jsonld'}]
    event_links.attach(entry('Jazz Night', '/jazz'), LISTING, events)
    assert events == [{'title': 'Jazz Night', 'url': 'https://venue.test/from-jsonld'}]


def test_repeats_of_one_title_take_their_own_entries_links():
    body = entry('Trivia', '/trivia-1') + entry('Trivia', '/trivia-2')
    events = attach(body, 'Trivia', 'Trivia')
    assert [e['url'] for e in events] == ['https://venue.test/trivia-1', 'https://venue.test/trivia-2']


def test_entries_shown_twice_still_get_their_own_links():
    cells = '<td><a href="/e/dj-2">DJ Night</a></td><td><a href="/e/dj-3">DJ Night</a></td>'
    popups = entry('DJ Night', '/e/dj-2') + entry('DJ Night', '/e/dj-3')
    events = attach(f'<table><tr>{cells}</tr></table>{popups}', 'DJ Night', 'DJ Night')
    assert [e['url'] for e in events] == ['https://venue.test/e/dj-2', 'https://venue.test/e/dj-3']


def test_a_link_differently_named_events_share_is_not_their_own():
    body = ('<section><div><h3>Tasting Tuesdays</h3></div><div><h3>Blues Wednesdays</h3></div>'
            '<a href="/events/entertainment">Learn more</a></section>')
    for body in (body, entry('Tasting Tuesdays', '/events/entertainment')
                 + entry('Blues Wednesdays', '/events/entertainment')):
        assert not any(e.get('url') for e in attach(body, 'Tasting Tuesdays', 'Blues Wednesdays'))


def test_site_pages_are_not_an_events_own():
    [event] = attach('<p><a href="/about?utm_source=x">Trivia Night at QCR</a></p>', 'Trivia Night at QCR')
    assert 'url' not in event


def test_a_listings_detail_links_count_even_without_telling_text():
    body = '<div><h3>Jazz Night</h3><a href="/p/123">→</a><a href="/about">Venue</a></div>'
    [event] = attach(body, 'Jazz Night', detail_links=['https://venue.test/p/123'])
    assert event['url'] == 'https://venue.test/p/123'


def test_same_event():
    listing = {'title': 'Jazz Night with the Trio'}
    assert event_links.same_event(listing, {'title': 'Jazz Night with The Trio | Rosie’s'})
    assert not event_links.same_event(listing, {'title': 'About Us'})


# ── Through parse_page ────────────────────────────────────────────────────────

def response(html, url=LISTING):
    return HtmlResponse(url=url, body=html.encode(), encoding='utf-8')


def ai_returning(*events):
    return lambda html, url: AIResult(events=[dict(e) for e in events])


def test_model_read_listing_events_get_their_links():
    html = '<html><body>' + entry('Jazz Night', '/jazz') + entry('Quiz Night', '/quiz') + '</body></html>'
    ai = ai_returning({'title': 'Jazz Night', 'start_datetime': '2026-11-01T20:00', 'evidence': 'x'},
                      {'title': 'Quiz Night', 'start_datetime': '2026-11-02T20:00', 'evidence': 'x'})
    result, events = extraction.parse_page(response(html), kind='listing', ai=ai)
    assert [e['url'] for e in events] == ['https://venue.test/jazz', 'https://venue.test/quiz']
    assert result.links is not None


def test_a_guessed_link_to_some_other_page_leaves_the_listing_entry_alone():
    partial = {'title': 'Jazz Night', 'start_datetime': '2026-11-01T20:00', 'description': 'Short.',
               'url': 'https://venue.test/jazz', event_links.GUESSED: True}
    other = ('<html><head><meta property="og:title" content="About Us">'
             '<meta property="og:description" content="We are a bar."></head><body></body></html>')
    _, [event] = extraction.parse_page(response(other, partial['url']), kind='detail', partial=partial)
    assert event['title'] == 'Jazz Night' and event['description'] == 'Short.'

    right = other.replace('About Us', 'Jazz Night | Rosie’s').replace('We are a bar.', 'The long story.')
    _, [event] = extraction.parse_page(response(right, partial['url']), kind='detail', partial=partial)
    assert event['description'] == 'The long story.'
