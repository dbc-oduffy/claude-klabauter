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
    """Write the index: a None footprint is a minted run that has not staged. Each run gets its record dir
    (a row whose dir is gone is a ghost, released by the guard)."""
    for run_id, footprint in footprints.items():
        (root / "state" / "warp" / run_id).mkdir(parents=True, exist_ok=True)
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
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    (rd / "run.json").write_text(json.dumps({"sizing_rel": sizing_rel}), encoding="utf-8")


def test_a_refire_is_not_denied_by_its_own_staged_run_without_halted_json(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    sizing = "state/sizings/2026-11-02-own.yaml"
    own = "warp-2026-11-02-own-20261102T183622-f3eaab"
    _staged_unmarked_run(tmp_path, own, sizing)
    _workflow_record(monkeypatch, own)
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


def _workflow_record(monkeypatch, run_id: str) -> None:
    """A live token record for `run_id` whose harness evidence shows no end."""
    from coordinator_core import warp_live

    monkeypatch.setattr(warp_live, "_token_records", lambda root: iter([{"run_id": run_id, "minted_at": 1.0}]))
    monkeypatch.setattr(warp_live, "run_ended_on_evidence", lambda root, rec: False)


def test_a_staged_run_of_another_deliverable_with_no_workflow_holds_no_claim(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    (tmp_path / ".git").mkdir()
    other = _sizing(tmp_path, "2026-11-02-other", "dlv-y")
    new = _sizing(tmp_path, "2026-11-05-new", "dlv-x")
    stale = "ask-20261105T155602-6cccf1"
    _staged_unmarked_run(tmp_path, stale, other)
    _live(tmp_path, {stale: {"pkg/mod.py"}})
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    monkeypatch.setattr(warp_live, "_token_records", lambda root: iter([]))
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {"deliverable_id": "dlv-x"}, new) is None
    _workflow_record(monkeypatch, stale)
    why = mod._live_write_overlap_deny(tmp_path, tool_input, {"deliverable_id": "dlv-x"}, new)
    assert why and f"Run {stale} is live and writes" in why


def test_a_staged_run_of_another_deliverable_or_a_running_one_still_denies(tmp_path, monkeypatch):
    import json

    (tmp_path / ".git").mkdir()
    old = _sizing(tmp_path, "2026-11-02-own", "dlv-x")
    new = _sizing(tmp_path, "2026-11-03-own-v2", "dlv-x")
    other = _sizing(tmp_path, "2026-11-02-other", "dlv-y")
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    peer = "warp-2026-11-02-other-20261102T183622-f3eaab"
    _staged_unmarked_run(tmp_path, peer, other)
    _live(tmp_path, {peer: {"pkg/mod.py"}})
    _workflow_record(monkeypatch, peer)
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
    rd.mkdir(parents=True, exist_ok=True)
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


def test_every_ended_marker_including_resumed_releases(tmp_path):
    (tmp_path / ".git").mkdir()
    run_id = "ask-20261101T173854-9a093a"
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    for status in ("resumed", "superseded", "closed", "halted", "landed"):
        _live(tmp_path, {run_id: {"pkg/mod.py"}})
        _marker(tmp_path, run_id, status)
        assert mod._live_write_overlap_deny(tmp_path, tool_input, {}) is None, status


def test_a_resumed_or_ghost_row_is_dropped_from_the_index_but_a_halted_one_stays(tmp_path):
    (tmp_path / ".git").mkdir()
    tool_input = {"args": {"writes": ["pkg/mod.py"]}}
    resumed, ghost, halted = "ask-20261101T173854-aaaaaa", "ask-20261101T173854-bbbbbb", "ask-20261101T173854-cccccc"
    _live(tmp_path, {resumed: {"pkg/mod.py"}, halted: {"pkg/mod.py"}})
    live_run_index.stage(tmp_path, ghost, {"pkg/mod.py"}, minted_at=1.0)
    _marker(tmp_path, resumed, "resumed")
    _marker(tmp_path, halted, "halted")
    assert mod._live_write_overlap_deny(tmp_path, tool_input, {}) is None
    rows = live_run_index.read(tmp_path, 1000.0)
    assert resumed not in rows and ghost not in rows and halted in rows


def test_a_dir_less_ghost_row_denies_nothing(tmp_path):
    (tmp_path / ".git").mkdir()
    live_run_index.stage(tmp_path, "ask-20261103T130000-dddddd", {"pkg/mod.py"}, minted_at=1.0)
    assert mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {}) is None


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


def test_stalled_liveness_read_denies_nothing(tmp_path, monkeypatch):
    """An index hit whose ended-read stalls is unknown, not live: the ask is allowed."""
    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"run-a": {"pkg/mod.py"}})
    real = mod._bounded_call

    def stall_ended(key, fn, deadline_ms=None):
        return halt_resume_record.STALLED if key[0] == "ended" else real(key, fn, deadline_ms)

    monkeypatch.setattr(mod, "_bounded_call", stall_ended)
    seen: list = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "fix it", "source": "pm", "writes": ["pkg/mod.py"]}), emit=_emit_recorder(tmp_path, seen))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(seen) == 1


OLD_RUN = "ask-20261104T110538-8f5875"
NEW_RUN = "warp-2026-11-04-neutral-anchor-6ec8fe-20261104T150000-6ec8fe"
SIZING = "state/sizings/2026-11/2026-11-04-neutral-anchor-6ec8fe.yaml"


def _stage_run(root: Path, run_id: str, stage: str, session: str = "dead-session") -> None:
    import json

    rd = root / "state" / "warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run-stage.json").write_text(json.dumps({"stage": stage, "session_id": session}), encoding="utf-8")
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")


def test_a_superseded_run_with_a_halted_successor_does_not_block_a_submission(tmp_path):
    (tmp_path / ".git").mkdir()
    _stage_run(tmp_path, OLD_RUN, "staged")
    _stage_run(tmp_path, NEW_RUN, "halted")
    _live(tmp_path, {OLD_RUN: {"pkg/mod.py"}})
    seen: list = []
    env = mod.evaluate(_payload(tmp_path, {"ask": "fix it", "source": "pm", "writes": ["pkg/mod.py"]}), emit=_emit_recorder(tmp_path, seen))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert len(seen) == 1


def test_a_superseded_run_with_a_live_successor_still_blocks(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _workflow_record(monkeypatch, OLD_RUN)
    _stage_run(tmp_path, OLD_RUN, "staged")
    _stage_run(tmp_path, NEW_RUN, "running")
    _live(tmp_path, {OLD_RUN: {"pkg/mod.py"}})
    why = mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {})
    assert why and OLD_RUN in why


def test_a_stale_orphaned_blocker_names_the_adopt_op(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    _workflow_record(monkeypatch, OLD_RUN)
    _stage_run(tmp_path, OLD_RUN, "staged")
    _live(tmp_path, {OLD_RUN: {"pkg/mod.py"}})
    why = mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {})
    assert why and OLD_RUN in why and "dispatch.adopt_orphaned_run" in why


def test_a_blocker_with_no_recorded_owner_names_no_remedy(tmp_path):
    (tmp_path / ".git").mkdir()
    _live(tmp_path, {"run-a": {"pkg/mod.py"}})
    why = mod._live_write_overlap_deny(tmp_path, {"args": {"writes": ["pkg/mod.py"]}}, {})
    assert why and "adopt_orphaned_run" not in why


def test_the_supersession_and_orphan_preconditions_hold_for_the_staged_runs(tmp_path):
    from coordinator_core.ops.dispatch_emit.orphan_adopt import orphan_verdict

    _stage_run(tmp_path, OLD_RUN, "staged")
    _stage_run(tmp_path, NEW_RUN, "halted")
    verdict = orphan_verdict(tmp_path, OLD_RUN, "")
    assert verdict["reason"] == "superseded" and verdict["superseded_by"] == NEW_RUN
    lone = tmp_path / "lone"
    _stage_run(lone, OLD_RUN, "staged")
    verdict = orphan_verdict(lone, OLD_RUN, "")
    assert verdict["orphaned"] is True and verdict["recorded_session"] == "dead-session"


def test_emit_runs_under_the_callers_session_identity(tmp_path):
    from coordinator_core.session.core import resolve_session_id

    caller = "e9b562a0-1111-4222-8333-444455556666"
    seen: list = []

    def emit(params: dict, root: Path) -> dict:
        seen.append(resolve_session_id())
        return {}

    mod._emit_within_budget(emit, {"session_id": caller}, tmp_path, 5.0)
    assert seen == [caller]


def test_emit_without_a_uuid_session_id_binds_no_override(tmp_path):
    from coordinator_core.session.core import resolve_session_id

    outside = resolve_session_id()
    seen: list = []

    def emit(params: dict, root: Path) -> dict:
        seen.append(resolve_session_id())
        return {}

    mod._emit_within_budget(emit, {"session_id": "s1"}, tmp_path, 5.0)
    mod._emit_within_budget(emit, {}, tmp_path, 5.0)
    assert seen == [outside, outside]
