"""
test_priority_ledger_entity — parse/reject tests for `PriorityLedgerEntry`
(C6b, `entities/priority_ledger_entry.py`).

Spec backlink: docs/plans/2026-07-26-priority-ledger.md § C6b
Spec backlink: coordinator/schemas/priority-ledger.schema.json (example-doctrine-repo repo)
"""
from __future__ import annotations

from coordinator_core.contract.cockpit_schema.entities.priority_ledger_entry import (
    PriorityLedgerEntry,
)
from coordinator_core.contract.cockpit_schema.tests.conftest import (
    zod_parse,
    zod_safe_parse_ok,
)

PROV = {
    "source_kind": "local_fs",
    "repo": "fixture-owner/fixture-repo",
    "ref": None,
    "path": "state/priority-ledger/2026-07-01_100000_fixture-handoff.yaml",
    "observed_at": "2026-07-26T10:00:00Z",
    "derivation": "parsed",
    "entity_anchor": None,
}

VALID = {
    "repo": "fixture-owner/fixture-repo",
    "coordinator_root_path": ".",
    "target_id": "2026-07-01_100000_fixture-handoff",
    "target_kind": "handoff",
    "priority": "urgent",
    "source": "op",
    "set_by": "example-operator",
    "set_at": "2026-07-26T10:00:00Z",
    "source_repo": None,
    "note": "escalated after the sibling-notification incident",
    "provenance": PROV,
}


def test_priority_ledger_entry_valid_record_parses():
    zod_parse(PriorityLedgerEntry, VALID)


def test_priority_ledger_entry_nullable_fields_as_null_parses():
    nulled = {
        **VALID,
        "set_by": None,
        "set_at": None,
        "source_repo": None,
        "note": None,
    }
    zod_parse(PriorityLedgerEntry, nulled)


def test_priority_ledger_entry_explicit_clear_sentinel_parses():
    """`priority: "none"` is the EXPLICIT-CLEAR SENTINEL — a real authored
    row, not a deletion. Must parse like any other tier value."""
    cleared = {**VALID, "priority": "none"}
    zod_parse(PriorityLedgerEntry, cleared)


def test_priority_ledger_entry_external_intent_source_with_source_repo():
    external = {
        **VALID,
        "source": "external-intent",
        "source_repo": "example-cockpit-repo",
    }
    zod_parse(PriorityLedgerEntry, external)


def test_priority_ledger_entry_rejects_unknown_priority_tier():
    invalid = {**VALID, "priority": "P0"}
    assert not zod_safe_parse_ok(PriorityLedgerEntry, invalid)


def test_priority_ledger_entry_rejects_unknown_target_kind():
    invalid = {**VALID, "target_kind": "epic"}
    assert not zod_safe_parse_ok(PriorityLedgerEntry, invalid)


def test_priority_ledger_entry_rejects_unknown_source():
    invalid = {**VALID, "source": "cockpit-intent"}
    assert not zod_safe_parse_ok(PriorityLedgerEntry, invalid)


def test_priority_ledger_entry_rejects_missing_required_field():
    missing = {k: v for k, v in VALID.items() if k != "target_id"}
    assert not zod_safe_parse_ok(PriorityLedgerEntry, missing)


def test_priority_ledger_entry_rejects_resolved_from_field():
    """NEGATIVE-SPEC (2), priority-ledger.schema.json: no
    `inherited_from`/`resolved_from`/`priority_predecessor` — resolved-from
    provenance is derived-emission-layer only (HandoffSummary.pm_priority_*,
    C6a), never authored data on this entity."""
    tainted = {**VALID, "resolved_from": "some-ancestor-handoff"}
    assert not zod_safe_parse_ok(PriorityLedgerEntry, tainted)


def test_priority_ledger_entry_rejects_extra_field():
    extra = {**VALID, "unexpected_field": "nope"}
    assert not zod_safe_parse_ok(PriorityLedgerEntry, extra)
