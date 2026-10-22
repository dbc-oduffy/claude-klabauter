"""A ledger-check resume admits ids the halt pointer records as ruled."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import ask_contract, sizing_fire
from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt


def _setup(monkeypatch, tmp_path, ruled, staged=("R1",)):
    pointer = LedgerHalt("run1", "plan.md", "sizing.yaml", ("R1",), ruled=tuple(ruled))
    record = SimpleNamespace(
        sizing_rel="sizing.yaml", plan_rel="plan.md", staged=frozenset(staged),
        blocking=("R1",), remaining={},
    )
    state = SimpleNamespace(ledger_run="run1", ledger_sha="abc", run_halts={})
    monkeypatch.setattr(sizing_fire, "_own_pointer", lambda *a, **k: pointer)
    monkeypatch.setattr(sizing_fire, "_terminal_coded", lambda *a, **k: False)
    monkeypatch.setattr(sizing_fire, "_sha_coded", lambda *a, **k: False)
    monkeypatch.setattr(sizing_fire, "_carry", lambda root, plan, halt, rh: halt)
    monkeypatch.setattr(ask_contract, "read_halt_record", lambda *a, **k: record)
    return state


def test_ruled_id_missing_from_staged_still_resumes_and_blocks(monkeypatch, tmp_path):
    state = _setup(monkeypatch, tmp_path, ("R9",))
    halt = sizing_fire._ledger_halt_for(
        tmp_path, "plan.md", "sizing.yaml", [], ["R1", "R9"], state
    )
    assert halt is not None
    assert "R9" in halt.blocking and "R1" in halt.blocking


def test_no_ruled_id_missing_from_staged_is_none(monkeypatch, tmp_path):
    state = _setup(monkeypatch, tmp_path, ())
    assert sizing_fire._ledger_halt_for(
        tmp_path, "plan.md", "sizing.yaml", [], ["R1", "R9"], state
    ) is None
