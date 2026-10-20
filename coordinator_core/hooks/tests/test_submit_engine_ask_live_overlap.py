"""A single ask whose declared writes overlap a live run's footprint is denied at submit."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import live_run_writes


def _payload(tmp_path: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(tmp_path),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args},
    }


def _emit_recorder(tmp_path: Path, seen: list):
    def emit(params: dict, root: Path) -> dict:
        seen.append(params)
        return {"path": str(tmp_path / "run.js"), "run_id": "r1", "fire_args": {}}

    return emit


def _live(monkeypatch, footprints: dict):
    monkeypatch.setattr(live_run_writes, "live_footprints", lambda root, *, exclude_run_id: footprints)


def test_overlapping_declared_writes_are_denied_naming_run_and_path(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _live(monkeypatch, {"run-a": frozenset({"pkg/mod.py"})})
    seen: list = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "fix it", "source": "pm", "writes": ["pkg/mod.py"]}), emit=_emit_recorder(tmp_path, seen))
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "run-a" in reason and "pkg/mod.py" in reason and "Resubmit when run-a lands" in reason
    assert seen == []


def test_disjoint_writes_and_unstaged_runs_are_allowed(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _live(monkeypatch, {"run-a": frozenset({"other/x.py"}), "run-b": None})
    seen: list = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "fix it", "source": "pm", "writes": ["pkg/mod.py"]}), emit=_emit_recorder(tmp_path, seen))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(seen) == 1


def test_no_declared_writes_allows_without_reading_live_runs(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    def boom(root, *, exclude_run_id):
        raise AssertionError("live runs read with no declared writes")

    monkeypatch.setattr(live_run_writes, "live_footprints", boom)
    seen: list = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "fix it", "source": "pm"}), emit=_emit_recorder(tmp_path, seen))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_sizing_plan_spine_writes_are_declared_writes(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "p.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.cross_plan_write_overlap._declared_write_paths",
        lambda plan: {"pkg/mod.py"},
    )
    _live(monkeypatch, {"run-a": frozenset({"pkg"})})
    assert mod._declared_writes(tmp_path, {"args": {}}, {"plan": "docs/plans/p.md"}) == ["pkg/mod.py"]
    why = mod._live_write_overlap_deny(tmp_path, {"args": {}}, {"plan": "docs/plans/p.md"})
    assert why and "run-a" in why


def test_plan_edge_outside_docs_plans_declares_nothing(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "elsewhere.md").write_text("x", encoding="utf-8")
    assert mod._declared_writes(tmp_path, {"args": {}}, {"plan": "elsewhere.md"}) == []
