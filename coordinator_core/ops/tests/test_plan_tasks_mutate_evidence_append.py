"""Tests for the plan.tasks.mutate ``evidence-append`` verb.

Pins: evidence lands in the ``<stem>.evidence.yaml`` sidecar, the plan body bytes and
``approved_body_sha`` are untouched, entries round-trip and accumulate through
``read_row_evidence``, and bad input refuses with no sidecar written.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import check_approved_body, stamp_approved_body_sha
from coordinator_core.ops.plan_tasks_mutate import (
    _handler,
    evidence_sidecar_path,
    read_row_evidence,
)
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Evidence Plan"
status: ready
---

# Evidence Plan

## Tasks

```yaml plan-tasks
- id: C12
  title: Verify the thing
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Verify it.
```

## Trailer
"""


def _call(repo: Path, plan: Path, **extra) -> dict:
    params = {"verb": "evidence-append", "plan_path": str(plan), "id": "C12",
              "text": "verified: 14 passed"}
    params.update(extra)
    return asyncio.run(_handler(params, repo_root=repo / ".git"))


def _stamped_plan(tmp_path: Path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "e.md", "")
    plan.write_text(stamp_approved_body_sha(_PLAN), encoding="utf-8", newline="")
    return repo, plan


def test_append_leaves_plan_bytes_and_approved_body_sha_unchanged(tmp_path):
    repo, plan = _stamped_plan(tmp_path)
    before = plan.read_bytes()

    result = _call(repo, plan)

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert plan.read_bytes() == before
    state, _msg = check_approved_body(plan.read_text(encoding="utf-8"))
    assert state == "ok"
    assert evidence_sidecar_path(plan).is_file()


def test_sidecar_round_trips_and_accumulates(tmp_path):
    repo, plan = _stamped_plan(tmp_path)

    assert read_row_evidence(plan, "C12") == []
    _call(repo, plan, text="first: green")
    _call(repo, plan, text="second:\n  multi-line: yes # not a comment\n")

    entries = read_row_evidence(plan, "C12")
    assert [e["text"] for e in entries] == ["first: green", "second:\n  multi-line: yes # not a comment\n"]
    assert all(e["recorded_at"].endswith("Z") for e in entries)
    assert list(read_row_evidence(plan)) == ["C12"]


def test_unknown_row_and_empty_text_refuse_without_sidecar(tmp_path):
    repo, plan = _stamped_plan(tmp_path)

    assert _call(repo, plan, id="C99")["exit_code"] == 1
    for text in ("", "   ", None):
        assert _call(repo, plan, text=text)["exit_code"] == 1
    assert not evidence_sidecar_path(plan).exists()


def test_missing_plan_refuses(tmp_path):
    repo = _make_git_repo(tmp_path)
    result = _call(repo, repo / "docs" / "plans" / "nope.md")
    assert result["exit_code"] == 1
    assert "plan not found" in result["error"]
