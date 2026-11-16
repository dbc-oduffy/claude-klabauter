"""
coordinator_core.ops.tests.test_plan_tasks_mutate_register_rows_empty

A row declaring `register_rows: []` (resting on no register row) is schema-valid,
so plan.tasks.mutate resolve marks it coded; a malformed register_rows still refuses.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.win_portability import no_console_creationflags

_SCHEMA = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "plan-tasks.schema.json"

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
  register_rows: %s
  body: |
    Do the first thing.
```
"""


def _repo(tmp_path: Path, register_rows: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "docs" / "plans").mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo), capture_output=True, check=True, **no_console_creationflags(),
    )
    plan = repo / "docs" / "plans" / "p.md"
    plan.write_text(_PLAN % register_rows, encoding="utf-8")
    return repo, plan


def _resolve(repo: Path, plan: Path) -> dict:
    return asyncio.run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "coded",
            "disposition_ref": "abc1234",
            "disposition_detail": "shipped",
        },
        repo_root=repo / ".git",
    ))


def test_empty_register_rows_resolves_coded(tmp_path):
    repo, plan = _repo(tmp_path, "[]")
    result = _resolve(repo, plan)
    assert result["exit_code"] == 0, result
    assert "disposition: coded" in plan.read_text(encoding="utf-8")


def test_malformed_register_rows_still_refused(tmp_path):
    repo, plan = _repo(tmp_path, '["R1", "R1"]')
    before = plan.read_bytes()
    result = _resolve(repo, plan)
    assert result["exit_code"] != 0, result
    assert plan.read_bytes() == before


def test_schema_register_rows_has_no_min_items():
    prop = json.loads(_SCHEMA.read_text(encoding="utf-8"))["properties"]["register_rows"]
    assert "minItems" not in prop
