"""
test_producer_axis_entity — parse/reject tests for `HandoffSummary.producer`
(C6a, `entities/summaries.py` `_HandoffProducer`).

Model + emit pass-through only — this module does not exercise a resolver
(none exists yet for this field; a separate chunk supplies it). Covers the
`extra="forbid"` / required-with-null pair (present-as-null passes,
key-omitted is rejected) plus the three-state distinguishability the field
exists to preserve: "no ceremony ran" (`op_identity: hand-authored`),
"session typed nothing this turn" (`typed_command: null`), and "the field
stopped resolving" (`typed_command: "unresolved"`).

Spec backlink: docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md § C6a.
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.entities.summaries import HandoffSummary
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_dump,
    zod_parse,
    zod_safe_parse_ok,
)

PROV = {
    "source_kind": "local_fs",
    "repo": "fixture-owner/fixture-repo",
    "ref": None,
    "path": "state/handoffs/fixture-handoff.md",
    "observed_at": "2026-08-12T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

VALID = {
    "repo": "fixture-owner/fixture-repo",
    "coordinator_root_path": ".",
    "title": "Fixture Handoff",
    "created": "2026-08-12",
    "status": "open",
    "kind": "session-handoff",
    "baton_class": "continuation",
    "deployment_state": "ready_to_fire",
    "workstream": "producer-axis",
    "predecessor": "none",
    "scope": [],
    "claimed_by": None,
    "claimed_at": None,
    "continued_into": None,
    "closed_reason": None,
    "shipped_in": None,
    "picked_up_by": None,
    "acceptance_criteria": None,
    "provenance": PROV,
    "deliverable_id": None,
    "plan_id": None,
    "initiative": None,
    "caption": None,
    "status_reason": None,
    "owner": None,
    "last_meaningful_activity": None,
    "workstream_type": None,
    "shipped_sha": None,
    "deliverable_status": None,
    "origin_session": None,
    "origin_handoff": None,
    "origin_plan_id": None,
    "origin_goal_id": None,
    "roadmap_id": None,
    "handoff_id": "hnd-fixture-000000",
    "handoff_id_derivation": "derived",
    "pm_priority": None,
    "pm_priority_origin": None,
    "pm_priority_source_id": None,
    "suggested_priority": None,
    "producer": None,
}


def test_handoff_summary_valid_record_parses():
    zod_parse(HandoffSummary, VALID)


def test_producer_present_as_null_passes():
    """Required-with-null: the field itself may be an explicit null."""
    v = {**VALID, "producer": None}
    assert zod_safe_parse_ok(HandoffSummary, v)


def test_producer_key_omitted_entirely_rejected():
    """Required-with-null means present-as-null, never an absent key."""
    v = {k: val for k, val in VALID.items() if k != "producer"}
    assert not zod_safe_parse_ok(HandoffSummary, v)


def test_producer_op_minted_with_typed_command_round_trips():
    v = {
        **VALID,
        "producer": {"op_identity": "machine-minted", "typed_command": "queue_scaffold_baton"},
    }
    parsed = zod_parse(HandoffSummary, v)
    dumped = zod_dump(HandoffSummary, parsed)
    zod_parse(HandoffSummary, dumped)
    assert dumped["producer"] == {
        "op_identity": "machine-minted",
        "typed_command": "queue_scaffold_baton",
    }


def test_producer_hand_authored_with_typed_command_null_state():
    """State 1 — "no ceremony ran": op_identity is hand-authored, independent
    of whatever (if anything) the session typed."""
    v = {
        **VALID,
        "producer": {"op_identity": "hand-authored", "typed_command": None},
    }
    assert zod_safe_parse_ok(HandoffSummary, v)


def test_producer_machine_minted_nothing_typed_null_state():
    """State 2 — "session typed nothing this turn": typed_command is null,
    distinct from the hand-authored case above by op_identity."""
    v = {
        **VALID,
        "producer": {"op_identity": "machine-minted", "typed_command": None},
    }
    assert zod_safe_parse_ok(HandoffSummary, v)


def test_producer_unresolved_capture_failure_state():
    """State 3 — "the field stopped resolving": the unresolved sentinel,
    distinguishable from both null states above."""
    v = {
        **VALID,
        "producer": {"op_identity": "machine-minted", "typed_command": "unresolved"},
    }
    assert zod_safe_parse_ok(HandoffSummary, v)


def test_producer_other_command_sentinel_parses():
    v = {
        **VALID,
        "producer": {"op_identity": "hand-authored", "typed_command": "other-command"},
    }
    assert zod_safe_parse_ok(HandoffSummary, v)


def test_producer_rejects_unknown_op_identity():
    v = {
        **VALID,
        "producer": {"op_identity": "ai-authored", "typed_command": None},
    }
    assert not zod_safe_parse_ok(HandoffSummary, v)


def test_producer_typed_command_key_omitted_rejected():
    """`_HandoffProducer` is `extra="forbid"` with no default on either
    field — an absent `typed_command` key is a validation failure, same
    required-with-null discipline as the outer `producer` field."""
    v = {**VALID, "producer": {"op_identity": "machine-minted"}}
    assert not zod_safe_parse_ok(HandoffSummary, v)


def test_producer_rejects_extra_field():
    v = {
        **VALID,
        "producer": {
            "op_identity": "machine-minted",
            "typed_command": None,
            "unexpected_field": "nope",
        },
    }
    assert not zod_safe_parse_ok(HandoffSummary, v)
