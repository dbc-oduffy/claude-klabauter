"""dispatch.commit_halted_rows lands from a receipt-borne marker when the run script is gone."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_rows_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def _repo(tmp_path: Path, receipt: dict | None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "a.py").write_text("a = 0\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    marker = render_marker(CommitRequest(chunks=(ChunkCommit(id="r1", title="first", paths=("a.py",)),)))
    script = root / "run.workflow.mjs"
    if receipt is not None:
        receipt = {**receipt, "landing_markers": marker.splitlines()} if receipt.get("with_marker") else receipt
        receipt.pop("with_marker", None)
        emission_receipt_path(script).write_text(json.dumps(receipt), encoding="utf-8")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    return root


def _call(root: Path) -> dict:
    return halted_rows_commit._handler(
        {"script_path": "run.workflow.mjs", "delivered": ["r1"]}, repo_root=root / ".git"
    )


def test_rows_commit_from_receipt_borne_marker(tmp_path):
    root = _repo(tmp_path, {"sha256": "x", "with_marker": True})

    out = _call(root)

    assert out["ok"] is True, out
    assert [x["row"] for x in out["landed"]] == ["r1"]
    assert "a.py" not in _git(["status", "--porcelain"], root)
    receipt = json.loads(emission_receipt_path(root / "run.workflow.mjs").read_text(encoding="utf-8"))
    assert "landed" not in receipt


@pytest.mark.parametrize("receipt", [None, {"sha256": "x"}, {"sha256": "x", "landing_markers": []}])
def test_script_missing_refusal_names_the_rebuild_route(tmp_path, receipt):
    root = _repo(tmp_path, receipt)

    out = _call(root)

    assert out["ok"] is False
    assert out["refused_code"] == "script-missing"
    assert "coordinator_core.ops.dispatch_emit.stranded_part_rebuild" in out["error"]
    assert "a.py" in _git(["status", "--porcelain"], root)
