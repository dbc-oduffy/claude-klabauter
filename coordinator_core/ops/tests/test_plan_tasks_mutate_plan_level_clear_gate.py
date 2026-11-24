"""Tests for ``plan.tasks.mutate`` clear-gate with no ``id``: a row-less plan-level
frontmatter ``external_gate`` entry.

Pins: it clears with owner_repo + evidence; empty evidence and an owner mismatch refuse
bytes-unchanged; condition / gate_index disambiguate several entries; row-bound frontmatter
entries are not plan-level targets; task-level clear still works.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ONE = """\
---
title: "Gate Plan"
status: draft
external_gate:
  - id: g1
    owner_repo: claude-klabauter
    condition: claude-klabauter lands the flip
    blocks: execution
    cleared: false
---

# Gate Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: Chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the thing.
```

## Trailer
"""

_MANY = """\
---
title: "Gate Plan"
status: draft
external_gate:
  - id: g1
    owner_repo: claude-klabauter
    condition: first flip
    blocks: execution
    cleared: false
  - id: g2
    owner_repo: claude-klabauter
    condition: second flip
    blocks: execution
    cleared: false
  - id: g3
    row: C1
    owner_repo: claude-klabauter
    condition: row bound
    blocks: execution
    cleared: false
---

# Gate Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: Chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the thing.
```

## Trailer
"""


def _call(repo: Path, plan: Path, **extra) -> dict:
    params = {"verb": "clear-gate", "plan_path": str(plan),
              "owner_repo": "claude-klabauter", "evidence": "claude-klabauter commit abc1234 landed"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def test_no_id_clears_row_less_plan_level_gate(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", _ONE)

    result = _call(repo, plan)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    text = plan.read_text(encoding="utf-8")
    assert "cleared: true" in text
    assert "cleared: false" not in text
    assert "closure_evidence: claude-klabauter commit abc1234 landed" in text


def test_empty_evidence_refuses_bytes_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", _ONE)
    before = plan.read_bytes()

    result = _call(repo, plan, evidence="  ")

    assert result["exit_code"] != 0
    assert "'evidence' is required" in str(result)
    assert plan.read_bytes() == before


def test_owner_mismatch_refuses_bytes_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", _ONE)
    before = plan.read_bytes()

    result = _call(repo, plan, owner_repo="coordinator-content-repo")

    assert result["exit_code"] != 0
    assert "no external_gate matches" in str(result)
    assert plan.read_bytes() == before


def test_second_call_is_noop(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", _ONE)
    assert _call(repo, plan)["applied"] is True
    again = _call(repo, plan)
    assert again["exit_code"] == 0
    assert again["applied"] is False


def test_several_entries_need_selection_and_row_bound_is_excluded(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", _MANY)
    before = plan.read_bytes()

    ambiguous = _call(repo, plan)
    assert ambiguous["exit_code"] != 0
    assert "2 external_gate entries match" in str(ambiguous)
    assert "row bound" not in str(ambiguous)
    assert plan.read_bytes() == before

    by_condition = _call(repo, plan, condition="second")
    assert by_condition["exit_code"] == 0, by_condition
    assert by_condition["applied"] is True
    text = plan.read_text(encoding="utf-8")
    assert text.count("cleared: true") == 1
    assert text.count("cleared: false") == 2
    assert "id: g2\n    owner_repo: claude-klabauter\n    condition: second flip\n    blocks: execution\n    cleared: true" in text

    by_index = _call(repo, plan, gate_index=1)
    assert by_index["applied"] is True
    assert plan.read_text(encoding="utf-8").count("cleared: true") == 2


def test_task_level_clear_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", _MANY)

    result = _call(repo, plan, id="C1")

    assert result["exit_code"] == 0, result
    text = plan.read_text(encoding="utf-8")
    assert text.count("cleared: true") == 1
    assert "condition: row bound\n    blocks: execution\n    cleared: true" in text


def test_no_id_clears_plan_level_gate_when_plan_has_no_task_spine(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", _ONE.split("## Tasks")[0] + "## Trailer\n")

    result = _call(repo, plan)

    assert result["exit_code"] == 0, result
    assert "cleared: true" in plan.read_text(encoding="utf-8")
