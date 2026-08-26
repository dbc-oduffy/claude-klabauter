"""
coordinator_core.ops.session.tests.test_record_pickup — session.record_pickup op tests.

Coverage:
  - AC-9: a subprocess-spy test (`patch("subprocess.run", wraps=...)`, never
    static inspection) proving `session.record_pickup`'s handler makes ZERO
    git subprocess spawns for a normal write — the positive assertion behind
    the C5 seam-routing finding recorded in `record_pickup.py`'s module
    docstring: this op's own write target
    (`<git_common_dir>/coordinator-sessions/<sid>/session-shape.json`) sits
    under `.git/`, is untracked by construction, and is therefore never a
    candidate for `commit_pipeline :: commit`'s `commit_paths` — there is no
    git activity to fold in, so a spawn-count ratchet of exactly 0 is the
    test that stands behind that "cannot reach it" conclusion, mirroring the
    spawn-count assertion pattern C4 uses for its own fold-in seam
    (`test_commit_pipeline_archival_fold_in.py`).
  - Functional coverage of the read-modify-write itself: create-if-absent,
    flat `pickup` REPLACE semantics, append-only `pickup_history` ledger, a
    second (pivot) call producing `repoint_detected: True`, and the optional
    `deliverable_id` sibling field's omit-rather-than-guess contract.

Real git repo is used as the fixture root (a throwaway `git init` per test),
matching this directory's other real-git-fixture modules — the point of the
AC-9 test is proving *this op* never shells out to git even though a real
repo is present and resolvable, not proving git itself is absent.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from coordinator_core.ops.session.record_pickup import _handler
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors this
    # directory's sibling real-git fixtures; no console window risk on the
    # CI/dev platforms this suite runs on.
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=10,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


@pytest.fixture()
def real_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run(params: dict) -> dict:
    return asyncio.run(_handler(params, repo_root=None))


def _shape_path(repo: Path, sid: str) -> Path:
    return repo / ".git" / "coordinator-sessions" / sid / "session-shape.json"


def test_record_pickup_makes_zero_git_spawns(real_repo: Path) -> None:
    """AC-9: the pickup occasion's own op never shells out to git — the
    positive assertion behind the C5 finding that it cannot reach
    `commit_pipeline :: commit`'s `commit_paths` (its write target is
    untracked by construction, so there is nothing eligible to stage)."""
    real_run = subprocess.run
    with patch("subprocess.run", wraps=real_run) as spy:
        result = _run(
            {
                "sid": "sess-spawncount",
                "handoff_relpath": "state/handoffs/foo.md",
                "repo_root": str(real_repo),
            }
        )
    assert result["exit_code"] == 0
    assert spy.call_count == 0


def test_record_pickup_creates_flat_pickup_and_history(real_repo: Path) -> None:
    result = _run(
        {
            "sid": "sess-1",
            "handoff_relpath": "state/handoffs/foo.md",
            "repo_root": str(real_repo),
        }
    )
    assert result["exit_code"] == 0
    assert result["pickup"] == {"happened": True, "handoff": "state/handoffs/foo.md"}
    assert result["pickup_history_len"] == 1
    assert result["repoint_detected"] is False

    on_disk = json.loads(_shape_path(real_repo, "sess-1").read_text(encoding="utf-8"))
    assert on_disk["pickup"] == {"happened": True, "handoff": "state/handoffs/foo.md"}
    assert len(on_disk["pickup_history"]) == 1


def test_record_pickup_second_call_appends_and_flags_repoint(real_repo: Path) -> None:
    _run(
        {
            "sid": "sess-2",
            "handoff_relpath": "state/handoffs/a.md",
            "repo_root": str(real_repo),
        }
    )
    result = _run(
        {
            "sid": "sess-2",
            "handoff_relpath": "state/handoffs/b.md",
            "repo_root": str(real_repo),
        }
    )
    assert result["exit_code"] == 0
    assert result["pickup"] == {"happened": True, "handoff": "state/handoffs/b.md"}
    assert result["pickup_history_len"] == 2
    assert result["repoint_detected"] is True

    on_disk = json.loads(_shape_path(real_repo, "sess-2").read_text(encoding="utf-8"))
    handoffs = [entry["handoff"] for entry in on_disk["pickup_history"]]
    assert handoffs == ["state/handoffs/a.md", "state/handoffs/b.md"]


def test_record_pickup_deliverable_id_optional_and_omitted_when_blank(real_repo: Path) -> None:
    result = _run(
        {
            "sid": "sess-3",
            "handoff_relpath": "state/handoffs/foo.md",
            "repo_root": str(real_repo),
            "deliverable_id": "  ",
        }
    )
    assert result["exit_code"] == 0
    assert "deliverable_id" not in result["pickup"]

    result2 = _run(
        {
            "sid": "sess-3",
            "handoff_relpath": "state/handoffs/bar.md",
            "repo_root": str(real_repo),
            "deliverable_id": "dlv-123",
        }
    )
    assert result2["pickup"]["deliverable_id"] == "dlv-123"
    on_disk = json.loads(_shape_path(real_repo, "sess-3").read_text(encoding="utf-8"))
    assert on_disk["pickup_history"][-1]["deliverable_id"] == "dlv-123"
    assert "deliverable_id" not in on_disk["pickup_history"][0]


def test_record_pickup_rejects_absolute_or_escaping_handoff_relpath(real_repo: Path) -> None:
    abs_result = _run(
        {
            "sid": "sess-4",
            "handoff_relpath": str((real_repo / "state/handoffs/foo.md").resolve()),
            "repo_root": str(real_repo),
        }
    )
    assert abs_result["exit_code"] == 1

    escape_result = _run(
        {
            "sid": "sess-4",
            "handoff_relpath": "../../etc/passwd",
            "repo_root": str(real_repo),
        }
    )
    assert escape_result["exit_code"] == 1
