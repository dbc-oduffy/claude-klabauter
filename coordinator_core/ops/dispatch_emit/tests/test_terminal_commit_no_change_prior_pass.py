"""_no_change_refusal credits paths an earlier pass of the same run recorded as written: the re-fire
whose own pass wrote nothing lands that work; a path no pass wrote still refuses."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest

RUN_ID = "ask-20261024T045125-99976d"


def _manifest(root, names):
    for n in names:
        (root / n).write_text("one\n")
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b", writes=tuple(names), wave=0,
        register_rows=("R1",),
    )
    return StageManifest(
        run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m",
        write_baseline=wb.capture(root, names),
    )


def _request(names):
    return CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(names)),))


def _run_dir(root, record_name, payload):
    run_dir = root / "state" / "warp" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / record_name).write_text(json.dumps(payload))
    return run_dir


def test_slice_files_of_an_earlier_pass_lift_the_refusal(tmp_path):
    m = _manifest(tmp_path, ["a.py"])
    run_dir = _run_dir(tmp_path, "run-outcome.json", {"inline_review": {"prep": {"slice_files": ["a.py"]}}})
    assert tc._no_change_refusal(tmp_path, m, _request(["a.py"]), None, run_dir) is None


def test_withheld_files_of_a_halted_earlier_pass_lift_the_refusal(tmp_path):
    names = [f"f{i}.py" for i in range(12)]
    m = _manifest(tmp_path, names)
    run_dir = _run_dir(tmp_path, "halted.json", {"run_artifacts": {"withheld": {n: "x" for n in names}}})
    assert tc._no_change_refusal(tmp_path, m, _request(names), None, run_dir) is None


def test_path_no_pass_wrote_still_refuses(tmp_path):
    m = _manifest(tmp_path, ["a.py", "b.py"])
    run_dir = _run_dir(tmp_path, "run-outcome.json", {"inline_review": {"prep": {"slice_files": ["other.py"]}}})
    out = tc._no_change_refusal(tmp_path, m, _request(["a.py", "b.py"]), None, run_dir)
    assert out["refused"] == wb.NO_CHANGE_REFUSAL


def test_without_a_run_dir_the_refusal_stands(tmp_path):
    m = _manifest(tmp_path, ["a.py"])
    out = tc._no_change_refusal(tmp_path, m, _request(["a.py"]), None)
    assert out["refused"] == wb.NO_CHANGE_REFUSAL
