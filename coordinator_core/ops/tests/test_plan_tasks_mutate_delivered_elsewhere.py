"""plan.tasks.mutate resolve: the delivered_elsewhere exit closes a row that --coded and --spun-off refuse."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = f"""\
---
title: "Delivered Elsewhere Plan"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  spun_off:
    status: pending
    approver: pm
    approved_at: 2026-08-30
    pm_utterance: 'not yet'
    digest: 'sha256:{"0" * 64}'
---

# Delivered Elsewhere Plan

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
  body: |
    First.
```
"""

_REF = "other-repo@abc1234def"
_DETAIL = "landed as another repo's commit via direct message"


def _call(repo, plan, **params):
    return asyncio.run(_handler(
        {"verb": "resolve", "plan_path": str(plan), "id": "C1", **params},
        repo_root=repo / ".git",
    ))


def _fixture(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "de.md", _PLAN)
    (repo / "b.py").write_text("x = 1\n", encoding="utf-8")
    flags = no_console_creationflags()
    subprocess.run(["git", "add", "b.py"], cwd=repo, check=True, capture_output=True, **flags)
    subprocess.run(["git", "commit", "-m", "b"], cwd=repo, check=True, capture_output=True, **flags)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, **flags
    ).stdout.strip()
    _seed_plan(repo, "spinoff.md", "# Spinoff\n")
    return repo, plan, sha


def _delivered(repo, plan, **overrides):
    params = {
        "disposition": "delivered_elsewhere",
        "disposition_ref": _REF,
        "disposition_detail": _DETAIL,
        "delivered_via": "direct-message",
    }
    params.update(overrides)
    return _call(repo, plan, **params)


def test_coded_refusal_is_replaced_by_delivered_elsewhere(tmp_path):
    repo, plan, sha = _fixture(tmp_path)
    original = plan.read_bytes()

    refused = _call(repo, plan, disposition="coded", disposition_ref=sha, disposition_detail="x")
    assert refused["exit_code"] != 0
    assert "touches none of its writes" in refused["error"]
    assert plan.read_bytes() == original

    closed = _delivered(repo, plan)
    assert closed["exit_code"] == 0, closed
    assert closed["applied"] is True
    row = load_rows(plan.read_text(encoding="utf-8")).rows[0]
    assert row["disposition"] == "delivered_elsewhere"
    assert row["delivered_via"] == "direct-message"
    assert row["disposition_ref"] == _REF


def test_spun_off_refusal_is_replaced_by_delivered_elsewhere(tmp_path):
    repo, plan, _sha = _fixture(tmp_path)
    original = plan.read_bytes()

    refused = _call(
        repo, plan, disposition="spun_off",
        disposition_ref="docs/plans/spinoff.md", disposition_detail="moved",
    )
    assert refused["exit_code"] != 0
    assert "spun_off" in refused["error"]
    assert plan.read_bytes() == original

    closed = _delivered(repo, plan, delivered_via="memo")
    assert closed["exit_code"] == 0, closed
    assert load_rows(plan.read_text(encoding="utf-8")).rows[0]["delivered_via"] == "memo"


def test_close_warns_that_completion_is_asserted_not_proven(tmp_path):
    repo, plan, _sha = _fixture(tmp_path)

    closed = _delivered(repo, plan)

    assert closed["exit_code"] == 0, closed
    assert f"completion asserted, not commit-proven: {_REF} via direct-message" in closed["warnings"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"disposition_ref": "  "},
        {"disposition_detail": " "},
        {"delivered_via": None},
        {"delivered_via": "carrier-pigeon"},
    ],
)
def test_blank_ref_detail_or_bad_via_is_refused_and_writes_nothing(tmp_path, overrides):
    repo, plan, _sha = _fixture(tmp_path)
    original = plan.read_bytes()

    result = _delivered(repo, plan, **overrides)

    assert result["exit_code"] != 0, result
    assert "delivered_elsewhere requires" in result["error"] or "delivered_via must be" in result["error"]
    assert plan.read_bytes() == original
