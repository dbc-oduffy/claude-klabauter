"""
test_session_hierarchy — parse/reject tests for the SessionHierarchy entity (ccos-5).

Pytest port of DoE `coordinator/cockpit-contract/test/session-hierarchy.test.ts`.

Spec backlinks:
  - docs/plans/2026-06-30-ccos-8-cockpit-read-contract-spine-entities.md § C2
  - docs/wiki/cockpit-contract-entity-addition-protocol.md §Steps (Step 9)
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.entities.session_hierarchy import (
    SessionHierarchy,
)
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_parse,
    zod_safe_parse_ok,
)

PROV = {
    "source_kind": "coordinator_artifact",
    "repo": ".example-doctrine-mirror-repo",
    "ref": None,
    "path": "state/session-hierarchy.test.json",
    "observed_at": "2026-06-30T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

VALID = {
    "repo": ".example-doctrine-mirror-repo",
    "coordinator_root_path": ".",
    "session_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "session_type": "session",
    "workstream": "ccos-8-spine-entities",
    "branch": "work/machine-a/2026-06-30",
    "parent_session_id": "f0e1d2c3-b4a5-6789-fedc-ba0987654321",
    "linked_handoffs": ["state/handoffs/2026-06-30_120000_ccos-8-kickoff.md"],
    "created_by_session": "f0e1d2c3-b4a5-6789-fedc-ba0987654321",
    "provenance_completeness": "complete",
    "capture_source": "derived_handoff_lineage",
    "completeness": "complete",
    "provenance": PROV,
}


def test_valid_record_parses():
    zod_parse(SessionHierarchy, VALID)


def test_all_nullable_as_null_parses():
    nulled = {
        **VALID,
        "branch": None,
        "parent_session_id": None,
        "linked_handoffs": None,
        "created_by_session": None,
        "provenance_completeness": None,
        "capture_source": None,
        "completeness": None,
    }
    zod_parse(SessionHierarchy, nulled)


def test_invalid_session_type_enum_rejected():
    assert not zod_safe_parse_ok(SessionHierarchy, {**VALID, "session_type": "standup"})


def test_all_valid_session_type_values_accepted():
    for t in ["session", "workstream", "blitz"]:
        assert zod_safe_parse_ok(SessionHierarchy, {**VALID, "session_type": t})


def test_missing_session_id_rejected():
    bad = dict(VALID)
    del bad["session_id"]
    assert not zod_safe_parse_ok(SessionHierarchy, bad)


def test_missing_workstream_rejected():
    bad = dict(VALID)
    del bad["workstream"]
    assert not zod_safe_parse_ok(SessionHierarchy, bad)


def test_missing_session_type_rejected():
    bad = dict(VALID)
    del bad["session_type"]
    assert not zod_safe_parse_ok(SessionHierarchy, bad)


def test_d9_nullable_field_omitted_entirely_rejected():
    bad = dict(VALID)
    del bad["branch"]
    assert not zod_safe_parse_ok(SessionHierarchy, bad)


def test_invalid_provenance_completeness_enum_rejected():
    assert not zod_safe_parse_ok(
        SessionHierarchy, {**VALID, "provenance_completeness": "partial"}
    )


def test_invalid_completeness_enum_rejected():
    assert not zod_safe_parse_ok(SessionHierarchy, {**VALID, "completeness": "invalid-value"})
