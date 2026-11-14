"""
Score the scraper over a set of cases: event recall and precision, accuracy
per field, and the same broken down by how each event was extracted.
tools/evaluate.py prints it and compares it with an earlier run.
"""
from __future__ import annotations
from collections import defaultdict
from typing import Iterable, Optional

from scraper.eval.case import Case
from scraper.eval.compare import compare
from scraper.eval.run import RunResult, run_case

SLOWEST = 5


def scorecard(cases: Iterable[Case], *, ai: Optional[str] = 'replay', api_key: str = '',
              model: Optional[str] = None) -> dict:
    return aggregate({case.id: score_case(case, run_case(case, ai=ai, api_key=api_key, model=model))
                      for case in cases})


def score_case(case: Case, result: RunResult) -> dict:
    """One case's part of a scorecard: its per_case entry plus the counts aggregate() adds up."""
    c = compare(case.events, result.events, not_events=case.not_events,
                complete=case.complete, timezone_name=case.timezone)
    fields: dict[str, list[int]] = defaultdict(lambda: [0, 0])           # field → [right, asserted]
    methods: dict[str, dict] = defaultdict(lambda: {'events': 0, 'fields_right': 0, 'fields': 0})
    for m in c.matches:
        method = (m.actual or {}).get('extraction_method', 'missed') if m.actual else 'missed'
        methods[method]['events'] += 1
        for key, r in m.fields.items():
            fields[key][1] += 1
            fields[key][0] += r.ok
            methods[method]['fields'] += 1
            methods[method]['fields_right'] += r.ok
    known = {k['path'] for k in case.known_failures}
    extra = {}
    if case.kind == 'site':
        from scraper.eval.sites import discovery
        extra['discovery'] = discovery(case, result.fetched)
    return {
        **extra,
        'source': case.source, 'strategy': result.strategy, 'ai_used': result.ai_used,
        'ai_missing': result.ai_missing, 'ai_stale': result.ai_stale, 'recall': c.recall, 'precision': c.precision,
        'failures': c.failures, 'new_failures': [p for p in c.failures if p not in known],
        'counts': {
            'labelled': len(c.matches), 'found': c.found,
            'parsed_wrong': len(c.rejected) + (len(c.extras) if case.complete else 0),
            'fields': dict(fields), 'methods': dict(methods),
        },
    }


def aggregate(scores: dict) -> dict:
    """A scorecard from score_case() results keyed by case id."""
    labelled = found = parsed_wrong = 0
    fields: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    methods: dict[str, dict] = defaultdict(lambda: {'events': 0, 'fields_right': 0, 'fields': 0})
    per_case = {}
    for case_id, score in scores.items():
        counts = score['counts']
        labelled += counts['labelled']
        found += counts['found']
        parsed_wrong += counts['parsed_wrong']
        for key, (right, asserted) in counts['fields'].items():
            fields[key][0] += right
            fields[key][1] += asserted
        for method, m in counts['methods'].items():
            for k in ('events', 'fields_right', 'fields'):
                methods[method][k] += m[k]
        per_case[case_id] = {k: v for k, v in score.items() if k != 'counts'}

    total_parsed = found + parsed_wrong
    timed = [(case_id, p['duration_ms']) for case_id, p in per_case.items() if p.get('duration_ms') is not None]
    return {
        'cases': len(per_case),
        'ai_missing': sum(1 for p in per_case.values() if p.get('ai_missing')),
        'ai_stale': sum(1 for p in per_case.values() if p.get('ai_stale')),
        'slowest': [{'case': c, 'ms': ms} for c, ms in sorted(timed, key=lambda t: -t[1])[:SLOWEST]],
        'events': labelled,
        'recall': found / labelled if labelled else 1.0,
        'precision': found / total_parsed if total_parsed else 1.0,
        'fields': {k: {'right': v[0], 'asserted': v[1], 'accuracy': v[0] / v[1]}
                   for k, v in sorted(fields.items())},
        'methods': {k: {**v, 'accuracy': v['fields_right'] / v['fields'] if v['fields'] else None}
                    for k, v in sorted(methods.items())},
        'per_case': per_case,
    }


def regressions(current: dict, baseline: dict) -> dict:
    """Cases that fail somewhere they didn't before, and cases that improved."""
    worse, better = {}, {}
    for case_id, now in current['per_case'].items():
        before = baseline.get('per_case', {}).get(case_id)
        if before is None:
            continue
        new = sorted(set(now['failures']) - set(before['failures']))
        fixed = sorted(set(before['failures']) - set(now['failures']))
        if new:
            worse[case_id] = new
        if fixed:
            better[case_id] = fixed
    return {'worse': worse, 'better': better}
