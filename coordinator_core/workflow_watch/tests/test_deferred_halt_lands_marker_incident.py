"""Incident pin: a refusal halt of a run started by a deferred start script lands `halted.json`, so the dead run is not live.

The watcher resolves the run it halts by matching the watched script path against a `submission.json`. A deferred start
script is named by no submission, so its halt landed no marker: `run-stage.json` stayed `staged` with a fresh token,
`live_runs.run_is_live` answered True, and a refire was refused as 'a second launcher is not started'."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_live, workflow_watch
from coordinator_core.ops.dispatch_emit import ask_contract, deferred_emit_script, halted_run, land_run_halt
from coordinator_core.ops.dispatch_emit.nested_run_contract import RUN_SCRIPT_SUFFIX
from coordinator_core.session import liveness
from coordinator_core.warm import live_runs

pytestmark = pytest.mark.complete

RUN_ID = "ask-20261122T100000-abcdef"
SIZING = "state/sizings/2026-11/2026-11-22-x.yaml"
SESSION = "sess1"
NESTING_REFUSAL = (
    "stage reply sidecar does not match its handle: reader scratch/stage-readers/h.js did not run: "
    "workflow() cannot be called from within a child workflow — nesting is limited to one level"
)


@pytest.fixture(autouse=True)
def _live_owner_and_stub_land(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid == SESSION)
    monkeypatch.setattr(land_run_halt, "land", lambda worktree_root, repo_root, params: {"committed": True, "sha": "abc"})


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _staged_run(root: Path, run_id: str = RUN_ID, stage: str = "staged") -> Path:
    """A run the deferred start began: stage `staged` with a fresh stamp, a held token, a live owner, and a submission
    naming the run's own script, never the deferred start script."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    record = ask_contract.record_dir(root, run_id)
    record.mkdir(parents=True, exist_ok=True)
    (record / ask_contract.RUN_STAGE_FILE).write_text(
        json.dumps({"stage": stage, "last_activity_at": _now_stamp(), "session_id": SESSION}), encoding="utf-8",
    )
    submission = root / ask_contract.RUN_DIR_ROOT / run_id / warp_live.SUBMISSION_FILE
    submission.parent.mkdir(parents=True, exist_ok=True)
    submission.write_text(json.dumps({
        "run_id": run_id, "script_path": f"scratch/warp/{run_id}{RUN_SCRIPT_SUFFIX}", "sizing_rel": SIZING,
        "submitted_at": time.time() - 60,
    }), encoding="utf-8")
    tokens = root / ".git" / "coordinator-warp-run-tokens"
    tokens.mkdir(parents=True, exist_ok=True)
    (tokens / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "minted_at": time.time()}), encoding="utf-8")
    return record


def _defer_script(root: Path) -> str:
    """The rendered deferred-emit script of this run: the path the watcher is given."""
    script = deferred_emit_script.write_deferred_script(root, {"ask": "build the thing", "preminted_run_id": RUN_ID})
    return script.as_posix()


def _minted_start_script(root: Path, *run_ids: str) -> str:
    """The rendered minted-start script naming `run_ids` (`RUN_IDS`, no `PARAMS_FILE`): the path the watcher is given."""
    from coordinator_core.hooks import submit_engine_ask

    return submit_engine_ask._write_minted_start_script(root, list(run_ids)).as_posix()


def _journal(root: Path) -> str:
    journal = root / "runs" / "wf_defer" / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"type": "started", "agentId": "a1", "label": "deferred-emit", "key": "v2:a1"}) + "\n", encoding="utf-8")
    return str(journal)


def test_the_staged_run_reads_live_before_any_halt_lands(tmp_path: Path) -> None:
    _staged_run(tmp_path)
    assert live_runs.run_is_live(tmp_path, RUN_ID)


def test_a_deferred_start_that_ended_unrecorded_lands_the_halt_marker_and_the_run_is_not_live(tmp_path: Path) -> None:
    record = _staged_run(tmp_path)
    defer = _defer_script(tmp_path)
    workflow_watch._write_unrecorded_end_halt(_journal(tmp_path), "failed", defer, root=tmp_path)
    marker = halted_run.read_strict(record)
    assert marker is not None and marker["halted"] == "run-ended-unrecorded"
    assert not live_runs.run_is_live(tmp_path, RUN_ID)


def test_a_deferred_start_that_returned_the_nesting_refusal_lands_the_halt_marker_and_the_run_is_not_live(tmp_path: Path) -> None:
    record = _staged_run(tmp_path)
    defer = _defer_script(tmp_path)
    result = json.dumps({"halted_by": {"halted": "stage-reply-handle", "reason": NESTING_REFUSAL}})
    workflow_watch._write_returned_halt(result, defer, root=tmp_path)
    marker = halted_run.read_strict(record)
    assert marker is not None and marker["halted"] == "stage-reply-handle"
    assert not live_runs.run_is_live(tmp_path, RUN_ID)


def _refire(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """The hook's resume route for the run's sizing, as a refire names it; the sizing file is the one the submission records."""
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr

    sizing = root / SIZING
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text(yaml.safe_dump({
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "interaction_mode": "pm", "deliverable_id": "dlv-defer-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-22", "mode": "pm"}},
    }, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(hrr, "engine_sha", lambda: None)
    monkeypatch.setattr(hook, "_spawn_recompose", lambda r, run_id: True)
    payload = {
        "tool_name": "Workflow", "cwd": str(root), "session_id": SESSION, "tool_use_id": "toolu_x",
        "tool_input": {"name": hook.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING, "resume_run_id": RUN_ID}},
    }
    params = {"session_id": SESSION, "ask": True, "sizing_path": SIZING}
    return hook._resume_route(root, SIZING, payload, params, SESSION, None)


def test_a_refire_after_a_deferred_start_halt_is_not_refused_as_a_second_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _staged_run(tmp_path)
    defer = _defer_script(tmp_path)
    result = json.dumps({"halted_by": {"halted": "stage-reply-handle", "reason": NESTING_REFUSAL}})
    workflow_watch._write_returned_halt(result, defer, root=tmp_path)
    envelope, _emitted = _refire(tmp_path, monkeypatch)
    assert envelope is not None, "the refire fell through to a plain emit instead of resuming the halted run"
    assert "a second launcher is not started" not in json.dumps(envelope)
    assert "scriptPath" in json.dumps(envelope)


def test_a_halt_of_the_run_script_itself_still_lands_through_its_submission(tmp_path: Path) -> None:
    record = _staged_run(tmp_path)
    run_script = f"scratch/warp/{RUN_ID}{RUN_SCRIPT_SUFFIX}"
    workflow_watch._write_unrecorded_end_halt(_journal(tmp_path), "failed", run_script, root=tmp_path)
    assert halted_run.read_strict(record)["halted"] == "run-ended-unrecorded"
    assert not live_runs.run_is_live(tmp_path, RUN_ID)


def test_a_script_no_run_is_started_by_lands_no_marker(tmp_path: Path) -> None:
    record = _staged_run(tmp_path)
    workflow_watch._write_unrecorded_end_halt(_journal(tmp_path), "failed", "scratch/warp/unrelated.workflow.mjs", root=tmp_path)
    assert halted_run.read_strict(record) is None
    assert live_runs.run_is_live(tmp_path, RUN_ID)


def test_a_minted_start_that_ended_unrecorded_lands_the_halt_marker_and_the_run_is_not_live(tmp_path: Path) -> None:
    record = _staged_run(tmp_path)
    start = _minted_start_script(tmp_path, RUN_ID)
    workflow_watch._write_unrecorded_end_halt(_journal(tmp_path), "failed", start, root=tmp_path)
    marker = halted_run.read_strict(record)
    assert marker is not None and marker["halted"] == "run-ended-unrecorded"
    assert not live_runs.run_is_live(tmp_path, RUN_ID)


def test_a_refire_after_a_minted_start_halt_is_not_refused_as_a_second_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _staged_run(tmp_path)
    workflow_watch._write_unrecorded_end_halt(_journal(tmp_path), "failed", _minted_start_script(tmp_path, RUN_ID), root=tmp_path)
    envelope, _emitted = _refire(tmp_path, monkeypatch)
    assert envelope is not None, "the refire fell through to a plain emit instead of resuming the halted run"
    assert "a second launcher is not started" not in json.dumps(envelope)


SECOND_ID = "ask-20261122T100001-abcdee"


@pytest.mark.parametrize("ids", [(RUN_ID, SECOND_ID), (SECOND_ID, RUN_ID)], ids=["staged-first", "staged-last"])
def test_a_minted_start_over_two_runs_halts_only_the_run_that_never_reached_a_stage(tmp_path: Path, ids: tuple) -> None:
    """RUN_ID never left `staged`; SECOND_ID reached `landed`, which a halt never overwrites, whatever order the script names them."""
    staged = _staged_run(tmp_path, RUN_ID)
    landed = _staged_run(tmp_path, SECOND_ID, stage="landed")
    workflow_watch._write_unrecorded_end_halt(_journal(tmp_path), "failed", _minted_start_script(tmp_path, *ids), root=tmp_path)
    marker = halted_run.read_strict(staged)
    assert marker is not None and marker["halted"] == "run-ended-unrecorded"
    assert halted_run.read_strict(landed) is None
    assert not live_runs.run_is_live(tmp_path, RUN_ID)
