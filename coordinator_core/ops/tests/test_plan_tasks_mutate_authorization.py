"""plan.tasks.mutate stamp writes a row's `authorization` and a re-read returns it unchanged.

Runs against the vendored plan-tasks schema: an `em-inferred` record round-trips, an unknown
`source` is refused with zero bytes written.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Authorization Plan"
status: draft
---

# Authorization Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    First.
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    Second.
```
"""

_EM_INFERRED = {"source": "em-inferred", "rationale": "Follows from the stated goal.", "on": "2026-10-14"}


def _stamp(repo, plan, authorization):
    return asyncio.run(_handler(
        {"verb": "stamp", "plan_path": str(plan),
         "updates": [{"id": "C1", "authorization": authorization}]},
        repo_root=repo / ".git",
    ))


def _rows(plan) -> dict:
    return {r["id"]: r for r in load_rows(plan.read_text(encoding="utf-8")).rows}


def test_stamp_writes_em_inferred_authorization_and_read_returns_it(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "auth-ok.md", _PLAN)

    result = _stamp(repo, plan, dict(_EM_INFERRED))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    rows = _rows(plan)
    assert rows["C1"]["authorization"] == _EM_INFERRED
    assert "authorization" not in rows["C2"]


def test_restamping_identical_authorization_is_a_no_op(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "auth-idem.md", _PLAN)
    assert _stamp(repo, plan, dict(_EM_INFERRED))["exit_code"] == 0
    before = plan.read_bytes()

    result = _stamp(repo, plan, dict(_EM_INFERRED))

    assert result["exit_code"] == 0, result
    assert plan.read_bytes() == before


def test_stamp_refuses_unknown_authorization_source_and_writes_nothing(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "auth-bad.md", _PLAN)
    original = plan.read_bytes()

    result = _stamp(repo, plan, {**_EM_INFERRED, "source": "self-granted"})

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_bytes() == original


def test_stamp_refuses_bare_scalar_authorization_and_writes_nothing(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "auth-scalar.md", _PLAN)
    original = plan.read_bytes()

    result = _stamp(repo, plan, "em-inferred")

    assert result["exit_code"] == 1, result
    assert plan.read_bytes() == original


@pytest.mark.parametrize("signoff", [
    {"source": "pm", "pm_quote": "Do it.", "on": "2026-10-14"},
    {"source": "apm", "apm_ruling": "Approved.", "ruling_ref": "run-1", "on": "2026-10-14"},
    {"source": "g-em", "approver": "g-em-1", "ruling": "Approved.", "ruling_ref": "run-1", "on": "2026-10-14"},
    {"source": "uhura", "approver": "uhura-1", "ruling": "Approved.", "ruling_ref": "run-1", "on": "2026-10-14"},
])
def test_stamp_refuses_signoff_arm_authorization_and_writes_nothing(tmp_path, signoff):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "auth-signoff.md", _PLAN)
    original = plan.read_bytes()

    result = _stamp(repo, plan, signoff)

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_bytes() == original
