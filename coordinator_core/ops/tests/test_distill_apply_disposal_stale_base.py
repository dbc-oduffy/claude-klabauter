"""Race pin: a peer commit landing between the private-index build and the
commit makes `_delete_tracked_and_append_log` refuse, never revert the peer."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import distill_apply_disposal as mod
from coordinator_core.win_portability import no_console_creationflags

# Real git is load-bearing: the race is between a real index build and a real
# peer commit.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "gone.md").write_text("body\n", encoding="utf-8")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _run_landing(repo: Path, monkeypatch, peer_commits: bool):
    real_run_git = mod._run_git
    state = {"injected": False}

    async def racing_run_git(*args, cwd, env):
        result = await real_run_git(*args, cwd=cwd, env=env)
        if peer_commits and args[0] == "rm" and not state["injected"]:
            state["injected"] = True
            (repo / "peer.md").write_text("peer work\n", encoding="utf-8")
            _git(["add", "peer.md"], repo)
            _git(["commit", "-q", "-m", "peer lands"], repo)
        return result

    monkeypatch.setattr(mod, "_run_git", racing_run_git)
    return asyncio.run(
        mod._delete_tracked_and_append_log(
            worktree_root=repo,
            tracked_paths=[repo / "gone.md"],
            log_path=repo / "state" / "distillation-log.md",
            log_rows=[],
            subject="test: dispose gone.md",
        )
    )


def test_moved_head_refuses_and_peer_commit_survives(repo, monkeypatch):
    reaped, _dw, _dwf, failed = _run_landing(repo, monkeypatch, peer_commits=True)
    peer_head = _git(["rev-parse", "HEAD"], repo)

    assert reaped == []
    assert [f["path"] for f in failed] == ["gone.md"]
    assert "stale-index-base" in failed[0]["reason"]
    assert _git(["log", "-1", "--format=%s"], repo) == "peer lands"
    assert _git(["ls-tree", "--name-only", peer_head], repo).split() == [
        "README.md", "gone.md", "peer.md",
    ]
    assert (repo / "peer.md").read_text(encoding="utf-8") == "peer work\n"


def test_unmoved_head_still_lands(repo, monkeypatch):
    reaped, _dw, _dwf, failed = _run_landing(repo, monkeypatch, peer_commits=False)

    assert failed == []
    assert reaped == ["gone.md"]
    assert "gone.md" not in _git(["ls-tree", "--name-only", "HEAD"], repo).split()
