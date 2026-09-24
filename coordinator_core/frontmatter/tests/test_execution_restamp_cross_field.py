"""
coordinator_core/frontmatter/tests/test_execution_restamp_cross_field.py

Subject: `_cf_execution_restamp_quartet` in `schema_validate.py`, and its
registration in both `_PLAN_CROSS_FIELD_RULES` and `_HANDOFF_CROSS_FIELD_RULES`.

Mirrors test_mise_prepped_cross_field.py's structure (registration test, then
presence-symmetric quartet tests, then the cutoff), kept in its own file so it
stays disjoint from C1's exec_auth_stamp tests and C3's handoff_phase_stamp
tests (docs/plans/2026-09-23-exec-authorized-restamp-shape.md, C2).

Two additional constraints beyond the mise-prep shape:
  - the quartet requires a prior execution_authorized_by/_sha pair;
  - execution_restamped_from_sha must differ from execution_authorized_sha.

Zero spawns; every case is an in-memory frontmatter dict.
"""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter import schema_validate as sv

CUTOFF_SAFE_CREATED = "2026-09-23"

_AUTH = {
    "execution_authorized_by": "PM",
    "execution_authorized_at": "2026-09-20",
    "execution_authorized_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "execution_authorized_note": "looks good",
}

_QUARTET = {
    "execution_restamped_by": "EM:session-01",
    "execution_restamped_at": "2026-09-23",
    "execution_restamped_from_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "execution_restamped_note": "corrected D8",
}


def _plan_fm(**extra) -> dict:
    return {
        "title": "fixture",
        "author": "claude-klabauter-em",
        "status": "draft",
        "created": CUTOFF_SAFE_CREATED,
        **extra,
    }


def _handoff_fm(**extra) -> dict:
    return {
        "title": "fixture handoff",
        "created": CUTOFF_SAFE_CREATED,
        "branch": "work/test",
        "status": "active",
        "predecessor": None,
        "kind": "session-handoff",
        **extra,
    }


def _errors(schema_name: str, fm: dict) -> list:
    result = sv.validate(schema_name, fm)
    return result.get("errors", [])


def _quartet_errors(schema_name: str, fm: dict) -> list:
    return [
        e for e in _errors(schema_name, fm)
        if "execution_restamped" in e["field"] or "execution_authorized" in e["field"]
    ]


# ---------------------------------------------------------------------------
# The registration — without it the rule never runs, on either schema
# ---------------------------------------------------------------------------


def test_the_plan_and_handoff_keys_are_registered():
    assert sv._cf_execution_restamp_quartet in sv._CROSS_FIELD_RULES_BY_SCHEMA["plan"]
    assert sv._cf_execution_restamp_quartet in sv._CROSS_FIELD_RULES_BY_SCHEMA["handoff"]


def test_the_rule_fires_through_the_real_validate_dispatch():
    errors = _quartet_errors("plan", _plan_fm(execution_restamped_by="EM:session-01"))
    assert len(errors) == 1
    assert errors[0]["error"] == "required when any execution_restamped_* field is present"


# ---------------------------------------------------------------------------
# Presence-symmetric, never presence-required
# ---------------------------------------------------------------------------


def test_a_plan_with_no_restamp_fields_is_not_an_error():
    assert _quartet_errors("plan", _plan_fm(**_AUTH)) == []


def test_a_handoff_with_no_restamp_fields_is_not_an_error():
    assert _quartet_errors("handoff", _handoff_fm(**_AUTH)) == []


def test_the_full_quartet_validates_on_a_plan():
    assert _quartet_errors("plan", _plan_fm(**_AUTH, **_QUARTET)) == []


def test_the_full_quartet_validates_on_a_handoff():
    assert _quartet_errors("handoff", _handoff_fm(**_AUTH, **_QUARTET)) == []


@pytest.mark.parametrize("present", sorted(_QUARTET))
def test_any_one_member_alone_owes_the_other_three(present):
    errors = _quartet_errors("plan", _plan_fm(**_AUTH, **{present: _QUARTET[present]}))
    assert len(errors) == 1
    named = errors[0]["field"]
    assert present not in named
    for other in _QUARTET:
        if other != present:
            assert other in named


@pytest.mark.parametrize("missing", sorted(_QUARTET))
def test_any_one_member_absent_is_a_partial_stamp(missing):
    fm = _plan_fm(**_AUTH, **{k: v for k, v in _QUARTET.items() if k != missing})
    errors = _quartet_errors("plan", fm)
    assert len(errors) == 1
    assert errors[0]["field"] == missing


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_scalar_is_not_a_declaration(blank):
    fm = _plan_fm(**_AUTH, **{**_QUARTET, "execution_restamped_by": blank})
    errors = _quartet_errors("plan", fm)
    assert len(errors) == 1
    assert errors[0]["field"] == "execution_restamped_by"


# ---------------------------------------------------------------------------
# A restamp needs a prior authorization
# ---------------------------------------------------------------------------


def test_a_complete_quartet_with_no_prior_authorization_is_rejected():
    fm = _plan_fm(**_QUARTET)
    errors = _quartet_errors("plan", fm)
    assert len(errors) == 1
    assert errors[0]["field"] == "execution_authorized_by, execution_authorized_sha"
    assert errors[0]["error"] == "required when the execution_restamped_* quartet is present"


def test_a_complete_quartet_missing_only_authorized_sha_is_rejected():
    fm = _plan_fm(execution_authorized_by="PM", **_QUARTET)
    errors = _quartet_errors("plan", fm)
    assert len(errors) == 1
    assert errors[0]["field"] == "execution_authorized_sha"


# ---------------------------------------------------------------------------
# _from_sha must differ from the live authorized sha — a rebind-to-nothing
# is the shape the revert-to-witnessed-sha path removes the quartet for
# ---------------------------------------------------------------------------


def test_from_sha_equal_to_authorized_sha_is_rejected():
    fm = _plan_fm(
        **_AUTH,
        **{**_QUARTET, "execution_restamped_from_sha": _AUTH["execution_authorized_sha"]},
    )
    errors = _quartet_errors("plan", fm)
    assert len(errors) == 1
    assert errors[0]["field"] == "execution_restamped_from_sha"
    assert errors[0]["error"] == "must differ from execution_authorized_sha"


def test_from_sha_different_from_authorized_sha_validates():
    assert _quartet_errors("plan", _plan_fm(**_AUTH, **_QUARTET)) == []


# ---------------------------------------------------------------------------
# The cutoff
# ---------------------------------------------------------------------------


def test_a_plan_created_before_the_cutoff_is_exempt():
    fm = _plan_fm(execution_restamped_by="EM:session-01")
    fm["created"] = "2026-01-01"
    assert _quartet_errors("plan", fm) == []


def test_a_plan_with_no_created_date_is_still_checked():
    fm = _plan_fm(execution_restamped_by="EM:session-01")
    del fm["created"]
    assert len(_quartet_errors("plan", fm)) == 1


# ---------------------------------------------------------------------------
# No collateral onto other schemas
# ---------------------------------------------------------------------------


def test_the_rule_does_not_reach_other_schemas():
    for name, rules in sv._CROSS_FIELD_RULES_BY_SCHEMA.items():
        if name not in ("plan", "handoff"):
            assert sv._cf_execution_restamp_quartet not in rules
