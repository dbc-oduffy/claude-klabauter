"""A research-mode prior-art-check (carries `verdict`) names no plan; a plan-sidecar one still must."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_SCHEMA = Path(__file__).parent.parent / "schemas" / "prior-art-check.schema.json"


def _fields(errors) -> list[str]:
    return [e["field"] for e in errors]


def test_research_mode_check_without_plan_is_accepted():
    fm = {"kind": "prior-art-check", "reviewer": "coordinator:prior-art-checker", "verdict": "DEGRADED"}
    assert validate_frontmatter(fm, _SCHEMA) == []


def test_plan_sidecar_check_without_plan_is_still_refused():
    fm = {"kind": "prior-art-check", "author": "x", "created": "2026-11-13", "status": "reviewed"}
    assert "plan" in _fields(validate_frontmatter(fm, _SCHEMA))


def test_plan_sidecar_check_with_plan_is_accepted():
    fm = {"plan": "docs/plans/2026-11-13-x.md", "conflicts": 0, "compatible": 1, "silent": 2}
    assert validate_frontmatter(fm, _SCHEMA) == []


@pytest.mark.parametrize("bad", [{"verdict": 3}, {"verdict": "OK", "reviewer": 7}])
def test_research_mode_fields_stay_typed(bad):
    assert validate_frontmatter({"kind": "prior-art-check", **bad}, _SCHEMA) != []
