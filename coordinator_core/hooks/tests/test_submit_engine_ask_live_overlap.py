"""A single ask whose declared writes overlap a live run's staged footprint is denied at submit.

The overlap check reads the live-run index; a missing or stalled index denies nothing."""

from __future__ import annotations

import time
from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halt_resume_record, live_run_index


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


def _live(root: Path, footprints: dict) -> None:
    """Write the index: a None footprint is a minted run that has not staged."""
    for run_id, footprint in footprints.items():
        if footprint is None:
            live_run_index.add(root, run_id, 1.0)
        else:
            live_run_index.stage(root, run_id, footprint, minted_at=1.0)


def test_overlapping_declared_writes_are_denied_naming_run_and_path(tmp_path):
    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"run-a": {"pkg/mod.py"}})
    seen: list = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "fix it", "source": "pm", "writes": ["pkg/mod.py"]}), emit=_emit_recorder(tmp_path, seen))
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "run-a" in reason and "pkg/mod.py" in reason and "Resubmit when run-a lands" in reason
    assert seen == []


def test_disjoint_writes_and_unstaged_runs_are_allowed(tmp_path):
    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"run-a": {"other/x.py"}, "run-b": None})
    seen: list = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "fix it", "source": "pm", "writes": ["pkg/mod.py"]}), emit=_emit_recorder(tmp_path, seen))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(seen) == 1


def test_no_declared_writes_allows_without_reading_the_index(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()

    def boom(*a, **k):
        raise AssertionError("live-run index read with no declared writes")

    monkeypatch.setattr(live_run_index, "read", boom)
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
    _live(tmp_path, {"run-a": {"pkg"}})
    assert mod._declared_writes(tmp_path, {"args": {}}, {"plan": "docs/plans/p.md"}) == ["pkg/mod.py"]
    why = mod._live_write_overlap_deny(tmp_path, {"args": {}}, {"plan": "docs/plans/p.md"})
    assert why and "run-a" in why


def test_plan_edge_outside_docs_plans_declares_nothing(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "elsewhere.md").write_text("x", encoding="utf-8")
    assert mod._declared_writes(tmp_path, {"args": {}}, {"plan": "elsewhere.md"}) == []


def test_absent_index_denies_nothing(tmp_path):
    (tmp_path / ".git").mkdir()
    assert mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {}) is None


def test_stalled_index_read_denies_nothing_and_returns_within_the_deadline(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"run-a": {"pkg/mod.py"}})
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 50.0)
    monkeypatch.setattr(halt_resume_record, "_read_file", lambda path: time.sleep(1.0) or b"")
    started = time.perf_counter()
    why = mod._bounded_call(("t",), lambda: time.sleep(1.0), deadline_ms=50.0)
    assert why is halt_resume_record.STALLED
    started = time.perf_counter()
    assert mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {}) is None
    assert time.perf_counter() - started < 0.5
    rows = (tmp_path / mod.FRICTION_LOG_REL).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1 and "overlap check skipped" in rows[0]


def test_absent_index_records_no_friction_row(tmp_path):
    (tmp_path / ".git").mkdir()
    assert mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {}) is None
    assert not (tmp_path / mod.FRICTION_LOG_REL).exists()


def test_a_refire_is_never_denied_by_the_halted_run_it_resumes(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"ask-halted": {"pkg/mod.py"}, "run-other": {"pkg/mod.py"}})
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.halted_run.halted_run_to_resume", lambda root, rel: "ask-halted"
    )
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    why = mod._live_write_overlap_deny(tmp_path, tool_input, {}, "state/sizings/s.yaml")
    assert why and "run-other" in why and "ask-halted" not in why
    _live(tmp_path, {"run-other": {"x.py"}})
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {}, "state/sizings/s.yaml") is None


def test_a_halted_run_still_in_the_index_denies_nothing(tmp_path):
    import json

    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"ask-20261029T080212-46892a": {"pkg/mod.py"}})
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {})
    rd = tmp_path / "state" / "warp" / "ask-20261029T080212-46892a"
    rd.mkdir(parents=True)
    (rd / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {}) is None
