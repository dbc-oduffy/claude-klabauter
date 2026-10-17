"""A landed ledger-check halt records each blocking item's remaining work and evidence; other halts do not."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_BLOCKING_ITEMS_FIELD,
    HALT_PLAN_FIELD,
    read_halt_record,
)
from coordinator_core.ops.dispatch_emit.halted_record_commit import _EVIDENCE_CAP_BYTES
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_halted_marker_records_blocking import (  # noqa: F401
    PLAN,
    RUN,
    _land,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_ledger_halt_records_remaining_and_evidence_and_reads_back(repo):
    items = [
        {"id": "R1", "remaining": ["wire the gate", 7, "pin it"], "evidence": "falsifier still fails", "extra": "x"},
        {"id": "R2"},
        {"remaining": ["no id"]},
    ]
    marker = _land(repo, {"halted": "ledger-check", "reason": "r", HALT_PLAN_FIELD: PLAN, "items": items})
    assert marker[HALT_BLOCKING_ITEMS_FIELD] == [
        {"id": "R1", "remaining": ["wire the gate", "pin it"], "evidence": "falsifier still fails"},
        {"id": "R2", "remaining": [], "evidence": ""},
    ]
    record = read_halt_record(repo, RUN)
    assert record is not None
    assert record.remaining["R1"] == {"remaining": ["wire the gate", "pin it"], "evidence": "falsifier still fails"}
    assert record.remaining["R2"] == {"remaining": [], "evidence": ""}


def test_evidence_is_capped_in_bytes(repo):
    marker = _land(
        repo,
        {"halted": "ledger-check", "reason": "r", HALT_PLAN_FIELD: PLAN,
         "items": [{"id": "R1", "remaining": [], "evidence": "é" * 5000}]},
    )
    kept = marker[HALT_BLOCKING_ITEMS_FIELD][0]["evidence"]
    assert 0 < len(kept.encode("utf-8")) <= _EVIDENCE_CAP_BYTES


def test_non_ledger_halt_carries_no_items_key(repo):
    marker = _land(repo, {"halted": "refusal", "reason": "r", HALT_PLAN_FIELD: PLAN, "items": [{"id": "R1", "remaining": ["x"]}]})
    assert HALT_BLOCKING_ITEMS_FIELD not in marker


def test_marker_writes_items_only_when_given(tmp_path):
    bare = write_halt_marker(tmp_path / "a", halted="ledger-check", sizing_rel="state/sizings/x.yaml")
    assert HALT_BLOCKING_ITEMS_FIELD not in json.loads(bare.read_text(encoding="utf-8"))
    given = write_halt_marker(
        tmp_path / "b", halted="ledger-check", sizing_rel="state/sizings/x.yaml",
        blocking_items=[{"id": "R1", "remaining": [], "evidence": ""}],
    )
    assert json.loads(given.read_text(encoding="utf-8"))[HALT_BLOCKING_ITEMS_FIELD][0]["id"] == "R1"
