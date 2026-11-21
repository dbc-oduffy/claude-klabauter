"""Empty review prep on a resume whose owed rows are em-discharged or landed closes to the ledger re-judge; a halt never re-names its own refire."""

from __future__ import annotations

import shutil

import pytest

from coordinator_core.ops.dispatch_emit import halt_refire as hr
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_EM_DONE_FIELD
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT

_node = pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")


def test_prefix_twin_matches_the_halt_constant() -> None:
    assert hr.NO_SLICES_HALT_PREFIX == NO_SLICES_HALT


def test_gate_projects_the_em_discharged_blocking_items(tmp_path, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import ask_gate
    from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt

    sizing = {"requirement_register": {"rows": [{"id": "R1", "status": "open"}, {"id": "R2", "status": "open"}]}}
    halt = LedgerHalt(run_id="run-1", plan_rel="docs/plans/p.md", sizing_rel="s.yaml", blocking=("R1", "R2"))
    monkeypatch.setattr(ask_gate, "_discharged_em_items", lambda *_a: {"R1"})
    assert ask_gate._resume_ledger(sizing, halt, tmp_path)[RESUME_LEDGER_EM_DONE_FIELD] == ["R1"]
    monkeypatch.setattr(ask_gate, "_discharged_em_items", lambda *_a: set())
    assert RESUME_LEDGER_EM_DONE_FIELD not in ask_gate._resume_ledger(sizing, halt, tmp_path)
