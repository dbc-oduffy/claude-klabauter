"""Tests for the plan.tasks.mutate ``em-discharge`` verb and ``read_em_discharges``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.plan_tasks_mutate import (
    EM_DISCHARGE_KEY,
    _handler,
    evidence_sidecar_path,
    read_em_discharges,
    read_row_evidence,
)
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Discharge Plan"
status: ready
---

# Discharge Plan

## Tasks

```yaml plan-tasks
- id: E1
  title: EM step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: open
  body: |
    Do it.
- id: M1
  title: Send memo
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  kind: memo-send
  body: |
    Send.
- id: O1
  title: Operator step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  execution_mode: operator
  body: |
    Operator.
- id: X1
  title: Executor step
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  body: |
    Code.
- id: D1
  title: Already closed
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  performer: em
  disposition: wont_do
  body: |
    Closed.
```

## Trailer
"""


def _plan(tmp_path: Path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "d.md", "")
    plan.write_text(_PLAN, encoding="utf-8", newline="")
    return repo, plan


def _call(repo: Path, plan: Path, **extra) -> dict:
    params = {"verb": "em-discharge", "plan_path": str(plan), "id": "E1", "text": "probe done"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def test_discharge_writes_key_and_leaves_plan_bytes(tmp_path):
    repo, plan = _plan(tmp_path)
    before = plan.read_bytes()
    result = _call(repo, plan)
    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert plan.read_bytes() == before
    got = read_em_discharges(plan)
    assert set(got) == {"E1"}
    assert got["E1"]["text"] == "probe done"
    assert got["E1"]["recorded_at"]
    assert read_row_evidence(plan) == {}
    assert EM_DISCHARGE_KEY == "em_discharged"


def test_second_discharge_is_noop_not_overwrite(tmp_path):
    repo, plan = _plan(tmp_path)
    _call(repo, plan)
    first = read_em_discharges(plan)["E1"]
    result = _call(repo, plan, text="different")
    assert result["exit_code"] == 0 and result["applied"] is False
    assert read_em_discharges(plan)["E1"] == first


@pytest.mark.parametrize("row_id", ["NOPE", "M1", "O1", "X1", "D1"])
def test_refusals_write_nothing(tmp_path, row_id):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, id=row_id)
    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert not evidence_sidecar_path(plan).exists()


@pytest.mark.parametrize("extra", [{"id": ""}, {"text": ""}, {"text": "   "}])
def test_empty_id_or_text_refused(tmp_path, extra):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, **extra)
    assert result["exit_code"] == 1
    assert not evidence_sidecar_path(plan).exists()


def test_reader_never_raises(tmp_path):
    plan = tmp_path / "p.md"
    assert read_em_discharges(plan) == {}
    evidence_sidecar_path(plan).write_text(": : [garbled", encoding="utf-8")
    assert read_em_discharges(plan) == {}
    evidence_sidecar_path(plan).write_text("em_discharged: [1, 2]\n", encoding="utf-8")
    assert read_em_discharges(plan) == {}
