"""status=closed <=> closed_reason present, routed through the real cross-field dispatch.

Registration is tested via `_apply_cross_field_rules`, since an unregistered rule
passes direct-call tests while inert at runtime.
"""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter import schema_validate as sv

SCHEMAS = ["plan", "initiative", "goal"]


@pytest.mark.parametrize("schema", SCHEMAS)
def test_closed_without_reason_is_refused(schema):
    errs = sv._apply_cross_field_rules({"status": "closed"}, schema)
    assert any(e["error"] == "required when status=closed" for e in errs)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_closed_with_blank_reason_is_refused(schema):
    errs = sv._apply_cross_field_rules({"status": "closed", "closed_reason": "  "}, schema)
    assert any(e["error"] == "required when status=closed" for e in errs)


@pytest.mark.parametrize("schema", SCHEMAS)
def test_closed_with_reason_passes(schema):
    fm = {"status": "closed", "closed_reason": "anything-doe-defines"}
    assert sv._apply_cross_field_rules(fm, schema) == []


@pytest.mark.parametrize("schema", SCHEMAS)
def test_abandoned_without_reason_passes(schema):
    assert sv._apply_cross_field_rules({"status": "abandoned"}, schema) == []


@pytest.mark.parametrize("schema", SCHEMAS)
def test_reason_without_closed_is_refused(schema):
    errs = sv._apply_cross_field_rules({"status": "abandoned", "closed_reason": "x"}, schema)
    assert any(e["error"] == "permitted only when status=closed" for e in errs)


def test_plan_keeps_both_quartet_rules():
    rules = sv._CROSS_FIELD_RULES_BY_SCHEMA["plan"]
    assert sv._cf_mise_prepped_stamp_quartet in rules
    assert sv._cf_execution_restamp_quartet in rules


def test_rule_never_returns_skip_sentinel():
    for fm in ({}, {"status": "closed"}, {"closed_reason": "x"}):
        out = sv._cf_status_closed_reason_required(fm)
        assert out is None or "__skip__" not in out
