"""plan.tasks.mutate never writes a spine row carrying a boolean mapping key.

An unquoted `on:` key in a nested mapping must survive a later verb as the string
key, and an add-task whose authorization carries `on` must land with the string key.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import (
    _make_git_repo,
    _seed_plan,
    _valid_task,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = """\
---
title: "Bool Keys Plan"
status: draft
---

# Bool Keys Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  authorization:
    source: em-inferred
    on: 2026-10-14
    rationale: Follows from the goal.
  body: |
    First.
```
"""


def _spine_rows(plan) -> list:
    text = plan.read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)


def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


def _call(repo, payload):
    return asyncio.run(_handler(payload, repo_root=repo / ".git"))


def test_add_task_keeps_on_key_in_authorization_as_string(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "bk-add.md", _PLAN)
    task = _valid_task("C2")
    task["authorization"] = {
        "source": "em-inferred", "on": "2026-10-23", "rationale": "Follows from the goal.",
    }

    result = _call(repo, {"verb": "add-task", "plan_path": str(plan), "task": task})

    assert result["exit_code"] == 0, result
    rows = {r["id"]: r for r in _spine_rows(plan)}
    assert [k for k in _all_keys(list(rows.values())) if isinstance(k, bool)] == []
    assert rows["C2"]["authorization"]["on"] == "2026-10-23"


def test_preseeded_unquoted_on_key_is_not_rewritten_to_true(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "bk-seed.md", _PLAN)

    result = _call(repo, {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C2")})

    assert result["exit_code"] == 0, result
    rows = {r["id"]: r for r in _spine_rows(plan)}
    assert [k for k in _all_keys(list(rows.values())) if isinstance(k, bool)] == []
    assert "on" in rows["C1"]["authorization"]
    assert rows["C1"]["authorization"]["source"] == "em-inferred"


def test_stamp_over_preseeded_on_key_keeps_string_key(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "bk-stamp.md", _PLAN)

    result = _call(repo, {
        "verb": "stamp", "plan_path": str(plan),
        "updates": [{"id": "C1", "title": "Renamed chunk"}],
    })

    assert result["exit_code"] == 0, result
    rows = _spine_rows(plan)
    assert [k for k in _all_keys(rows) if isinstance(k, bool)] == []
    assert "on" in rows[0]["authorization"]


def test_add_task_with_in_process_boolean_key_is_refused_naming_key(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "bk-refuse.md", _PLAN)
    before = plan.read_text(encoding="utf-8")
    task = _valid_task("C2")
    task["authorization"] = {
        "source": "em-inferred", True: "2026-10-23", "rationale": "Follows from the goal.",
    }

    result = _call(repo, {"verb": "add-task", "plan_path": str(plan), "task": task})

    assert result["exit_code"] != 0, result
    message = str(result)
    assert "authorization: key True is not a string" in message
    assert plan.read_text(encoding="utf-8") == before


def test_patch_row_chunk_keeps_on_key_as_string():
    from coordinator_core.ops.plan_tasks_mutate import _StringKeyLoader, _patch_row_chunk

    chunk = (
        "- id: C1\n"
        "  title: First\n"
        "  authorization:\n"
        "    source: em-inferred\n"
        "    on: 2026-10-14\n"
        "    rationale: r\n"
    )
    orig = yaml.load(chunk, Loader=_StringKeyLoader)[0]
    row = {**orig, "title": "Renamed"}

    patched = _patch_row_chunk(chunk, orig, row)

    assert patched is not None
    assert "    on: 2026-10-14\n" in patched
    loaded = yaml.load(patched, Loader=_StringKeyLoader)[0]
    assert "on" in loaded["authorization"] and True not in loaded["authorization"]
