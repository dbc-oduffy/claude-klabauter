"""A `workflow.revise` `add_paths` entry naming a row (`{row, path}`) reaches that row's brief on the
refire: the handler stamps the path into the plan row's `writes`, and `ask_stage.stage` (the default
refire stager) writes a brief that names it, while a row the revise does not name keeps its scope."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

RUN = "ask-20261103T000000-abc123"
REL = "state/sizings/2026-11-03-row-widen.yaml"
PLAN_REL = "docs/plans/2026-11-03-row-widen.md"
OLD = "src/lib/route-registrars.ts"
NEW = "src/app/api/mcp/route.ts"
OTHER = "src/lib/other.ts"

PLAN = f"""---
deliverable_id: dlv-rowwiden-abc123
---
# Row widen

## Problem

Two rows.

## Tasks

```yaml plan-tasks
- id: C8
  title: registrar
  change_kind: code-edit
  surface: {OLD}
  writes:
    - {OLD}
- id: C9
  title: other
  change_kind: code-edit
  surface: {OTHER}
  writes:
    - {OTHER}
```
"""


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}], "plan": PLAN_REL,
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    write_run_scripts(tmp_path, RUN)
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    (tmp_path / RECORD_DIR_ROOT / RUN).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _revise(root, add_paths):
    return rl._handler({"run_id": RUN, "add_paths": add_paths}, repo_root=root)


def _briefs(root) -> dict:
    manifest = stage(root, run_id=RUN, plan_rel=PLAN_REL)
    return {r.id: (root / r.brief_path).read_text(encoding="utf-8") for r in manifest.rows}


def test_row_named_add_path_reaches_that_rows_refired_brief(run):
    assert NEW not in _briefs(run)["C8"]

    reply = _revise(run, [{"row": "C8", "path": NEW}])
    assert reply.get("ok") is True, reply
    assert "footprint_widened" not in reply

    briefs = _briefs(run)
    assert f"`{NEW}`" in briefs["C8"] and f"`{OLD}`" in briefs["C8"]
    assert NEW not in briefs["C9"]
    assert "Cover " not in (run / PLAN_REL).read_text(encoding="utf-8")


def test_row_unknown_to_the_plan_falls_back_to_a_footprint_row(run):
    reply = _revise(run, [{"row": "C99", "path": NEW}])
    assert reply.get("ok") is True, reply
    assert reply["footprint_widened"][0]["paths"] == [NEW]
    assert NEW not in _briefs(run)["C8"]


def test_add_path_without_a_row_widens_no_row_brief(run):
    reply = _revise(run, [NEW])
    assert reply.get("ok") is True, reply
    assert reply["footprint_widened"][0]["paths"] == [NEW]
    briefs = _briefs(run)
    assert NEW not in briefs["C8"] and NEW not in briefs["C9"]


def test_refused_revise_leaves_the_plan_unmutated(run, monkeypatch):
    from coordinator_core.ops import plan_tasks_mutate

    monkeypatch.setattr(plan_tasks_mutate, "_add_task", lambda *a, **k: {"applied": False})
    before = (run / PLAN_REL).read_text(encoding="utf-8")
    reply = _revise(run, [{"row": "C8", "path": NEW}, "src/lib/plain.ts"])
    assert reply.get("ok") is not True, reply
    assert (run / PLAN_REL).read_text(encoding="utf-8") == before


def test_refused_fallback_leaves_an_earlier_stampable_row_unmutated(run, monkeypatch):
    from coordinator_core.ops import plan_tasks_mutate

    monkeypatch.setattr(plan_tasks_mutate, "_add_task", lambda *a, **k: {"applied": False})
    before = (run / PLAN_REL).read_text(encoding="utf-8")
    reply = _revise(run, [{"row": "C8", "path": NEW}, {"row": "C99", "path": "src/lib/ghost.ts"}])
    assert reply.get("ok") is not True, reply
    assert (run / PLAN_REL).read_text(encoding="utf-8") == before


def test_row_path_already_in_the_rows_writes_is_a_noop(run):
    before = (run / PLAN_REL).read_text(encoding="utf-8")
    reply = _revise(run, [{"row": "C8", "path": OLD}])
    assert reply.get("ok") is True, reply
    assert (run / PLAN_REL).read_text(encoding="utf-8") == before


def test_repeated_revise_does_not_duplicate_the_path(run):
    _revise(run, [{"row": "C8", "path": NEW}])
    _revise(run, [{"row": "C8", "path": NEW}])
    rows = yaml.safe_load((run / PLAN_REL).read_text(encoding="utf-8").split("```yaml plan-tasks\n")[1].split("```")[0])
    assert next(r for r in rows if r["id"] == "C8")["writes"] == [OLD, NEW]
