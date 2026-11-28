"""add-task folds a supplied `description` into the row `body` the pipeline reads."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import yaml

import coordinator_core.ops.plan_tasks_mutate  # noqa: F401 — fires @register_op
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.win_portability import no_console_creationflags

_PLAN = """\
---
title: "Test Plan"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the first thing.
```
"""


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    for args in (["init", "-b", "main"], ["config", "user.email", "t@t.test"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True, **no_console_creationflags())
    plan = repo / "docs" / "plans" / "p.md"
    plan.write_text(_PLAN, encoding="utf-8")
    return repo, plan


def _task(**extra) -> dict:
    return {
        "id": "C2", "title": "Second chunk", "change_kind": "script-edit",
        "surface": "some/other.py", "queue_scope": "project", "deferred": False, **extra,
    }


def _add(repo: Path, plan: Path, task: dict) -> dict:
    return asyncio.run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": task}, repo_root=repo / ".git",
    ))


def _row(plan: Path, row_id: str) -> dict:
    located = locate_fenced_block(plan.read_text(encoding="utf-8"))
    return next(r for r in yaml.safe_load(located.body) if r["id"] == row_id)


def test_description_lands_as_body(tmp_path: Path) -> None:
    repo, plan = _repo(tmp_path)
    result = _add(repo, plan, _task(description="Detail the pipeline must read."))
    assert result["exit_code"] == 0, result
    row = _row(plan, "C2")
    assert "Detail the pipeline must read." in row["body"]
    assert "description" not in row


def test_description_and_body_both_kept(tmp_path: Path) -> None:
    repo, plan = _repo(tmp_path)
    result = _add(repo, plan, _task(body="Original body.\n", description="Extra detail."))
    assert result["exit_code"] == 0, result
    body = _row(plan, "C2")["body"]
    assert "Original body." in body and "Extra detail." in body


def test_body_alone_unchanged(tmp_path: Path) -> None:
    repo, plan = _repo(tmp_path)
    result = _add(repo, plan, _task(body="Only body.\n"))
    assert result["exit_code"] == 0, result
    assert _row(plan, "C2")["body"] == "Only body.\n"
