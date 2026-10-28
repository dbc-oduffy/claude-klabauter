"""dispatch.ask_gate refuses under 500ms, naming the holder, when the sizing's lock is held."""

from __future__ import annotations

import time

import pytest
import yaml

from coordinator_core.locked_write import held_lock
from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL

REL = "state/sizings/2026-10-01-gate.yaml"
RUN = "warp-fixture-run"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_gate, "main_worktree_root", lambda p: p)
    doc = {
        "schema": "sizing-object",
        "name": "gate fixture",
        "intent": "exercise the gate",
        "estimate": {"tshirt": "S", "provisional": True},
        "route": "spec-dispatch",
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def test_held_sizing_lock_refuses_under_budget_naming_holder(repo):
    with held_lock(repo / REL, anchor_root=repo, holder_label="abandoned-run-thread"):
        start = time.monotonic()
        out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
        elapsed = time.monotonic() - start
    assert out["halt"]["kind"] == HALT_REFUSAL
    reason = out["halt"]["reason"]
    assert "lock held" in reason and REL in reason and "abandoned-run-thread" in reason
    assert elapsed < 0.5


def test_free_sizing_lock_does_not_refuse(repo):
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert "lock held" not in str((out.get("halt") or {}).get("reason", ""))


def test_gate_waits_no_longer_than_the_budget_on_a_held_spine_lock(repo):
    plan = repo / "state" / "plans" / "spine.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("x\n", encoding="utf-8")
    with held_lock(plan, anchor_root=repo, holder_label="other-run"):
        start = time.monotonic()
        error = ask_gate._append_spine_row(repo, "state/plans/spine.md", {"id": "R1"})
        elapsed = time.monotonic() - start
    assert error is not None and "lock held" in error and "other-run" in error
    assert elapsed < 0.5
