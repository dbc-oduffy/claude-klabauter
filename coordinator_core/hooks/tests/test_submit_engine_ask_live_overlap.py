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


def _staged_unmarked_run(root: Path, run_id: str, sizing_rel: str) -> None:
    import json

    rd = root / "state" / "warp" / run_id
    rd.mkdir(parents=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    (rd / "run.json").write_text(json.dumps({"sizing_rel": sizing_rel}), encoding="utf-8")


def test_a_refire_is_not_denied_by_its_own_staged_run_without_halted_json(tmp_path):
    (tmp_path / ".git").mkdir()
    sizing = "state/sizings/2026-11-02-own.yaml"
    own = "warp-2026-11-02-own-20261102T183622-f3eaab"
    _staged_unmarked_run(tmp_path, own, sizing)
    _live(tmp_path, {own: {"pkg/mod.py"}, "run-other": {"x.py"}})
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {}, sizing) is None
    assert own in (mod._live_write_overlap_deny(tmp_path, tool_input, {}) or "")
    _live(tmp_path, {"run-other": {"pkg/mod.py"}})
    why = mod._live_write_overlap_deny(tmp_path, tool_input, {}, sizing)
    assert why and "run-other" in why and own not in why


def _sizing(root: Path, name: str, deliverable: str) -> str:
    rel = f"state/sizings/{name}.yaml"
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(f"deliverable_id: {deliverable}\n", encoding="utf-8")
    return rel


def test_a_refire_citing_another_sizing_record_of_the_deliverable_is_not_denied_by_its_halted_run(tmp_path):
    (tmp_path / ".git").mkdir()
    old = _sizing(tmp_path, "2026-11-02-own", "dlv-x")
    new = _sizing(tmp_path, "2026-11-03-own-v2", "dlv-x")
    own = "warp-2026-11-02-own-20261102T183622-f3eaab"
    _staged_unmarked_run(tmp_path, own, old)
    _live(tmp_path, {own: {"pkg/mod.py"}})
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    new_doc = {"deliverable_id": "dlv-x"}
    assert mod._live_write_overlap_deny(tmp_path, tool_input, new_doc, new) is None
    # Same pair the other way round: the newer record bound the run, the refire cites the older one.
    older_doc = {"deliverable_id": "dlv-x"}
    _staged_unmarked_run(tmp_path, "warp-2026-11-03-own-v2-20261103T100000-aaaaaa", new)
    _live(tmp_path, {"warp-2026-11-03-own-v2-20261103T100000-aaaaaa": {"pkg/mod.py"}})
    assert mod._live_write_overlap_deny(tmp_path, tool_input, older_doc, old) is None


def test_a_staged_run_of_another_deliverable_or_a_running_one_still_denies(tmp_path):
    import json

    (tmp_path / ".git").mkdir()
    old = _sizing(tmp_path, "2026-11-02-own", "dlv-x")
    new = _sizing(tmp_path, "2026-11-03-own-v2", "dlv-x")
    other = _sizing(tmp_path, "2026-11-02-other", "dlv-y")
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    peer = "warp-2026-11-02-other-20261102T183622-f3eaab"
    _staged_unmarked_run(tmp_path, peer, other)
    _live(tmp_path, {peer: {"pkg/mod.py"}})
    why = mod._live_write_overlap_deny(tmp_path, tool_input, {"deliverable_id": "dlv-x"}, new)
    assert why and peer in why
    mine = "warp-2026-11-02-own-20261102T190000-bbbbbb"
    _staged_unmarked_run(tmp_path, mine, old)
    (tmp_path / "state" / "warp" / mine / "run-stage.json").write_text(json.dumps({"stage": "running"}), encoding="utf-8")
    _live(tmp_path, {mine: {"pkg/mod.py"}, peer: {"x.py"}})
    why = mod._live_write_overlap_deny(tmp_path, tool_input, {"deliverable_id": "dlv-x"}, new)
    assert why and mine in why


def test_a_run_whose_stage_reads_halted_denies_nothing(tmp_path):
    (tmp_path / ".git").mkdir()
    _staged_unmarked_run(tmp_path, "run-halted", "state/sizings/other.yaml")
    import json

    (tmp_path / "state" / "warp" / "run-halted" / "run-stage.json").write_text(
        json.dumps({"stage": "halted"}), encoding="utf-8"
    )
    _live(tmp_path, {"run-halted": {"pkg/mod.py"}})
    assert mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {}) is None


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


def test_a_fanout_child_halted_in_its_sibling_status_denies_nothing(tmp_path):
    import json

    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import status_rel

    (tmp_path / ".git").mkdir()
    run_id = "ask-20261030T094351-c6ffdb"
    _live(tmp_path, {run_id: {"pkg/mod.py"}})
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    status = tmp_path / status_rel(run_id)
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"status": "held"}), encoding="utf-8")
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {})
    status.write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {}) is None


def _marker(root: Path, run_id: str, status: str) -> None:
    """Write `halted.json` with `status` into the run's record dir."""
    import json

    rd = root / "state" / "warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "halted.json").write_text(json.dumps({"status": status}), encoding="utf-8")


def test_superseded_and_closed_markers_release_but_a_resumed_one_stays_live(tmp_path):
    (tmp_path / ".git").mkdir()
    run_id = "ask-20261101T173854-9a093a"
    _live(tmp_path, {run_id: {"pkg/mod.py"}})
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    _marker(tmp_path, run_id, "resumed")
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {})
    for status in ("superseded", "closed", "halted", "landed"):
        _marker(tmp_path, run_id, status)
        assert mod._live_write_overlap_deny(tmp_path, tool_input, {}) is None, status


def test_a_run_that_ended_on_harness_evidence_without_halted_json_denies_nothing(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    (tmp_path / ".git").mkdir()
    run_id = "ask-20261101T173854-9a093a"
    _live(tmp_path, {run_id: {"pkg/mod.py"}})
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    record = {"run_id": run_id, "minted_at": 1.0}
    monkeypatch.setattr(warp_live, "_token_records", lambda root: iter([record]))
    monkeypatch.setattr(warp_live, "run_ended_on_evidence", lambda root, rec: False)
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {})
    monkeypatch.setattr(warp_live, "run_ended_on_evidence", lambda root, rec: True)
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {}) is None


DEAD_RUN = "warp-2026-11-02-antigravity-reply-pipe-1b4dab-20261102T151348-56719e"


def _deferred_run(tmp_path, monkeypatch, notification: str) -> Path:
    """A minted run with no submission and no launch record under its own script (started inside a
    deferred-emit script), plus its owner session's transcript holding `notification`."""
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import run_token

    (tmp_path / ".git").mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: managed / name)
    config = tmp_path / "config"
    project = config / "projects" / "p"
    project.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    (project / "s1.jsonl").write_text(notification + "\n", encoding="utf-8")
    run_token.mint(tmp_path, DEAD_RUN, f"scratch/warp/{DEAD_RUN}.workflow.mjs", session_id="s1")
    _live(tmp_path, {DEAD_RUN: {"pkg/mod.py"}})
    return tmp_path


def _notification(status: str, run_id: str) -> str:
    import json

    result = json.dumps({"arm": "m_plus", "run_id": run_id, "halted_by": {"halted": "ledger-check"}})
    body = f"<task-notification>\n<task-id>w7vzsgr7g</task-id>\n<status>{status}</status>\n<result>{result}</result>\n</task-notification>"
    return json.dumps({"type": "queue-operation", "content": body})


def test_a_completed_workflow_whose_result_names_the_run_denies_nothing(tmp_path, monkeypatch):
    root = _deferred_run(tmp_path, monkeypatch, _notification("completed", DEAD_RUN))
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    assert mod._live_write_overlap_deny(root, tool_input, {}) is None
    assert mod._live_write_overlap_deny(root, tool_input, {}, "state/sizings/own.yaml") is None


def test_a_workflow_still_running_or_naming_another_run_stays_live(tmp_path, monkeypatch):
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    root = _deferred_run(tmp_path, monkeypatch, _notification("running", DEAD_RUN))
    assert mod._live_write_overlap_deny(root, tool_input, {})
    (tmp_path / "config" / "projects" / "p" / "s1.jsonl").write_text(
        _notification("completed", "warp-2026-11-02-other-20261102T151348-aaaaaa") + "\n", encoding="utf-8"
    )
    assert mod._live_write_overlap_deny(root, tool_input, {})


def test_a_run_with_no_token_record_and_no_marker_stays_live(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"run-a": {"pkg/mod.py"}})
    monkeypatch.setattr(warp_live, "_token_records", lambda root: iter([]))
    assert mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {})
