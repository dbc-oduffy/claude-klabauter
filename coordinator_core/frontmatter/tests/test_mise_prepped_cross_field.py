"""
coordinator_core/frontmatter/tests/test_mise_prepped_cross_field.py

Subject: `_cf_mise_prepped_stamp_quartet` in `schema_validate.py`, and the
`'plan'` key in `_CROSS_FIELD_RULES_BY_SCHEMA` that makes it run.

WHY THE REGISTRATION HAS ITS OWN TEST. `_apply_cross_field_rules` resolves
`_CROSS_FIELD_RULES_BY_SCHEMA.get(schema_name, [])`. There was no `'plan'` key
before this rule existed, so a rule written and unit-tested by direct call would
pass every one of its own cases while being INERT at runtime — the exact failure
`_CUTOVER_CROSS_FIELD_RULES` records against itself. Half of this file therefore
routes through `validate('plan', ...)`, the real dispatch, never the rule
function.

Zero spawns; every case is an in-memory frontmatter dict.
"""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter import schema_validate as sv

CUTOFF_SAFE_CREATED = "2026-09-07"

_QUARTET = {
    "mise_prepped_by": "session-01",
    "mise_prepped_at": "2026-09-07T12:00:00Z",
    "mise_prepped_sha": "1958e194aa4c1c0b6d0a4b7c8d9e0f1a2b3c4d5e",
    "mise_prepped_findings": [],
}


def _plan_fm(**extra) -> dict:
    return {
        "title": "fixture",
        "author": "claude-klabauter-em",
        "status": "draft",
        "created": CUTOFF_SAFE_CREATED,
        **extra,
    }


def _errors(fm: dict) -> list:
    result = sv.validate("plan", fm)
    return result.get("errors", [])


def _quartet_errors(fm: dict) -> list:
    return [e for e in _errors(fm) if "mise_prepped" in e["field"]]


# ---------------------------------------------------------------------------
# The registration — without it the rule never runs
# ---------------------------------------------------------------------------


def test_the_plan_key_is_registered():
    assert "plan" in sv._CROSS_FIELD_RULES_BY_SCHEMA
    assert sv._cf_mise_prepped_stamp_quartet in sv._CROSS_FIELD_RULES_BY_SCHEMA["plan"]


def test_the_rule_fires_through_the_real_validate_dispatch():
    """Not by calling the rule function. `validate('plan', ...)` is the path every
    consumer takes, and it is the path a missing table key silently short-circuits."""
    errors = _quartet_errors(_plan_fm(mise_prepped_by="session-01"))
    assert len(errors) == 1
    assert errors[0]["error"] == "required when any mise_prepped_* field is present"


def test_the_plan_schema_x_schema_name_is_the_key_the_table_uses():
    """`_dispatch_validate` keys cross-field rules on `x-schema-name`. A table key
    that does not match it registers a rule nothing looks up."""
    schemas = sv.load_schemas(sv._SCHEMAS_DIR)
    assert schemas["plan"]["x-schema-name"] == "plan"


# ---------------------------------------------------------------------------
# Presence-symmetric, never presence-required
# ---------------------------------------------------------------------------


def test_an_unstamped_plan_is_not_an_error():
    """UNSTAMPED is a legitimate state, and the only state the whole corpus is in
    until the write op runs. There is no sibling flag that makes the quartet owed
    the way `handoff_phase: execution` does for the execution stamp — the trigger
    is one of its own members appearing."""
    assert _quartet_errors(_plan_fm()) == []


def test_the_full_quartet_validates():
    assert _quartet_errors(_plan_fm(**_QUARTET)) == []
    assert sv.validate("plan", _plan_fm(**_QUARTET)) == {"ok": True}


@pytest.mark.parametrize("present", sorted(_QUARTET))
def test_any_one_member_alone_owes_the_other_three(present):
    errors = _quartet_errors(_plan_fm(**{present: _QUARTET[present]}))
    assert len(errors) == 1
    named = errors[0]["field"]
    assert present not in named
    for other in _QUARTET:
        if other != present:
            assert other in named


@pytest.mark.parametrize("missing", sorted(_QUARTET))
def test_any_one_member_absent_is_a_partial_stamp(missing):
    fm = _plan_fm(**{k: v for k, v in _QUARTET.items() if k != missing})
    errors = _quartet_errors(fm)
    assert len(errors) == 1
    assert errors[0]["field"] == missing


# ---------------------------------------------------------------------------
# The declared-empty
# ---------------------------------------------------------------------------


def test_declared_empty_findings_counts_as_declared():
    """`mise_prepped_findings: []` is a declared-empty, structurally different
    from an absent key — the same distinction `writes: []` draws. Treating an
    empty list as missing would report every clean certification as a partial
    stamp, which is precisely inverted."""
    assert _quartet_errors(_plan_fm(**{**_QUARTET, "mise_prepped_findings": []})) == []


def test_a_null_findings_value_is_not_a_declaration():
    """A `null` fails the schema's own `type: array` too, so both errors land —
    the cross-field one is asserted here because it is the one that names the
    quartet rather than the type."""
    fm = _plan_fm(**{**_QUARTET, "mise_prepped_findings": None})
    errors = _quartet_errors(fm)
    assert any(
        e["error"] == "required when any mise_prepped_* field is present"
        and e["field"] == "mise_prepped_findings"
        for e in errors
    )


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_scalar_is_not_a_declaration(blank):
    fm = _plan_fm(**{**_QUARTET, "mise_prepped_by": blank})
    errors = _quartet_errors(fm)
    assert len(errors) == 1
    assert errors[0]["field"] == "mise_prepped_by"


# ---------------------------------------------------------------------------
# The cutoff
# ---------------------------------------------------------------------------


def test_a_plan_created_before_the_cutoff_is_exempt():
    """Going-forward only, mirroring `_cf_category_required_post_cutoff`. The
    any-of-four trigger already excludes the historical corpus; the cutoff's only
    live effect is exempting a backdated going-forward plan."""
    fm = _plan_fm(mise_prepped_by="session-01")
    fm["created"] = "2026-01-01"
    assert _quartet_errors(fm) == []


def test_a_plan_with_no_created_date_is_still_checked():
    fm = _plan_fm(mise_prepped_by="session-01")
    del fm["created"]
    assert len(_quartet_errors(fm)) == 1


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------


def test_the_hint_names_the_declared_empty_and_routes_to_the_op():
    """Register: one fact, once, plus the terse alternative. No override key —
    there is no way to satisfy this rule except by writing all four."""
    hint = _quartet_errors(_plan_fm(mise_prepped_by="session-01"))[0]["hint"]
    assert "mise_prepped_findings: []" in hint
    assert "plan.stamp_prepped" in hint
    for forbidden in ("you can", "simply", "just ", "don't worry", "override"):
        assert forbidden not in hint.lower()


# ---------------------------------------------------------------------------
# No collateral
# ---------------------------------------------------------------------------


def test_the_new_plan_rule_set_does_not_reach_other_schemas():
    """The rule is keyed on `plan` alone. A handoff carrying the same field names
    is not this rule's business — and no other schema declares them."""
    assert sv._CROSS_FIELD_RULES_BY_SCHEMA["plan"] == [sv._cf_mise_prepped_stamp_quartet]
    for name, rules in sv._CROSS_FIELD_RULES_BY_SCHEMA.items():
        if name != "plan":
            assert sv._cf_mise_prepped_stamp_quartet not in rules


def test_the_schema_declares_all_four_fields():
    """The rule must be sequenced AFTER the schema field declaration, or the
    write-guard warns on every plan the op stamps."""
    properties = sv.load_schemas(sv._SCHEMAS_DIR)["plan"]["properties"]
    for field in sv._MISE_PREPPED_FIELDS:
        assert field in properties, f"{field} undeclared in the vendored plan schema"
