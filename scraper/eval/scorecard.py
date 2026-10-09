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
from scraper.eval.run import run_case


def scorecard(cases: Iterable[Case], *, ai: Optional[str] = 'replay', api_key: str = '',
              model: Optional[str] = None) -> dict:
    labelled = found = parsed_wrong = 0
    fields: dict[str, list[int]] = defaultdict(lambda: [0, 0])           # field → [right, asserted]
    methods: dict[str, dict] = defaultdict(lambda: {'events': 0, 'fields_right': 0, 'fields': 0})
    per_case = {}

    for case in cases:
        result = run_case(case, ai=ai, api_key=api_key, model=model)
        c = compare(case.events, result.events, not_events=case.not_events,
                    complete=case.complete, timezone_name=case.timezone)
        labelled += len(c.matches)
        found += c.found
        parsed_wrong += len(c.rejected) + (len(c.extras) if case.complete else 0)
        for m in c.matches:
            method = (m.actual or {}).get('extraction_method', 'missed') if m.actual else 'missed'
            methods[method]['events'] += 1
            for key, r in m.fields.items():
                fields[key][1] += 1
                fields[key][0] += r.ok
                methods[method]['fields'] += 1
                methods[method]['fields_right'] += r.ok
        known = {k['path'] for k in case.known_failures}
        per_case[case.id] = {
            'source': case.source, 'strategy': result.strategy, 'ai_used': result.ai_used,
            'ai_missing': result.ai_missing, 'recall': c.recall, 'precision': c.precision,
            'failures': c.failures, 'new_failures': [p for p in c.failures if p not in known],
        }

    total_parsed = found + parsed_wrong
    return {
        'cases': len(per_case),
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
