"""
Tests for the re-vendored plan.schema.json's `prime_exit_criterion.falsifier` /
`exit_criterion_met` shapes (canonical landed at a976f694d).

Coverage targets:
  - A minimal plan (no falsifier, no exit_criterion_met) still validates — these
    fields are schema-level optional (proportionality is a read-side check
    against `sizing_object`, never structural here).
  - `prime_exit_criterion.falsifier` requires all of `how`, `baseline_output`,
    `baseline_ref`, `expected_when_true` when present.
  - `exit_criterion_met` requires only `asserted`; `asserted: false` requires
    `reason` via the schema's `allOf`/`if`/`then` cross-field rule.
  - No `digest_algo` / `observed_digest` anywhere in the vendored schema — the
    canonical landed WITHOUT the two properties this plan originally proposed,
    and this is a negative-spec regression guard against ever reintroducing
    them by hand.

Spec backlink: state/dispatch-briefs/2026-08-27-the-close-ceremony-refuses-a-
goal-nothing-observed/C3b.md
"""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"
_PLAN_SCHEMA = _SCHEMAS_DIR / "plan.schema.json"


def _valid_plan(**overrides) -> dict:
    """Minimal valid plan dict (required: title, created, author, status)."""
    base = {
        "title": "Test plan",
        "created": "2026-08-06",
        "author": "test-em",
        "status": "draft",
    }
    base.update(overrides)
    return base


def _valid_falsifier(**overrides) -> dict:
    base = {
        "how": "pytest coordinator_core/frontmatter/tests/test_x.py -q",
        "baseline_output": "1 failed, 0 passed",
        "baseline_ref": "abc1234",
        "expected_when_true": "0 failed, 1 passed",
    }
    base.update(overrides)
    return base


class TestFalsifierIsSchemaOptional:
    def test_minimal_plan_with_no_falisifer_fields_is_valid(self):
        errors = validate_frontmatter(_valid_plan(), _PLAN_SCHEMA)
        assert errors == []

    def test_plan_with_prime_exit_criterion_but_no_falsifier_is_valid(self):
        fm = _valid_plan(
            prime_exit_criterion={
                "statement": "The engine warms in under 50ms.",
                "derived_from": "state/goals/example-goal.yaml#kr-latency",
            }
        )
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors == []


class TestFalsifierRequiredFields:
    def test_falsifier_with_all_required_fields_is_valid(self):
        fm = _valid_plan(
            prime_exit_criterion={
                "statement": "The engine warms in under 50ms.",
                "derived_from": "state/goals/example-goal.yaml#kr-latency",
                "falsifier": _valid_falsifier(),
            }
        )
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors == []

    def test_falsifier_missing_how_is_invalid(self):
        falsifier = _valid_falsifier()
        del falsifier["how"]
        fm = _valid_plan(
            prime_exit_criterion={
                "statement": "The engine warms in under 50ms.",
                "derived_from": "state/goals/example-goal.yaml#kr-latency",
                "falsifier": falsifier,
            }
        )
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors != []

    def test_falsifier_missing_baseline_output_is_invalid(self):
        falsifier = _valid_falsifier()
        del falsifier["baseline_output"]
        fm = _valid_plan(
            prime_exit_criterion={
                "statement": "The engine warms in under 50ms.",
                "derived_from": "state/goals/example-goal.yaml#kr-latency",
                "falsifier": falsifier,
            }
        )
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors != []

    def test_falsifier_missing_baseline_ref_is_invalid(self):
        falsifier = _valid_falsifier()
        del falsifier["baseline_ref"]
        fm = _valid_plan(
            prime_exit_criterion={
                "statement": "The engine warms in under 50ms.",
                "derived_from": "state/goals/example-goal.yaml#kr-latency",
                "falsifier": falsifier,
            }
        )
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors != []

    def test_falsifier_missing_expected_when_true_is_invalid(self):
        falsifier = _valid_falsifier()
        del falsifier["expected_when_true"]
        fm = _valid_plan(
            prime_exit_criterion={
                "statement": "The engine warms in under 50ms.",
                "derived_from": "state/goals/example-goal.yaml#kr-latency",
                "falsifier": falsifier,
            }
        )
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors != []


class TestExitCriterionMet:
    def test_asserted_true_alone_is_valid(self):
        fm = _valid_plan(exit_criterion_met={"asserted": True})
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors == []

    def test_asserted_false_without_reason_is_invalid(self):
        fm = _valid_plan(exit_criterion_met={"asserted": False})
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors != []

    def test_asserted_false_with_reason_is_valid(self):
        fm = _valid_plan(
            exit_criterion_met={
                "asserted": False,
                "reason": "Falsifier still fails; work continues in follow-on plan.",
            }
        )
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors == []

    def test_exit_criterion_met_missing_asserted_is_invalid(self):
        fm = _valid_plan(exit_criterion_met={"prose": "no asserted field here"})
        errors = validate_frontmatter(fm, _PLAN_SCHEMA)
        assert errors != []


class TestNoDigestFieldsVendored:
    """Negative-spec guard: the canonical landed WITHOUT `digest_algo` /
    `observed_digest` — this plan's original proposal — per C3b's memo
    withdrawing that ask. A hand-edit reintroducing either field would pass
    silently without this regression guard."""

    def test_schema_text_has_no_digest_algo(self):
        text = _PLAN_SCHEMA.read_text(encoding="utf-8")
        assert "digest_algo" not in text

    def test_schema_text_has_no_observed_digest(self):
        text = _PLAN_SCHEMA.read_text(encoding="utf-8")
        assert "observed_digest" not in text

    def test_falsifier_output_field_is_raw_not_digested(self):
        data = json.loads(_PLAN_SCHEMA.read_text(encoding="utf-8"))
        exit_criterion_met = data["properties"]["exit_criterion_met"]
        assert "falsifier_output" in exit_criterion_met["properties"]
        assert "observed_digest" not in exit_criterion_met["properties"]
