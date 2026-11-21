"""A `resumed` marker whose fire died is re-halted, so a named resume finds the run instead of minting a fresh one."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.session_hub_path import hub_for_common

pytestmark = pytest.mark.complete

SIZING = "state/sizings/2026-11/2026-11-21-foo.yaml"
RUN = "warp-2026-11-21-foo-20261121T020407-c94de0"


def _resumed_run(root: Path) -> Path:
    rd = root / RECORD_DIR_ROOT / RUN
    hr.write_halt_marker(rd, halted="staged_stall", sizing_rel=SIZING)
    assert hr.mark_resumed(rd) is True
    return rd


def _fire(root: Path, fire_id: str, state: str, pid: int = 0) -> None:
    registry = hub_for_common(root / ".git") / "workflow-fires"
    registry.mkdir(parents=True, exist_ok=True)
    (registry / f"{fire_id}.json").write_text(
        json.dumps({"fire_id": fire_id, "run_id": RUN, "state": state, "pid": pid}), encoding="utf-8"
    )


def _status(rd: Path) -> dict:
    return json.loads((rd / hr.HALTED_FILE).read_text(encoding="utf-8"))


def test_dead_fire_flips_resumed_to_halted(tmp_path: Path) -> None:
    rd = _resumed_run(tmp_path)
    _fire(tmp_path, "f1", "exited")
    assert hr.rehalt_if_resumer_dead(tmp_path, RUN, "runtime exited 1") is True
    marker = _status(rd)
    assert marker["status"] == "halted"
    assert marker["halted"] == hr.HALT_FIRE_ENDED
    assert marker["reason"] == "runtime exited 1"
    assert marker["sizing_rel"] == SIZING


def test_live_fire_keeps_resumed(tmp_path: Path) -> None:
    rd = _resumed_run(tmp_path)
    _fire(tmp_path, "f1", "pending")
    assert hr.rehalt_if_resumer_dead(tmp_path, RUN, "x") is False
    assert _status(rd)["status"] == "resumed"


def test_running_fire_with_live_pid_keeps_resumed(tmp_path: Path) -> None:
    rd = _resumed_run(tmp_path)
    _fire(tmp_path, "f1", "running", pid=os.getpid())
    assert hr.rehalt_if_resumer_dead(tmp_path, RUN, "x") is False
    assert _status(rd)["status"] == "resumed"


def test_only_a_resumed_marker_is_touched(tmp_path: Path) -> None:
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    hr.write_halt_marker(rd, halted="criterion_not_met", sizing_rel=SIZING)
    assert hr.rehalt_if_resumer_dead(tmp_path, RUN, "x") is False
    assert _status(rd)["halted"] == "criterion_not_met"


def test_named_resume_of_a_stranded_resumed_run_finds_it(tmp_path: Path) -> None:
    # The peer's case: the resumed fire timed out, halted.json stayed `resumed`, and resume_run_id missed it.
    _resumed_run(tmp_path)
    _fire(tmp_path, "f1", "exited")
    assert hr.halted_run_to_resume(tmp_path, SIZING, run_id=RUN) == RUN
    assert hr.halted_run_named(tmp_path, RUN, SIZING) == RUN


def test_named_resume_of_a_live_resumed_run_stays_unresolved(tmp_path: Path) -> None:
    rd = _resumed_run(tmp_path)
    _fire(tmp_path, "f1", "pending")
    assert hr.halted_run_named(tmp_path, RUN, SIZING) is None
    assert _status(rd)["status"] == "resumed"


def test_headless_failure_rehalts_its_run(tmp_path: Path) -> None:
    from coordinator_core.ops.workflow_fire import headless

    rd = _resumed_run(tmp_path)
    script = tmp_path / "s.workflow.mjs"
    script.write_text("export default 1", encoding="utf-8")
    artifacts = tmp_path / "art"
    artifacts.mkdir()
    (artifacts / "start").write_text("", encoding="utf-8")
    record_path = artifacts / "record.json"
    record_path.write_text(
        json.dumps({
            "artifact_dir": str(artifacts), "repo_root": str(tmp_path), "run_id": RUN, "fire_id": "f1",
            "script_path": str(script), "script_sha256": hashlib.sha256(b"other").hexdigest(),
            "backend": "codex", "timeout_s": 30, "state": "running", "pid": os.getpid(),
        }),
        encoding="utf-8",
    )
    result = headless.execute(record_path)
    assert result["outcome"] == "failed"
    marker = _status(rd)
    assert marker["status"] == "halted"
    assert marker["halted"] == hr.HALT_FIRE_ENDED
    assert "changed after fire" in marker["reason"]
