"""plan.tasks.mutate redirects a flat docs/plans path to its canonical month shard."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_NAME = "2026-11-12-canonical-b2a5e6.md"
_PLAN = """\
---
title: "Canonical Plan"
status: ready
---

# Canonical Plan

## Tasks

```yaml plan-tasks
- id: C2
  title: Step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  disposition: open
  body: |
    Do it.
```
"""


def _seed(repo: Path, rel: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PLAN, encoding="utf-8")
    return path


def _resolve(repo: Path, plan_path: str) -> dict:
    return asyncio.run(
        _handler(
            {"verb": "resolve", "plan_path": plan_path, "id": "C2", "disposition": "coded",
             "disposition_ref": "abc1234"},
            repo / ".git",
        )
    )


def _is_coded(path: Path) -> bool:
    return "disposition: coded" in path.read_text(encoding="utf-8")


def test_flat_cite_with_only_shard_writes_shard(tmp_path):
    repo = _make_git_repo(tmp_path)
    shard = _seed(repo, f"docs/plans/2026-11/{_NAME}")
    result = _resolve(repo, f"docs/plans/{_NAME}")
    assert result["exit_code"] == 0, result
    assert _is_coded(shard)
    assert not (repo / "docs" / "plans" / _NAME).exists()
    assert result["plan_path"] == f"docs/plans/2026-11/{_NAME}"
    assert "advisory" not in result


def test_both_exist_writes_shard_and_names_flat_duplicate(tmp_path):
    repo = _make_git_repo(tmp_path)
    shard = _seed(repo, f"docs/plans/2026-11/{_NAME}")
    flat = _seed(repo, f"docs/plans/{_NAME}")
    result = _resolve(repo, f"docs/plans/{_NAME}")
    assert result["exit_code"] == 0, result
    assert _is_coded(shard)
    assert not _is_coded(flat)
    assert result["plan_path"] == f"docs/plans/2026-11/{_NAME}"
    assert f"docs/plans/{_NAME}" in result["advisory"]


def test_flat_only_stays_flat(tmp_path):
    repo = _make_git_repo(tmp_path)
    flat = _seed(repo, f"docs/plans/{_NAME}")
    result = _resolve(repo, f"docs/plans/{_NAME}")
    assert result["exit_code"] == 0, result
    assert _is_coded(flat)
    assert "plan_path" not in result


def test_shard_cite_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    shard = _seed(repo, f"docs/plans/2026-11/{_NAME}")
    result = _resolve(repo, f"docs/plans/2026-11/{_NAME}")
    assert result["exit_code"] == 0, result
    assert _is_coded(shard)
    assert "plan_path" not in result and "advisory" not in result


def test_path_outside_docs_plans_still_refused(tmp_path):
    repo = _make_git_repo(tmp_path)
    _seed(repo, "elsewhere/x.md")
    result = _resolve(repo, "elsewhere/x.md")
    assert result["exit_code"] == 1
    assert "escapes docs/plans" in result["error"]
