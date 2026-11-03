"""review.prep: the handler attaches `legs` naming the over-floor phase, and the reply is unchanged."""

from __future__ import annotations

import asyncio
import subprocess
import time

from coordinator_core import ipc
from coordinator_core.ops import review_prep as rp
from coordinator_core.ops import review_prep_legs
from coordinator_core.telemetry import op_row_fields

BASE = "a" * 40


def _spin(ms: float) -> None:
    end = time.thread_time() + ms / 1000.0
    while time.thread_time() < end:
        pass


def _stub(tmp_path, monkeypatch, foreign):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(rp, "run_git", boom)
    monkeypatch.setattr(rp, "freeze_diff", lambda *a, **k: {"diff_path": str(tmp_path / "w.diff"), "error": None})
    monkeypatch.setattr(rp, "diff_product_files", lambda p: ["src/a.py"])
    monkeypatch.setattr(
        rp, "partition_slices", lambda *a, **k: {"verdict": "single-reviewer-ok", "slices": [], "error": None}
    )
    monkeypatch.setattr(rp, "foreign_claims", foreign)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"a = 1\n")
    (tmp_path / "plan.md").write_text("---\nplan_id: p\n---\n", encoding="utf-8")


def _call(tmp_path, slice_prefix):
    params = {
        "base": BASE, "paths": ["src/a.py"], "worktree": True, "slice_prefix": slice_prefix,
        "plan_path": "plan.md", "session_id": "sess-legs",
    }

    async def go():
        ot = op_row_fields.begin()
        ct = ipc._op_cpu_secs_var.set([])
        try:
            reply = await asyncio.to_thread(rp._handler, params, tmp_path)
        finally:
            ipc._op_cpu_secs_var.reset(ct)
        return reply, op_row_fields.drain(ot)

    return asyncio.run(go())


def test_slow_leg_is_named_in_the_drained_row(tmp_path, monkeypatch):
    def slow(*a, **k):
        _spin(120)
        return []

    _stub(tmp_path, monkeypatch, slow)
    reply, fields = _call(tmp_path, "legs-slow")
    assert reply["error"] is None
    assert fields["legs"][0]["name"] == "foreign_claims"
    assert fields["legs"][0]["name"] in review_prep_legs.LEGS
    assert fields["legs"][0]["ms"] >= 50.0


def test_fast_call_carries_no_legs_and_same_reply(tmp_path, monkeypatch):
    _stub(tmp_path, monkeypatch, lambda *a, **k: [])
    reply, fields = _call(tmp_path, "legs-fast")
    assert "legs" not in fields
    assert reply["error"] is None
    assert reply["foreign_claims"] == []
    assert reply["sidecar_path"].endswith("legs-fast.review-prep.md")
