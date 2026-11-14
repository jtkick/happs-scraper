import pytest

from scraper.eval.case import Case
from scraper.eval.compare import compare
from scraper.eval.run import run_case
from scraper.eval.synth import FORMATS, Unsupported, default_events, parse_variant, render

CAPTURED = '2026-09-15T12:00:00+00:00'
TZ = 'America/New_York'


def case_for(fmt, variant='long', events=None):
    html, labels, kind = render(events or default_events(CAPTURED), fmt, venue='The Tap', timezone=TZ,
                                variant=variant, captured_at=CAPTURED)
    return Case(id=f'{fmt}-{variant}', url=f'https://tap.test/{fmt}', html=html, kind=kind, captured_at=CAPTURED,
                seed_context={'location_title': 'The Tap', 'timezone': TZ}, events=labels)


@pytest.mark.parametrize('fmt', FORMATS)
def test_every_format_renders_valid_labels(fmt):
    case = case_for(fmt)
    assert case.validate() == []
    assert case.events and all(e['start_datetime'].endswith('-04:00') for e in case.events)


@pytest.mark.parametrize('fmt', ['jsonld', 'microdata'])
def test_structured_formats_parse_to_their_labels(fmt):
    # Guards the generator: machine-readable pages must give back exactly what they were written from.
    case = case_for(fmt)
    result = run_case(case, ai=None)
    c = compare(case.events, result.events, complete=True, timezone_name=TZ)
    assert c.failures == []


def test_date_and_time_styles_change_the_text_not_the_labels():
    long_case, short = case_for('html_list'), case_for('html_list', 'yearless+24h')
    assert 'Tuesday, September 29, 2026 at 8:00 PM' in long_case.html
    assert 'Tuesday, September 29 at 20:00' in short.html
    assert long_case.events == short.events


def test_weekly_events_start_on_the_next_such_day():
    html, labels, _ = render([{'title': 'Karaoke', 'start': '2026-09-01T21:00', 'weekly': ['TH']}], 'prose',
                             timezone=TZ, captured_at=CAPTURED)
    assert 'Every Thursday at 9:00 PM' in html
    assert labels == [{'title': 'Karaoke', 'start_datetime': '2026-09-17T21:00:00-04:00',
                       'recurrence_freq': 'weekly', 'recurrence_byday': ['TH']}]
    with pytest.raises(Unsupported):
        render([{'title': 'K', 'start': '2026-09-01T21:00', 'weekly': ['TH']}], 'jsonld', captured_at=CAPTURED)


def test_noise_asserts_events_dont_repeat():
    case = case_for('html_list', 'noise')
    assert 'Open daily' in case.html
    assert all(e['recurrence_freq'] == 'none' for e in case.events)


def test_nolinks_drops_url_labels():
    assert all('url' not in e for e in case_for('html_list', 'nolinks').events)
    assert all('url' in e for e in case_for('html_list').events)


def test_bad_variants_are_refused():
    with pytest.raises(ValueError):
        parse_variant('long+short')
    with pytest.raises(ValueError):
        parse_variant('sparkly')
