"""Schema coverage for plan.schema.json's optional `claimed_by_handoff` property.

The field is optional and string-or-null; it is never in `required`.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_PLAN_SCHEMA = Path(__file__).parent.parent / 'schemas' / 'plan.schema.json'


def _valid_plan(**overrides) -> dict:
    base = {
        'title': 'Test plan',
        'created': '2026-10-02',
        'author': 'test-em',
        'status': 'draft',
    }
    base.update(overrides)
    return base


def _field_errors(fm: dict) -> list:
    return [e for e in validate_frontmatter(fm, _PLAN_SCHEMA) if e['field'] == 'claimed_by_handoff']


def test_absent_ok():
    assert not _field_errors(_valid_plan())


def test_string_ok():
    assert not _field_errors(_valid_plan(claimed_by_handoff='state/handoffs/2026-10-02-x.md'))


def test_null_ok():
    assert not _field_errors(_valid_plan(claimed_by_handoff=None))


def test_non_string_rejected():
    assert _field_errors(_valid_plan(claimed_by_handoff=123))
    assert _field_errors(_valid_plan(claimed_by_handoff=['a']))


def test_not_required():
    schema = json.loads(_PLAN_SCHEMA.read_text(encoding='utf-8'))
    assert 'claimed_by_handoff' in schema['properties']
    assert 'claimed_by_handoff' not in schema['required']
