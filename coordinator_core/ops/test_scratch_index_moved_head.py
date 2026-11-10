"""Engine scratch-index landings refuse when a peer commit lands after the base is captured.

Each pin injects the peer commit at the route's own base-capture seam (right after
it returns), then asserts a non-ok result, that HEAD is the peer's commit, and
that the peer's file survives in HEAD's tree.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import propagate_body as pb
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.dispatch_emit import merge_commit
from coordinator_core.ops.dispatch_emit.tests.test_merge_commit import _build as _build_merge
from coordinator_core.ops.fleet import _common
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PEER_FILE = "peer.txt"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "user.email=t@example.invalid",
         "-c", "user.name=t", *args],
        cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _init(tmp_path: Path, tracked: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for name, text in tracked.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _land_peer(repo: Path) -> str:
    (repo / _PEER_FILE).write_text("peer work\n", encoding="utf-8", newline="")
    _git(repo, "add", "--", _PEER_FILE)
    _git(repo, "commit", "-q", "-m", "peer lands", "--", _PEER_FILE)
    return _head(repo)


def _once(repo: Path, landed: list):
    """Callable landing the peer commit on its first call only."""
    def land() -> None:
        if not landed:
            landed.append(_land_peer(repo))
    return land


def _assert_peer_survives(repo: Path, landed: list) -> None:
    assert landed, "the peer commit was never injected: the seam did not fire"
    assert _head(repo) == landed[0]
    _git(repo, "cat-file", "-e", "HEAD:" + _PEER_FILE)


def test_commit_scoped_private_index_refuses_when_head_moves_after_base_capture(tmp_path, monkeypatch):
    repo = _init(tmp_path, {"file.txt": "original\n"})
    (repo / "file.txt").write_text("changed\n", encoding="utf-8", newline="")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("edit\n\nSession-Id: t\n", encoding="utf-8")
    landed: list = []
    land = _once(repo, landed)
    real = git_native._git_state_head_sha

    def seam(root, *a, **k):
        sha = real(root, *a, **k)
        land()
        return sha

    monkeypatch.setattr(git_native, "_git_state_head_sha", seam)

    result = git_native.commit_scoped(["file.txt"], msg_file, repo)

    assert result.ok is False
    _assert_peer_survives(repo, landed)


def test_propagate_commit_delivery_refuses_when_head_moves_after_base_capture(tmp_path, monkeypatch):
    rel = "state/handoffs/h.md"
    repo = _init(tmp_path, {rel: "---\nstatus: claimed\n---\nbody\n"})
    (repo / rel).write_text("---\nstatus: claimed\n---\nbody\n\n## Propagated\n", encoding="utf-8", newline="")
    landed: list = []
    land = _once(repo, landed)
    real = pb._run_git

    def seam(args, cwd, env=None):
        result = real(args, cwd, env=env)
        if args == ["rev-parse", "HEAD"]:
            land()
        return result

    monkeypatch.setattr(pb, "_run_git", seam)

    sha, error = pb._commit_delivery(repo, rel, "slug", "summary")

    assert sha is None and error
    assert "compare-and-swap" in error
    _assert_peer_survives(repo, landed)


def test_propagate_commit_delivery_seeds_its_index_from_the_captured_base(tmp_path, monkeypatch):
    rel = "state/handoffs/h.md"
    repo = _init(tmp_path, {rel: "x\n"})
    (repo / rel).write_text("y\n", encoding="utf-8", newline="")
    base = _head(repo)
    seen: list = []
    real = pb._run_git

    def spy(args, cwd, env=None):
        if args and args[0] == "read-tree":
            seen.append(args[1])
        return real(args, cwd, env=env)

    monkeypatch.setattr(pb, "_run_git", spy)

    sha, error = pb._commit_delivery(repo, rel, "slug", "summary")

    assert error is None and sha
    assert seen == [base]


def test_commit_merge_refuses_when_head_moves_after_base_capture(tmp_path, monkeypatch):
    root, _, params = _build_merge(tmp_path)
    landed: list = []
    land = _once(root, landed)
    real = merge_commit.run_git

    def seam(args, **kw):
        result = real(args, **kw)
        if "--git-common-dir" in args:
            land()
        return result

    monkeypatch.setattr(merge_commit, "run_git", seam)

    out = merge_commit._handler(params, repo_root=root / ".git")

    assert out["ok"] is False
    _assert_peer_survives(root, landed)


def test_rm_and_commit_refuses_when_head_moves_after_base_capture(tmp_path, monkeypatch):
    repo = _init(tmp_path, {"sidecar.txt": "s\n", "keep.txt": "k\n"})
    landed: list = []
    land = _once(repo, landed)
    real = _common._read_head_sha

    def seam(root, *a, **k):
        sha = real(root, *a, **k)
        land()
        return sha

    monkeypatch.setattr(_common, "_read_head_sha", seam)

    reaped, failed = asyncio.run(
        _common.rm_and_commit(repo, [repo / "sidecar.txt"], "reap sidecar")
    )

    assert reaped == [] and failed
    _assert_peer_survives(repo, landed)
    assert (repo / "sidecar.txt").exists()


def test_rm_and_commit_seeds_its_index_from_the_captured_base(tmp_path, monkeypatch):
    repo = _init(tmp_path, {"sidecar.txt": "s\n", "keep.txt": "k\n"})
    base = _head(repo)
    seeds: list = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        if argv[:2] == ("git", "read-tree"):
            seeds.append(argv[2])
        return await real_exec(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

    reaped, failed = asyncio.run(
        _common.rm_and_commit(repo, [repo / "sidecar.txt"], "reap sidecar")
    )

    assert reaped and not failed
    assert seeds == [base]
