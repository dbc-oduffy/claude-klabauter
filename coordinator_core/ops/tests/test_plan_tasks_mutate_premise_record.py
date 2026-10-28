"""Tests for the plan.tasks.mutate ``premise-record`` verb."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.premise_gate import read_premise_verdicts
from coordinator_core.ops.plan_tasks_mutate import _handler, evidence_sidecar_path
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Premise Plan"
status: ready
---

# Premise Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: Premise
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  disposition: open
  body: |
    Decide.
```
"""


def _plan(tmp_path: Path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "p.md", "")
    plan.write_text(_PLAN, encoding="utf-8", newline="")
    return repo, plan


def _call(repo: Path, plan: Path, **extra) -> dict:
    params = {
        "verb": "premise-record",
        "plan_path": str(plan),
        "id": "C1",
        "verdict": "no",
        "ref": "state/audits/x.md",
    }
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def test_record_round_trips_through_reader_and_leaves_plan(tmp_path):
    repo, plan = _plan(tmp_path)
    before = plan.read_bytes()
    result = _call(repo, plan)
    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    got = read_premise_verdicts(plan)
    assert got["C1"]["verdict"] == "no"
    assert got["C1"]["ref"] == "state/audits/x.md"
    assert got["C1"]["recorded_at"]
    assert plan.read_bytes() == before


def test_yes_and_replace(tmp_path):
    repo, plan = _plan(tmp_path)
    assert _call(repo, plan, verdict="no")["exit_code"] == 0
    assert _call(repo, plan, verdict="Yes", ref="abc123")["exit_code"] == 0
    got = read_premise_verdicts(plan)["C1"]
    assert (got["verdict"], got["ref"]) == ("yes", "abc123")


@pytest.mark.parametrize("bad", ["maybe", "", None, True])
def test_bad_verdict_refused_without_write(tmp_path, bad):
    repo, plan = _plan(tmp_path)
    result = _call(repo, plan, verdict=bad)
    assert result["exit_code"] == 1
    assert "verdict" in result["error"]
    assert not evidence_sidecar_path(plan).exists()


def test_unknown_id_and_missing_ref_refused(tmp_path):
    repo, plan = _plan(tmp_path)
    assert "not found" in _call(repo, plan, id="ZZ")["error"]
    assert "ref" in _call(repo, plan, ref="  ")["error"]
    assert not evidence_sidecar_path(plan).exists()


def test_preserves_other_sidecar_keys_and_bad_yaml_aborts(tmp_path):
    repo, plan = _plan(tmp_path)
    sidecar = evidence_sidecar_path(plan)
    sidecar.write_text("rows:\n  C1:\n  - {recorded_at: t, text: hi}\n", encoding="utf-8")
    assert _call(repo, plan)["exit_code"] == 0
    loaded = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    assert loaded["rows"]["C1"][0]["text"] == "hi"
    assert loaded["premises"]["C1"]["verdict"] == "no"

    sidecar.write_text("a: [unclosed\n", encoding="utf-8")
    result = _call(repo, plan)
    assert result["exit_code"] == 1
    assert "not valid YAML" in result["error"]
