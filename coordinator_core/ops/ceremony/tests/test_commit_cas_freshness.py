
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import pytest

from coordinator_core.git.git_state import index_read_cache_scope
from coordinator_core.ops.ceremony import git_native

from .fixtures.real_git import make_diverged_path, real_git_repo
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    run_kwargs = {**no_console_creationflags(), **kwargs}
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **run_kwargs
    )


def _write_msg(tmp_path: Path, text: str = "cas freshness\n") -> Path:
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def _committed_files_at_head(repo: Path) -> list[str]:
    result = _git(["show", "--name-only", "--pretty=format:", "HEAD"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _install_peer_action(monkeypatch, attr_name: str, peer_action: Callable[[], None]) -> None:
    real_fn = getattr(git_native, attr_name)

    def _wrapped(*args, **kwargs):
        peer_action()
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(git_native, attr_name, _wrapped)


def test_index_cas_refuses_inside_open_cache_scope_when_peer_writes_index(
    tmp_path, monkeypatch
):
    repo = real_git_repo(tmp_path)
    make_diverged_path(repo, "file.txt", staged_content="OURS\n", worktree_content="WORKTREE\n")
    msg_file = _write_msg(tmp_path)

    def _peer_restages_same_path() -> None:
        (repo / "file.txt").write_text("PEER-NEWER\n", encoding="utf-8")
        _git(["add", "--", "file.txt"], repo)

    _install_peer_action(monkeypatch, "_resolve_commit_identity", _peer_restages_same_path)

    with index_read_cache_scope():
        result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert not result.ok
    assert "compare-and-swap failed" in result.stderr
    log_count = _git(["rev-list", "--count", "HEAD"], repo).stdout.strip()
    assert log_count == "1"
    assert "file.txt" not in _committed_files_at_head(repo)

    _git(["reset", "--", "file.txt"], repo)
    (repo / "file.txt").unlink()

