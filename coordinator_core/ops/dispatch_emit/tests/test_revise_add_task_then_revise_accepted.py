"""`workflow.revise` accepts a note naming a path no manifest row covers by widening the footprint through an
engine-written plan row; a plan row that already writes the path needs no widening, and a run with no readable
plan refuses the note naming the `plan.tasks.mutate add-task` call. The accepted note reaches the brief block."""

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


def test_a_note_naming_an_uncovered_path_widens_the_footprint_through_an_engine_written_row(repo):
    note = f"add a constant to {ADDED}"
    before = _call(repo, note=note)
    assert "note" in before["applied"] and "footprint_widened" in before["applied"] and before["ok"] is True
    assert before["refused"] == []
    (widened,) = before["footprint_widened"]
    assert widened["paths"] == [ADDED]
    plan = (repo / PLAN_REL).read_text(encoding="utf-8")
    assert "Engine-written by workflow.revise" in plan and f"covers {ADDED}" in plan

    brief = run_notes.brief_block(rl.carried_notes(repo, RUN))
    assert ADDED in brief


def test_add_task_row_covering_the_path_needs_no_widening(repo):
    _add_task(repo)
    after = _call(repo, note=f"add a constant to {ADDED}")
    assert "note" in after["applied"] and after["ok"] is True
    assert "footprint_widened" not in after["applied"] and "footprint_widened" not in after


def test_a_run_with_no_readable_plan_refuses_the_note_naming_the_add_task_call(repo):
    (repo / PLAN_REL).unlink()
    reply = _call(repo, note=f"add a constant to {ADDED}")
    assert "note" not in reply["applied"] and reply["ok"] is False
    (entry,) = [r for r in reply["refused"] if r.get("input") == "note"]
    assert "add-task" in entry["reason"] and "sizing `writes`" not in entry["reason"]
    assert entry["paths"] == [ADDED]


def test_plan_row_writes_under_widens_the_footprint(repo):
    _add_task(repo)
    files, dirs = rl.run_footprint(repo, rl.run_dir(repo, RUN), SIZING_REL)
    assert {INSIDE, ADDED} <= files and "gen/out" in dirs
    assert "note" in _call(repo, note="fix gen/out/g.py")["applied"]


def test_a_path_no_plan_row_covers_is_widened_by_its_own_engine_row_after_add_task(repo):
    _add_task(repo)
    reply = _call(repo, note=f"add a field to {UNCOVERED}")
    assert "note" in reply["applied"] and "footprint_widened" in reply["applied"]
    (widened,) = reply["footprint_widened"]
    assert widened["paths"] == [UNCOVERED]
    assert reply["refused"] == []


def test_run_without_a_readable_plan_keeps_the_manifest_footprint(repo):
    (repo / PLAN_REL).unlink()
    files, _ = rl.run_footprint(repo, rl.run_dir(repo, RUN), SIZING_REL)
    assert files == {INSIDE}
