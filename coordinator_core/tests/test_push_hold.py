"""push_hold: a held branch/repo is never pushed by push_outstanding; clearing resumes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.push_outstanding as po
from coordinator_core import push_hold
from coordinator_core.ops.ceremony.push import PushOutcome
from coordinator_core.warm import push_cadence

pytestmark = pytest.mark.spawns_process


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # popup-intentional-last-resort
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(["init", "-q", "-b", "work/x"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    _git(["commit", "-q", "--allow-empty", "-m", "c"], tmp_path)
    pushed: list[Path] = []

    def fake(root, **kw):
        pushed.append(root)
        return PushOutcome(exit_code=0, acted=["push"])

    monkeypatch.setattr(po, "push_with_retry", fake)
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    return tmp_path, pushed


def test_branch_hold_blocks_then_clear_resumes(repo):
    root, pushed = repo
    assert push_hold.set_hold(root, "work/x", "pr freeze")
    out = po.push_outstanding(root)
    assert "push:hold" in out.skipped and "push:hold-note:pr freeze" in out.skipped
    push_cadence._sweep_one(root)
    assert pushed == []
    assert push_hold.list_holds(root)["branches"] == {"work/x": "pr freeze"}
    assert push_hold.clear_hold(root, "work/x")
    po.push_outstanding(root)
    assert pushed == [root]


def test_hold_on_other_branch_does_not_block(repo):
    root, pushed = repo
    push_hold.set_hold(root, "work/other", "n")
    po.push_outstanding(root)
    assert pushed == [root]


def test_repo_hold_skips_entirely_and_clear_resumes(repo):
    root, pushed = repo
    assert push_hold.set_hold(root, None, None)
    out = po.push_outstanding(root)
    assert out.skipped[0] == "push:hold" and "push:hold-note:held" in out.skipped
    push_cadence.sweep_repos([root])
    assert pushed == []
    push_hold.clear_hold(root)
    assert push_hold.clear_hold(root)  # idempotent
    po.push_outstanding(root)
    assert pushed == [root]


def test_allow_sha_round_trips_and_clear_drops_it(repo):
    root, _ = repo
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()  # popup-intentional-last-resort
    assert push_hold.set_hold(root, "work/x", "r", allow_sha=sha[:10])
    assert push_hold.read_hold_allow(root, "work/x") == ("r", sha)
    assert push_hold.list_holds(root)["branch_allow"] == {"work/x": sha}
    assert push_hold.set_hold(root, "work/x", "r2")
    assert push_hold.read_hold_allow(root, "work/x") == ("r2", None)
    push_hold.set_hold(root, "work/x", "r", allow_sha=sha)
    assert push_hold.clear_hold(root, "work/x")
    assert push_hold.read_hold_allow(root, "work/x") == (None, None)
    assert not push_hold.set_hold(root, "work/x", "r", allow_sha="deadbeef" * 5)


def test_cli_set_allow_sha_then_list(repo):
    import sys

    root, _ = repo
    cli = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "push-hold.py"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # popup-intentional-last-resort

    def run(*a):
        return subprocess.run([sys.executable, str(cli), *a, "--repo", str(root)], capture_output=True,
                              text=True, creationflags=flags)

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
                         creationflags=flags).stdout.strip()
    assert run("set", "--branch", "work/x", "--reason", "r", "--allow-sha", sha[:8]).returncode == 0
    out = run("list")
    assert out.returncode == 0 and f"branch work/x: r (allow {sha})" in out.stdout
