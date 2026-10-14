"""A delivered_elsewhere spine row is closed for stamp-plan-implemented and for the close's no-commit-row guard."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.archive_stamp import cs_stamp_plan_implemented
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan
from coordinator_core.workstream_complete import apply as ws_apply

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Delivered Elsewhere Close"
status: draft
schema_version: '1.2.0'
deliverable_id: "dlv-fixture-delivered-elsewhere-01"
---

# Delivered Elsewhere Close

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  writes:
  - a.py
  queue_scope: project
  deferred: false
  disposition: open
  body: |
    First.
```
"""


def _resolve_delivered_elsewhere(repo, plan):
    return asyncio.run(_handler(
        {
            "verb": "resolve", "plan_path": str(plan), "id": "C1",
            "disposition": "delivered_elsewhere",
            "disposition_ref": "other-repo@abc1234def",
            "disposition_detail": "landed as another repo's commit",
            "delivered_via": "direct-message",
        },
        repo_root=repo / ".git",
    ))


def test_stamp_refuses_an_open_row_then_accepts_it_delivered_elsewhere(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "de-close.md", _PLAN)

    assert cs_stamp_plan_implemented(str(plan), refuse_open_spine_rows=True, commit=False) == 1
    assert "C1" in capsys.readouterr().err

    closed = _resolve_delivered_elsewhere(repo, plan)
    assert closed["exit_code"] == 0, closed
    capsys.readouterr()

    assert cs_stamp_plan_implemented(str(plan), refuse_open_spine_rows=True, commit=False) == 0


def test_no_commit_row_judgment_surfaces_open_row_and_ignores_delivered_elsewhere(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "de-judgment.md", _PLAN)
    decisions = {"governing_plan_slug": "de-judgment"}

    before = ws_apply._no_commit_row_judgment(decisions, repo)
    assert before is not None and "C1" in before["evidence"]

    closed = _resolve_delivered_elsewhere(repo, plan)
    assert closed["exit_code"] == 0, closed

    assert ws_apply._no_commit_row_judgment(decisions, repo) is None
