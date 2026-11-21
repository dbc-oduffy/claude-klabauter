"""`land_credited_profile`: the scale check, the flatness test, and the (a) and (h) legs on a tmp_path repo."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_credited_profile as lcp

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, creationflags=NO_WINDOW)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(root, "config", key, value)
    old = time.time() - 3600
    for name in ("a.py", "b.py"):
        (root / name).write_text(f"VALUE = '{name}'\n", encoding="utf-8")
        os.utime(root / name, (old, old))
    _git(root, "add", "a.py", "b.py")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _context(root: Path, declared: list) -> lcp.SpikeContext:
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(declared)),), plan_path="docs/plans/p.md")
    (root / "script.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return lcp.SpikeContext(root, "ask-20260101T000000-abcdef", {"script_path": "script.mjs"})


def test_credited_differing_names_only_paths_that_differ_from_head(tmp_path):
    root = _repo(tmp_path)
    (root / "a.py").write_text("VALUE = 'a changed, and longer than before'\n", encoding="utf-8")
    ctx = _context(root, ["a.py", "b.py", "c.py"])
    (root / "c.py").write_text("NEW = 1\n", encoding="utf-8")
    detail = lcp.leg_a_credited_differing(ctx)
    assert detail["gap"] is False
    assert ctx.differing == ["a.py", "c.py"]
    assert ctx.plan_rel == "docs/plans/p.md"


def test_commit_leg_moves_the_ref_with_exactly_the_credited_paths(tmp_path):
    root = _repo(tmp_path)
    ctx = _context(root, ["a.py", "b.py"])
    (root / "a.py").write_text("VALUE = 'a changed, and longer than before'\n", encoding="utf-8")
    (root / "peer.py").write_text("PEER = 1\n", encoding="utf-8")
    lcp.leg_a_credited_differing(ctx)
    before = lcp._head(root)
    detail = lcp.leg_h_commit(ctx)
    assert detail["moved"] is True and detail["committed"] == 1
    assert lcp._head(root) != before
    shown = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(root), capture_output=True, check=True, creationflags=NO_WINDOW)
    assert shown.stdout.decode().split() == ["a.py"]


def test_dirty_step_refuses_a_clone_with_no_credited_path(tmp_path):
    root = _repo(tmp_path)
    ctx = _context(root, ["absent.py"])
    lcp.leg_a_credited_differing(ctx)
    with pytest.raises(FileNotFoundError):
        lcp.dirty_credited_paths(ctx, 0)
