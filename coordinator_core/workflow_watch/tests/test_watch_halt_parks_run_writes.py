"""Each watcher-landed halt marker is routed through `land_run_halt.land`; a refusal leaves the marker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import workflow_watch
from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, land_run_halt
from coordinator_core.workflow_watch.tests.test_watch_writes_usage_limit_halt import RATE_LIMIT_TURN

SIZING = "state/sizings/2026-10-29-x.yaml"
RUN_ID = "ask-20261029T100000-abcdef"
SCRIPT = "scripts/wf.js"


def _setup(tmp_path: Path, *, limit_leg: bool = False) -> tuple[Path, str]:
    run_dir = tmp_path / "runs" / "wf_abc"
    run_dir.mkdir(parents=True)
    journal = run_dir / "journal.jsonl"
    journal.write_text(json.dumps({"type": "started", "agentId": "a1", "label": "leg:a1", "key": "v2:a1"}) + "\n",
                       encoding="utf-8")
    if limit_leg:
        (run_dir / "agent-a1.jsonl").write_text(json.dumps(RATE_LIMIT_TURN) + "\n", encoding="utf-8")
    record_dir = tmp_path / "scratch" / "warp" / RUN_ID
    record_dir.mkdir(parents=True)
    (record_dir / "submission.json").write_text(json.dumps({
        "run_id": RUN_ID, "script_path": SCRIPT, "sizing_rel": SIZING, "submitted_at": 1.0,
    }), encoding="utf-8")
    return ask_contract.record_dir(tmp_path, RUN_ID), str(journal)


@pytest.fixture
def lands(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def fake_land(worktree_root, repo_root, params):
        calls.append((Path(worktree_root), params))
        return {"committed": True, "sha": "abc"}

    monkeypatch.setattr(land_run_halt, "land", fake_land)
    return calls


def _assert_routed(calls: list, tmp_path: Path, kind: str) -> None:
    assert len(calls) == 1
    root, params = calls[0]
    assert root == tmp_path
    assert params["run_id"] == RUN_ID and params["script_path"] == SCRIPT
    assert params["halted_by"]["halted"] == kind


def test_usage_limit_halt_routes_through_land(tmp_path: Path, lands: list) -> None:
    record_dir, journal = _setup(tmp_path, limit_leg=True)
    workflow_watch._write_usage_limit_halt(journal, SCRIPT, root=tmp_path)
    _assert_routed(lands, tmp_path, halted_run.read_strict(record_dir)["halted"])


def test_unrecorded_end_routes_through_land(tmp_path: Path, lands: list) -> None:
    record_dir, journal = _setup(tmp_path)
    workflow_watch._write_unrecorded_end_halt(journal, "failed", SCRIPT, root=tmp_path)
    _assert_routed(lands, tmp_path, "run-ended-unrecorded")
    assert halted_run.read_strict(record_dir)["halted"] == "run-ended-unrecorded"


def test_returned_halt_routes_through_land(tmp_path: Path, lands: list) -> None:
    record_dir, _journal = _setup(tmp_path)
    result = json.dumps({"halted_by": {"halted": "ledger-check", "reason": "r"}})
    workflow_watch._write_returned_halt(result, SCRIPT, root=tmp_path)
    assert halted_run.read_strict(record_dir) is not None
    assert len(lands) == 1


def test_commit_stage_halt_routes_through_land(tmp_path: Path, lands: list, monkeypatch: pytest.MonkeyPatch) -> None:
    record_dir, journal = _setup(tmp_path)
    from coordinator_core.ops.dispatch_emit import commit_stage_halt

    monkeypatch.setattr(commit_stage_halt, "commit_leg_outcome", lambda _t: ("commit", "uncommitted", "x"))
    workflow_watch._write_commit_stage_halt(journal, "failed", SCRIPT, root=tmp_path)
    _assert_routed(lands, tmp_path, commit_stage_halt.HALT_COMMIT_STAGE)
    assert halted_run.read_strict(record_dir) is not None


def test_no_marker_written_means_no_land(tmp_path: Path, lands: list) -> None:
    _record_dir, journal = _setup(tmp_path)
    workflow_watch._write_unrecorded_end_halt(journal, "completed", SCRIPT, root=tmp_path)
    assert lands == []


def test_rows_unlanded_commit_leg_writes_no_unrecorded_end_halt(
    tmp_path: Path, lands: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_dir, journal = _setup(tmp_path)
    from coordinator_core.ops.dispatch_emit import commit_stage_halt

    monkeypatch.setattr(commit_stage_halt, "commit_leg_outcome", lambda _t: ("commit", "rows-unlanded", "a.py"))
    workflow_watch._write_unrecorded_end_halt(journal, "failed", SCRIPT, root=tmp_path)
    assert lands == []
    assert halted_run.read_strict(record_dir) is None


@pytest.mark.parametrize("reply", [{"committed": False,"error": "nope", "refused": "x"}, "boom"])
def test_land_refusal_or_failure_leaves_marker_and_logs_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, reply
) -> None:
    record_dir, journal = _setup(tmp_path)

    def refuse(*_a, **_k):
        if isinstance(reply, dict):
            return reply
        raise RuntimeError(reply)

    monkeypatch.setattr(land_run_halt, "land", refuse)
    workflow_watch._write_unrecorded_end_halt(journal, "failed", SCRIPT, root=tmp_path)
    assert halted_run.read_strict(record_dir)["halted"] == "run-ended-unrecorded"
    assert halted_run.read_strict(record_dir)["sizing_rel"] == SIZING
    assert "advisory" in capsys.readouterr().err
