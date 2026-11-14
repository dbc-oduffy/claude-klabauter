"""dispatch.record_halted_run_landed: a refusal-halted run re-armed as a full pass (running status, live token, staged run-stage) closes via its halt marker."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "A"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "one")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    st = tmp_path / c.status_rel(RID)
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"parent_id": "P", "status": "running"}), encoding="utf-8")
    run_token.mint(tmp_path, RID, "scratch/x.py")
    d = ask_contract.record_dir(tmp_path, RID)
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    return tmp_path, sha


def _marker(root: Path) -> None:
    halted_run.write_halt_marker(
        ask_contract.record_dir(root, RID), halted="staging-refusal", sizing_rel=None,
    )


def test_rearmed_refusal_halt_with_marker_records_landed(repo):
    root, sha = repo
    _marker(root)
    assert run_token.is_live(root, RID)
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert out["ok"] and out["written"] is True and out["sha"] == sha, out
    status = json.loads((root / c.status_rel(RID)).read_text(encoding="utf-8"))["status"]
    assert status == "landed"


def test_live_staged_run_with_resumed_marker_still_refuses(repo):
    root, sha = repo
    _marker(root)
    assert halted_run.mark_resumed(ask_contract.record_dir(root, RID))
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert not out["ok"] and "not halted" in out["error"]


def test_live_staged_run_without_marker_still_refuses(repo):
    root, sha = repo
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert not out["ok"] and "not halted" in out["error"]


def test_resumed_marker_refuses_even_when_run_stage_records_halted(repo):
    root, sha = repo
    _marker(root)
    assert halted_run.mark_resumed(ask_contract.record_dir(root, RID))
    (ask_contract.record_dir(root, RID) / "run-stage.json").write_text(
        json.dumps({"stage": "halted"}), encoding="utf-8",
    )
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert not out["ok"] and "not halted" in out["error"]
