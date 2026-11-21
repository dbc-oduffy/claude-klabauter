"""Pins for the plan-completion ledger and the commit-sha seam on `GitResult`."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core import plan_completion_ledger as led

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return res.stdout.strip()


def test_blob_id_equals_git_hash_object(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    for data in (b"", b"x\n", "café\r\nline\n".encode("utf-8"), bytes(range(256))):
        f = tmp_path / "f.bin"
        f.write_bytes(data)
        assert led.blob_id(data) == _git(tmp_path, "hash-object", "--no-filters", "f.bin")


def test_commit_authored_content_and_new_file_surface_the_commit_sha(tmp_path: Path) -> None:
    from coordinator_core.ops.ceremony import git_native

    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    msg = tmp_path / "msg.txt"
    msg.write_text("ledger row\n", encoding="utf-8")

    created = git_native.commit_authored_new_file("state/new.jsonl", "a\n", msg, tmp_path)
    assert created.ok, created.stderr
    assert created.commit_sha == _git(tmp_path, "rev-parse", "HEAD")

    edited = git_native.commit_authored_content("state/new.jsonl", "a\nb\n", msg, tmp_path)
    assert edited.ok, edited.stderr
    assert edited.commit_sha == _git(tmp_path, "rev-parse", "HEAD") != created.commit_sha

    refused = git_native.GitResult(returncode=1, stdout="", stderr="x")
    assert refused.commit_sha is None
