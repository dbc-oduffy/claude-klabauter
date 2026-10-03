"""Tests for the plan.tasks.mutate ``clear-gate`` verb.

Pins: a gate with no ``cleared`` key becomes ``cleared: true`` + ``closure_evidence``
and the row then reads as dispatchable; empty evidence refuses with the plan bytes
unchanged; a second call is ``applied: False``; ambiguous and absent matches refuse.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ROW_GATED = """\
- id: C1
  title: Gated chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  external_gate:
    - owner_repo: coordinator-content-repo
      condition: DoE lands the flip
      requires: commit-in-owner-repo
      blocks: execution
  body: |
    Do the thing.
"""

_PLAN = """\
---
title: "Gate Plan"
status: draft
---

# Gate Plan

## Tasks

```yaml plan-tasks
{rows}```

## Trailer
"""

_FM_PLAN = """\
---
title: "Gate Plan"
status: draft
external_gate:
  - id: g1
    row: C1
    owner_repo: coordinator-content-repo
    condition: DoE lands the flip
    blocks: execution
    cleared: false
---

# Gate Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: Gated chunk
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
    params = {"verb": "clear-gate", "plan_path": str(plan), "id": "C1",
              "owner_repo": "coordinator-content-repo", "evidence": "DoE commit abc1234 landed the flip"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def _excluded(plan: Path) -> list:
    exclusions: list = []
    read_spine(plan, exclusions)
    return [e["id"] for e in exclusions]


def test_row_level_gate_clears_and_row_is_dispatchable(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    assert _excluded(plan) == ["C1"]

    result = _call(repo, plan)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    text = plan.read_text(encoding="utf-8")
    assert "cleared: true" in text
    assert "closure_evidence: DoE commit abc1234 landed the flip" in text
    assert _excluded(plan) == []


def test_empty_evidence_refuses_bytes_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    before = plan.read_bytes()

    for ev in ("", "   "):
        result = _call(repo, plan, evidence=ev)
        assert result["exit_code"] == 1, result
        assert result["applied"] is False
    assert plan.read_bytes() == before


def test_second_call_is_not_applied(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    assert _call(repo, plan)["applied"] is True
    after_first = plan.read_bytes()

    again = _call(repo, plan)

    assert again["exit_code"] == 0, again
    assert again["applied"] is False
    assert plan.read_bytes() == after_first


def test_unknown_row_and_no_matching_gate_refuse(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))
    before = plan.read_bytes()

    assert _call(repo, plan, id="C9")["exit_code"] == 1
    miss = _call(repo, plan, owner_repo="other-repo")
    assert miss["exit_code"] == 1 and "no external_gate matches" in miss["error"]
    assert plan.read_bytes() == before


def test_two_gates_one_owner_repo_refuse_naming_them(tmp_path):
    repo = _make_git_repo(tmp_path)
    rows = _ROW_GATED.replace(
        "      blocks: execution\n",
        "      blocks: execution\n    - owner_repo: coordinator-content-repo\n      condition: second\n"
        "      blocks: execution\n",
    )
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=rows))
    before = plan.read_bytes()

    result = _call(repo, plan)

    assert result["exit_code"] == 1
    assert "2 external_gate entries match" in result["error"]
    assert plan.read_bytes() == before


def test_frontmatter_gate_with_row_clears(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "fm.md", _FM_PLAN)
    assert _excluded(plan) == ["C1"]

    result = _call(repo, plan)

    assert result["exit_code"] == 0 and result["applied"] is True, result
    text = plan.read_text(encoding="utf-8")
    assert "cleared: true" in text and "cleared: false" not in text
    assert text.count("## Trailer") == 1 and "title: Gate Plan" in text.replace('"', "")
    assert _excluded(plan) == []
    assert _call(repo, plan)["applied"] is False


def test_verb_list_error_names_clear_gate(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = asyncio.run(_handler({"plan_path": "x"}, repo_root=repo / ".git"))
    assert "clear-gate" in result["error"]
