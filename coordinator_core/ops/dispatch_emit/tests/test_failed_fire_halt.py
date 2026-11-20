"""A fire that exits failed halts the runs it drove; a resumed record is never left `resumed`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.workflow_fire import headless

RUN = "warp-2026-11-20-passive-freshness-status-969ba3-20261120T005822-b26336"
ERROR = "AgentFailed: codex returned no schema-valid result: 'arm' is a required property"


@pytest.fixture(autouse=True)
def _no_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(halted_run, "_schedule_resume_build", lambda *_a, **_k: None)
    monkeypatch.setattr(halted_run, "find_split_trees", lambda *_a, **_k: [])


def _resumed(root: Path) -> Path:
    rd = record_dir(root, RUN)
    halted_run.write_halt_marker(rd, halted="ledger-check", sizing_rel=None, reason="original halt")
    assert halted_run.mark_resumed(rd)
    return rd


def test_failed_resume_replaces_resumed_marker_with_a_halt_naming_the_failure(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import failed_fire_halt

    rd = _resumed(tmp_path)
    result = {"parent_id": "resume-" + RUN, "runs": [{"run_id": RUN, "status": "failed", "error": ERROR}]}
    ids = failed_fire_halt.failed_run_ids(result, str(tmp_path / "resume-x.launcher.workflow.mjs"))
    assert failed_fire_halt.halt_failed_fire(tmp_path, ids, ERROR) == [RUN]
    marker = json.loads((rd / "halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "halted" and marker["halted"] == "run_failed"
    assert "'arm' is a required property" in marker["reason"]


def test_failed_run_without_a_marker_gets_a_halt_from_its_script_name(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import failed_fire_halt

    rd = record_dir(tmp_path, RUN)
    rd.mkdir(parents=True)
    script = tmp_path / "scratch" / "warp" / f"{RUN}.workflow.mjs"
    ids = failed_fire_halt.failed_run_ids(None, str(script))
    failed_fire_halt.halt_failed_fire(tmp_path, ids, "Expecting ',' delimiter: line 1 column 485")
    assert halted_run.read_strict(rd)["halted"] == "run_failed"


def test_existing_halt_is_left_alone_and_unknown_run_is_not_invented(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit import failed_fire_halt

    rd = record_dir(tmp_path, RUN)
    halted_run.write_halt_marker(rd, halted="ledger-check", sizing_rel=None)
    assert failed_fire_halt.halt_failed_fire(tmp_path, [RUN, "warp-never-minted"], "boom") == []
    assert not record_dir(tmp_path, "warp-never-minted").exists()


def test_headless_execute_halts_failed_run_instead_of_exiting_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(headless, "_halt_failed_fire", lambda record, result, error: seen.append(error))
    source = tmp_path / f"{RUN}.workflow.mjs"
    source.write_text("export const meta={};\nthrow new Error('boom');", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "start").touch()
    import hashlib
    path = tmp_path / "record.json"
    path.write_text(json.dumps({"fire_id": "t", "script_path": str(source),
        "script_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "repo_root": str(tmp_path),
        "artifact_dir": str(artifacts), "backend": "codex", "journal_path": str(artifacts / "journal.jsonl"),
        "timeout_s": 10}), encoding="utf-8")
    assert headless.execute(path)["exit_code"] == 1
    assert seen and "boom" in seen[0]


def test_a_landed_record_is_not_overwritten_by_a_failed_fire(tmp_path):
    import json
    from coordinator_core.ops.dispatch_emit import failed_fire_halt, halted_run
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    run = "warp-2026-11-20-x-111111-20261120T000000-aaaaaa"
    rd = record_dir(tmp_path, run)
    rd.mkdir(parents=True)
    (rd / "halted.json").write_text(json.dumps({"status": halted_run.STATUS_LANDED}), encoding="utf-8")
    assert failed_fire_halt.halt_failed_fire(tmp_path, [run], "boom") == []
    assert json.loads((rd / "halted.json").read_text(encoding="utf-8"))["status"] == halted_run.STATUS_LANDED
