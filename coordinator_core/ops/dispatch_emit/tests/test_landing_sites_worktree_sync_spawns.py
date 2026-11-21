"""Landing sites surface `CommitOutcome.sync_refused` as `worktree_sync_refused` plus
`sync_incomplete: true` and never deny the landing. Real git fixture for the checkpoint stamp
site; a stand-in `commit_paths` outcome for the sites whose set-up is the whole run machinery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as git_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BASE = b"".join(b"line%02d\n" % i for i in range(12))


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, input=data, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


@pytest.mark.spawns_process
def test_checkpoint_stamp_land_reports_a_colliding_path_and_keeps_the_landing(
    tmp_path: Path, monkeypatch
) -> None:
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import _land_stamp

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.py").write_bytes(_BASE)
    _git(repo, "add", "--", "a.py")
    _git(repo, "commit", "-q", "-m", "seed")
    real = git_commit.commit_paths
    landed = _BASE.replace(b"line05\n", b"line05 landed\n")
    blob = _git(repo, "hash-object", "-w", "--stdin", data=landed)
    collide = _BASE.replace(b"line05\n", b"line05 peer\n")
    (repo / "a.py").write_bytes(collide)

    monkeypatch.setattr(
        git_commit, "commit_paths",
        lambda root, paths, msg, **kw: real(root, paths, msg, supplied_blobs={"a.py": blob}),
    )
    report: dict = {}
    sha = _land_stamp(
        repo, ["a.py"], "m\n\nSession-Id: 44444444-4444-4444-8444-444444444444", None, report=report
    )

    assert sha
    assert report == {"worktree_sync_refused": ["a.py"], "sync_incomplete": True}
    assert (repo / "a.py").read_bytes() == collide
    assert _git(repo, "rev-parse", "HEAD:a.py") == blob
