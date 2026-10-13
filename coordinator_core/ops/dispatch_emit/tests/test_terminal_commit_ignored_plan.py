"""The coded-stamp commit never stages a gitignored, untracked plan (a warp run spine)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = """---
title: p
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""


def _git(args, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, check=True,
        **no_console_creationflags(),
    )


def _stamp(root: Path, rel: str) -> list:
    seen: list = []

    def commit_v2(params: dict, repo_root: Path) -> dict:
        seen.append(list(params["paths"]))
        return {"committed": True, "sha": "f" * 40}

    terminal_commit._stamp_coded_commit(commit_v2, root, root, {rel: {"C3"}}, "a" * 40, None)
    return seen


@pytest.fixture
def root(tmp_path):
    _git(["init", "-q"], tmp_path)
    (tmp_path / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    return tmp_path


def test_ignored_untracked_plan_is_not_staged(root):
    rel = "scratch/warp/run/p.spine.md"
    (root / rel).parent.mkdir(parents=True)
    (root / rel).write_text(_PLAN, encoding="utf-8")
    assert _stamp(root, rel) == []
    assert "coded" in (root / rel).read_text(encoding="utf-8")


def test_tracked_plan_is_still_staged(root):
    rel = "docs/plan.md"
    (root / rel).parent.mkdir()
    (root / rel).write_text(_PLAN, encoding="utf-8")
    _git(["add", "."], root)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "p"], root)
    assert _stamp(root, rel) == [[rel]]


def _stamp_receipt(root: Path, rel: str) -> dict:
    return terminal_commit._stamp_coded_commit(
        lambda params, repo_root: {"committed": True, "sha": "f" * 40},
        root, root, {rel: {"C3"}}, "a" * 40, None, also_commit=(rel,), receipt=rel,
    )


def test_ignored_receipt_is_a_loud_coded_stamp_error(root):
    rel = "scratch/warp/run/p.spine.md"
    (root / rel).parent.mkdir(parents=True)
    (root / rel).write_text(_PLAN, encoding="utf-8")
    out = _stamp_receipt(root, rel)
    assert rel in out["coded_stamp_error"]


def test_tracked_receipt_is_committed_without_error(root):
    rel = "docs/plan.md"
    (root / rel).parent.mkdir()
    (root / rel).write_text(_PLAN, encoding="utf-8")
    _git(["add", "."], root)
    _git(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "p"], root)
    out = _stamp_receipt(root, rel)
    assert "coded_stamp_error" not in out and out["coded_sha"] == "f" * 40
