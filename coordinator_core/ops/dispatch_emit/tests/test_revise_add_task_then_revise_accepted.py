"""A `plan.tasks.mutate add-task` row widens what `workflow.revise` accepts: a note naming a path no manifest
row covers is refused, then accepted once the sizing's plan carries a task row writing it, and the accepted
note reaches the brief block. A path no plan row covers stays refused."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_notes

RUN = "warp-2026-10-30-x-75f803-20261030T101437-1e48f4"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-10-30-p.md"
INSIDE = "pkg/inside.py"
ADDED = "scripts/lib/added.py"
UNCOVERED = "pkg/uncovered.py"

_PLAN = """---
title: p
---

# p

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  writes:
    - pkg/inside.py
```
"""

_C4 = """- id: C4
  title: added
  writes:
    - scripts/lib/added.py
  writes_under:
    - gen\\out/
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    for rel in (INSIDE, ADDED, UNCOVERED):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "gen" / "out").mkdir(parents=True)
    (tmp_path / "gen" / "out" / "g.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(
        yaml.safe_dump({"schema": "sizing-object", "intent": "x", "plan": PLAN_REL}), encoding="utf-8"
    )
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    manifest = {"doc_writes": [], "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _call(repo, **params):
    return rl._handler({"run_id": RUN, **params}, repo)


def _add_task(repo):
    plan = repo / PLAN_REL
    plan.write_text(plan.read_text(encoding="utf-8").replace("```\n", _C4 + "```\n"), encoding="utf-8")


def test_add_task_then_revise_is_refused_before_and_accepted_after(repo):
    note = f"add a constant to {ADDED}"
    before = _call(repo, note=note)
    assert "note" not in before["applied"]
    (entry,) = [r for r in before["refused"] if r.get("input") == "note"]
    assert "add-task" in entry["reason"] and "sizing `writes`" not in entry["reason"]

    _add_task(repo)
    after = _call(repo, note=note)
    assert "note" in after["applied"] and after["ok"] is True

    brief = run_notes.brief_block(rl.carried_notes(repo, RUN))
    assert ADDED in brief


def test_plan_row_writes_under_widens_the_footprint(repo):
    _add_task(repo)
    files, dirs = rl.run_footprint(repo, rl.run_dir(repo, RUN), SIZING_REL)
    assert {INSIDE, ADDED} <= files and "gen/out" in dirs
    assert "note" in _call(repo, note="fix gen/out/g.py")["applied"]


def test_a_path_no_plan_row_covers_stays_refused_after_add_task(repo):
    _add_task(repo)
    reply = _call(repo, note=f"add a field to {UNCOVERED}")
    assert "note" not in reply["applied"]
    (entry,) = [r for r in reply["refused"] if r.get("input") == "note"]
    assert entry["paths"] == [UNCOVERED]


def test_run_without_a_readable_plan_keeps_the_manifest_footprint(repo):
    (repo / PLAN_REL).unlink()
    files, _ = rl.run_footprint(repo, rl.run_dir(repo, RUN), SIZING_REL)
    assert files == {INSIDE}
