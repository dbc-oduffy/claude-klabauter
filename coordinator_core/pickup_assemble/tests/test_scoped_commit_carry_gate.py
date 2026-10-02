"""pickup's terminal commit bypasses run_commit_pipeline, so it must run
carry_gate itself: a refused handoff never reaches `git add`."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_gates
from coordinator_core.pickup_assemble import apply as pickup_apply


def test_carry_gate_refusal_blocks_the_commit(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        commit_gates,
        "carry_gate",
        lambda root, paths: commit_gates.GateOutcome(passed=False, skipped=False, diagnostics=["bad carry"]),
    )
    monkeypatch.setattr(
        pickup_apply.apply_base, "scoped_commit", lambda *a, **k: calls.append(a) or "sha"
    )

    with pytest.raises(RuntimeError, match="carry_gate refused .*bad carry"):
        pickup_apply._scoped_commit(tmp_path, "state/handoffs/h.md", "handoff", "h.md", [])
    assert calls == []


def test_carry_gate_pass_commits(tmp_path, monkeypatch):
    monkeypatch.setattr(
        commit_gates, "carry_gate", lambda root, paths: commit_gates.GateOutcome(passed=True, skipped=True)
    )
    monkeypatch.setattr(pickup_apply.apply_base, "scoped_commit", lambda *a, **k: "sha")

    assert pickup_apply._scoped_commit(Path(tmp_path), "state/handoffs/h.md", "handoff", "h.md", []) == "sha"
