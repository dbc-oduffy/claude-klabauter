"""Schema coverage for plan.schema.json's optional `register_claims.pins`.

NEGATIVE SPEC: nothing here asserts a pin matches a sizing row; JSON Schema
checks shape only. Drift detection lives in requirement_register.pin_drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_PLAN_SCHEMA = Path(__file__).parent.parent / 'schemas' / 'plan.schema.json'
_GOOD = 'sha256:' + 'a' * 64


def _plan(**claims_extra) -> dict:
    claims = {'sizing': 'state/sizings/x.yaml', 'rows': ['R1']}
    claims.update(claims_extra)
    return {
        'title': 'Test plan',
        'created': '2026-10-14',
        'author': 'test-em',
        'status': 'draft',
        'register_claims': claims,
    }


def test_absent_pins_validates():
    assert validate_frontmatter(_plan(), _PLAN_SCHEMA) == []


def test_valid_pins_validate():
    assert validate_frontmatter(_plan(pins={'R1': _GOOD}), _PLAN_SCHEMA) == []


@pytest.mark.parametrize(
    'pins',
    [
        {'R1': 'md5:' + 'a' * 32},
        {'R1': 'sha256:' + 'A' * 64},
        {'R1': 'sha256:' + 'a' * 63},
        {'R1': 7},
        {'-bad id': _GOOD},
    ],
)
def test_malformed_pins_rejected(pins):
    assert validate_frontmatter(_plan(pins=pins), _PLAN_SCHEMA) != []
