"""`workflow.revise` finds the run's plan when the sizing's `plan:` edge is stale (wrong month folder), so a
`plan.tasks.mutate add-task` row on the real plan covers a note's path; a refusal names the plan it read."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-11-06-x-75f803-20261106T101437-1e48f4"
SIZING_REL = "state/sizings/2026-11/2026-11-06-x.yaml"
STALE_EDGE = "docs/plans/2026-11/2026-11-06-p.md"
REAL_PLAN = "docs/plans/2026-11-06-p.md"
INSIDE = "pkg/inside.py"
ADDED = "pkg/added.py"

_PLAN = """---
title: p
sizing_object: "state/sizings/2026-11/2026-11-06-x.yaml"
---

# p

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  writes:
    - pkg/inside.py
{extra}```
"""

_C7 = """- id: C7
  title: added
  writes:
    - pkg/added.py
"""


def _make(tmp_path, *, edge=STALE_EDGE, plan_text=None):
    (tmp_path / ".git").mkdir()
    for rel in (INSIDE, ADDED):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / REAL_PLAN).write_text(plan_text or _PLAN.format(extra=""), encoding="utf-8")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(
        yaml.safe_dump({"schema": "sizing-object", "intent": "x", "plan": edge}), encoding="utf-8"
    )
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    manifest = {"doc_writes": [], "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _call(repo, **params):
    return rl._handler({"run_id": RUN, **params}, repo)


def test_stale_edge_resolves_to_the_plan_that_names_the_sizing(tmp_path):
    repo = _make(tmp_path)
    assert rl._sizing_plan(repo, SIZING_REL)[0] == REAL_PLAN


def test_add_task_row_on_the_real_plan_covers_the_note_despite_a_stale_edge(tmp_path):
    repo = _make(tmp_path, plan_text=_PLAN.format(extra=_C7))
    reply = _call(repo, note=f"add a constant to {ADDED}")
    assert reply["ok"] is True and "note" in reply["applied"]
    assert "footprint_widened" not in reply["applied"] and reply["refused"] == []


def test_stale_edge_note_is_widened_through_the_real_plan(tmp_path):
    repo = _make(tmp_path)
    reply = _call(repo, note=f"add a constant to {ADDED}")
    assert "note" in reply["applied"] and "footprint_widened" in reply["applied"]
    assert "Engine-written by workflow.revise" in (repo / REAL_PLAN).read_text(encoding="utf-8")


def test_a_plan_that_does_not_name_the_sizing_is_not_adopted(tmp_path):
    repo = _make(tmp_path, plan_text=_PLAN.format(extra=_C7).replace("2026-11-06-x.yaml", "other.yaml"))
    assert rl._sizing_plan(repo, SIZING_REL)[0] is None
    reply = _call(repo, note=f"add a constant to {ADDED}")
    assert reply["ok"] is False and "note" not in reply["applied"]


def test_refusal_reports_that_it_read_no_plan(tmp_path):
    repo = _make(tmp_path)
    (repo / REAL_PLAN).unlink()
    reply = _call(repo, note=f"add a constant to {ADDED}")
    (entry,) = [r for r in reply["refused"] if r.get("input") == "note"]
    assert "it read no plan" in entry["reason"]


def test_refusal_names_the_plan_file_it_read_when_the_row_is_refused(tmp_path, monkeypatch):
    repo = _make(tmp_path)
    from coordinator_core.ops import plan_tasks_mutate

    monkeypatch.setattr(plan_tasks_mutate, "_add_task", lambda *a, **k: {"applied": False})
    reply = _call(repo, note=f"add a constant to {ADDED}")
    (entry,) = [r for r in reply["refused"] if r.get("input") == "note"]
    assert f"the plan it read is {REAL_PLAN}" in entry["reason"]
