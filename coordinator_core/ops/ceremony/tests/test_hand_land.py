"""`ceremony.hand_land` lands named paths through `commit_paths` (CAS on HEAD, worktree sync),
carries a `Hand-Landed: true` trailer, and refuses a stale `expect_head` with nothing moved.
Real git fixture under `tmp_path`; the op is called through its registered entry."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.hand_land  # noqa: F401 -- registers the op
from coordinator_core import ipc

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    (root / "a.py").write_bytes(b"one\ntwo\n")
    (root / "b.py").write_bytes(b"one\ntwo\n")
    _git(root, "add", "--", "a.py", "b.py")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _call(params: dict) -> dict:
    handler = ipc._REGISTRY["ceremony.hand_land"]
    out = handler(params, None)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def test_default_arguments_land_the_named_path_with_trailer_and_clean_sync(repo: Path) -> None:
    (repo / "a.py").write_bytes(b"one\ntwo\nthree\n")
    (repo / "b.py").write_bytes(b"peer\ntwo\n")

    reply = _call({"repo_root": str(repo), "paths": ["a.py"], "message": "hand land a"})

    assert reply["ok"] is True
    assert reply["sync_refused"] == []
    assert "sync_incomplete" not in reply
    assert reply["sha"] == _git(repo, "rev-parse", "HEAD")
    body = _git(repo, "log", "-1", "--format=%B")
    assert body.splitlines()[0] == "hand land a"
    assert "Hand-Landed: true" in body.splitlines()
    assert _git(repo, "show", "HEAD:a.py") == "one\ntwo\nthree"
    assert _git(repo, "diff", "--name-only", "HEAD~1", "HEAD") == "a.py"
    assert (repo / "b.py").read_bytes() == b"peer\ntwo\n"
    assert _git(repo, "status", "--porcelain", "--", "a.py") == ""


def test_matching_expect_head_lands_and_a_stale_one_refuses_with_nothing_moved(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_bytes(b"changed\n")

    stale = _call({
        "repo_root": str(repo), "paths": ["a.py"], "message": "m", "expect_head": "0" * 12,
    })

    assert stale["ok"] is False and stale["refused"] == "stale_head"
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / "a.py").read_bytes() == b"changed\n"

    fresh = _call({
        "repo_root": str(repo), "paths": ["a.py"], "message": "m", "expect_head": head[:12],
    })

    assert fresh["ok"] is True
    assert _git(repo, "rev-parse", "HEAD~1") == head


@pytest.mark.parametrize(
    "params",
    [
        {"paths": [], "message": "m"},
        {"paths": ["a.py"], "message": "  "},
        {"paths": ["a.py"], "message": "m", "expect_head": ""},
        {"paths": "a.py", "message": "m"},
    ],
)
def test_bad_params_refuse_before_touching_git(repo: Path, params: dict) -> None:
    head = _git(repo, "rev-parse", "HEAD")

    reply = _call({"repo_root": str(repo), **params})

    assert reply["ok"] is False and reply["refused"] == "params"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_nothing_to_commit_refuses_without_moving_head(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD")

    reply = _call({"repo_root": str(repo), "paths": ["a.py"], "message": "m"})

    assert reply["ok"] is False and reply["refused"] == "commit"
    assert _git(repo, "rev-parse", "HEAD") == head


def test_call_of_a_name_only_in_the_worktree_refuses_and_a_committed_name_lands(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_bytes(b"")
    (repo / "pkg" / "idx.py").write_bytes(b"def old():\n    return 1\n")
    _git(repo, "add", "--", "pkg")
    _git(repo, "commit", "-q", "-m", "pkg")
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg" / "idx.py").write_bytes(b"def old():\n    return 1\n\n\ndef record_claim():\n    return 2\n")
    (repo / "a.py").write_bytes(b"from pkg import idx\n\nidx.record_claim()\n")

    refused = _call({"repo_root": str(repo), "paths": ["a.py"], "message": "m"})

    assert refused["ok"] is False and refused["refused"] == "uncommitted_name"
    assert _git(repo, "rev-parse", "HEAD") == head

    (repo / "a.py").write_bytes(b"from pkg import idx\n\nidx.old()\n")

    landed = _call({"repo_root": str(repo), "paths": ["a.py"], "message": "m"})

    assert landed["ok"] is True
    assert "gate_unverified" not in landed
