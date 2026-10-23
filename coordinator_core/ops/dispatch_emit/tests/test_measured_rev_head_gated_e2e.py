"""Prime falsifier: a HEAD-pinned byte-budget gate goes green at the measured rev for a change
that is correct only in the working tree, and stays red for one that is not."""

import os
import subprocess

from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_ENV

BUDGET = 100


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _gate_size(root, path):
    """Fixture gate: the blob size of `path` at COORDINATOR_MEASURED_REV or HEAD."""
    rev = os.environ.get(MEASURED_REV_ENV) or "HEAD"
    return int(_git(root, "cat-file", "-s", f"{rev}:{path}"))


def _green(root, path):
    return _gate_size(root, path) <= BUDGET


def _over_budget_repo(root):
    """A repo whose committed `skill.md` is over BUDGET; returns its path."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    skill = root / "skill.md"
    skill.write_text("x" * 500)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "over budget")
    return skill


def test_head_gated_row_goes_green_at_measured_rev(tmp_path, monkeypatch):
    skill = _over_budget_repo(tmp_path)

    monkeypatch.delenv(MEASURED_REV_ENV, raising=False)
    skill.write_text("x" * 50)
    assert not _green(tmp_path, "skill.md")

    head = _git(tmp_path, "rev-parse", "HEAD")
    ref = (tmp_path / ".git" / "refs" / "heads" / "main").read_bytes()
    index = (tmp_path / ".git" / "index").read_bytes()
    out = mr.measured_rev("run-e2e", ["skill.md"], repo_root=tmp_path)

    monkeypatch.setenv(MEASURED_REV_ENV, out["sha"])
    assert _green(tmp_path, "skill.md")
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert (tmp_path / ".git" / "refs" / "heads" / "main").read_bytes() == ref
    assert (tmp_path / ".git" / "index").read_bytes() == index

    run_dir = tmp_path / "state" / "warp" / "run-e2e"
    assert mr.measured_rev_drift(run_dir, tmp_path) == []
    skill.write_text("y" * 60)
    assert mr.measured_rev_drift(run_dir, tmp_path) == ["skill.md"]


def test_over_budget_worktree_change_stays_red(tmp_path, monkeypatch):
    skill = _over_budget_repo(tmp_path)
    skill.write_text("x" * 400)
    out = mr.measured_rev("run-red", ["skill.md"], repo_root=tmp_path)
    monkeypatch.setenv(MEASURED_REV_ENV, out["sha"])
    assert not _green(tmp_path, "skill.md")
