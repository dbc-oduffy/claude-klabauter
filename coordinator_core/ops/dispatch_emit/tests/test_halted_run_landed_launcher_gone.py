"""dispatch.record_halted_run_landed: a running run whose launcher token is gone records landed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
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
    return tmp_path, sha


def _status(root: Path) -> str:
    return json.loads((root / c.status_rel(RID)).read_text(encoding="utf-8"))["status"]


def test_running_without_live_token_records_landed(repo):
    root, sha = repo
    assert not run_token.is_live(root, RID)
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert out["ok"] and out["written"] is True and out["sha"] == sha, out
    assert _status(root) == "landed"


def test_running_with_live_token_still_refuses(repo):
    root, sha = repo
    run_token.mint(root, RID, "scratch/x.py")
    assert run_token.is_live(root, RID)
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert not out["ok"] and "not halted" in out["error"]
    assert _status(root) == "running"


def test_gone_launcher_still_needs_a_reachable_sha(repo):
    root, _ = repo
    out = op._handler({"run_id": RID, "sha": "f" * 40}, root)
    assert not out["ok"]
    assert _status(root) == "running"


def _stage(root: Path, stage: str) -> None:
    from coordinator_core.ops.dispatch_emit import ask_contract

    d = ask_contract.record_dir(root, RID)
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")


def test_running_with_live_token_but_halted_stage_records_landed(repo):
    root, sha = repo
    run_token.mint(root, RID, "scratch/x.py")
    _stage(root, "halted")
    assert run_token.is_live(root, RID)
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert out["ok"] and out["written"] is True, out
    assert _status(root) == "landed"


def test_running_with_live_token_and_staged_stage_still_refuses(repo):
    root, sha = repo
    run_token.mint(root, RID, "scratch/x.py")
    _stage(root, "staged")
    out = op._handler({"run_id": RID, "sha": sha}, root)
    assert not out["ok"] and "not halted" in out["error"]
    assert _status(root) == "running"
