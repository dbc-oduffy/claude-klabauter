"""Exact-equality spawn count for `plan.tasks.mutate` `resolve`: every `coded` row's cited sha is
checked against the row's `writes` by ONE batched `git show` however many rows the batch names.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

CODED_SHA_CHECK_SPAWNS = 1

_PLAN = """\
---
title: "Spawn Plan"
status: ready
---

# Spawn Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First
  change_kind: script-edit
  surface: a.py
  writes: [a.py]
  queue_scope: project
  deferred: false
  body: |
    First.
- id: C2
  title: Second
  change_kind: script-edit
  surface: b.py
  writes: [b.py]
  queue_scope: project
  deferred: false
  body: |
    Second.
```

## Trailer
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _resolve(repo: Path, plan: Path, resolves: list) -> dict:
    params = {"verb": "resolve", "plan_path": str(plan), "resolves": resolves}
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def test_resolve_of_two_coded_rows_spawns_one_batched_git_show(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "s.md", _PLAN)
    sha_a, sha_b = _commit(repo, "a.py"), _commit(repo, "b.py")
    resolves = [
        {"id": "C1", "disposition": "coded", "disposition_ref": sha_a},
        {"id": "C2", "disposition": "coded", "disposition_ref": sha_b},
    ]

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _resolve(repo, plan, resolves)

    assert result["exit_code"] == 0, result
    assert len(spawns) == CODED_SHA_CHECK_SPAWNS, [s.argv for s in spawns]
    assert [s.origin for s in spawns] == ["run_git"] * len(spawns)
    assert "show" in spawns[0].argv and sha_a in spawns[0].argv and sha_b in spawns[0].argv


def test_resolve_without_coded_rows_spawns_nothing(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "s.md", _PLAN)

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = _resolve(repo, plan, [{"id": "C1", "disposition": "open"}])

    assert result["exit_code"] == 0, result
    assert len(spawns) == 0, [s.argv for s in spawns]
