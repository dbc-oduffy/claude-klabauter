"""A halt's `widened [{row, path}]` fed back through `workflow.revise` stamps the path into that plan row's
`writes`, so the refire's staged brief and run footprint cover it; a row the revise does not name keeps
its scope, and a denied or malformed item changes no plan."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import halted_run, in_run_widen
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

RUN = "ask-20261104T000000-abc123"
REL = "state/sizings/2026-11-04-widen-refire.yaml"
PLAN_REL = "docs/plans/2026-11-04-widen-refire.md"
OLD = "pkg/lr1.py"
NEW = "pkg/index_write.py"
OTHER = "pkg/other.py"

PLAN = f"""---
deliverable_id: dlv-widenrefire-abc123
---
# Widen refire

## Problem

Two rows.

## Tasks

```yaml plan-tasks
- id: LR1
  title: leg one
  change_kind: code-edit
  surface: {OLD}
  writes:
    - {OLD}
- id: LR2
  title: leg two
  change_kind: code-edit
  surface: {OTHER}
  writes:
    - {OTHER}
```
"""


@pytest.fixture
def halted(tmp_path, monkeypatch):
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda *a, **k: None)
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
    record_dir = tmp_path / RECORD_DIR_ROOT / RUN
    record_dir.mkdir(parents=True, exist_ok=True)
    halted_run.write_halt_marker(
        record_dir, halted="dependents_unstarted", sizing_rel=REL, plan_rel=PLAN_REL,
        reason=f"LR1 hit gate-blocker: outside-footprint: {NEW}",
    )
    return tmp_path


def _revise(root, widened):
    return rl._handler({"run_id": RUN, "widened": widened}, repo_root=root)


def _rows(root) -> dict:
    body = (root / PLAN_REL).read_text(encoding="utf-8").split("```yaml plan-tasks\n")[1].split("```")[0]
    return {r["id"]: r for r in yaml.safe_load(body)}


def _briefs(root) -> dict:
    manifest = stage(root, run_id=RUN, plan_rel=PLAN_REL)
    return {r.id: (root / r.brief_path).read_text(encoding="utf-8") for r in manifest.rows}


def test_halt_then_revise_widened_then_refire_footprint_includes_the_path(halted):
    assert NEW not in _briefs(halted)["LR1"]

    reply = _revise(halted, [{"row": "LR1", "path": NEW}])
    assert reply.get("ok") is True, reply
    assert reply["widened"] == 1

    assert _rows(halted)["LR1"]["writes"] == [OLD, NEW]
    assert _rows(halted)["LR2"]["writes"] == [OTHER]
    briefs = _briefs(halted)
    assert f"`{NEW}`" in briefs["LR1"] and f"`{OLD}`" in briefs["LR1"]
    assert NEW not in briefs["LR2"]
    dir_ = halted / RECORD_DIR_ROOT / RUN
    assert in_run_widen.widened_by_row(dir_) == {"LR1": [NEW]}


def test_repeated_revise_does_not_duplicate_the_path(halted):
    _revise(halted, [{"row": "LR1", "path": NEW}])
    _revise(halted, [{"row": "LR1", "path": NEW}])
    assert _rows(halted)["LR1"]["writes"] == [OLD, NEW]


def test_denied_path_stamps_nothing(halted):
    before = (halted / PLAN_REL).read_text(encoding="utf-8")
    _revise(halted, [{"row": "LR1", "path": "state/x.yaml"}])
    assert (halted / PLAN_REL).read_text(encoding="utf-8") == before


def test_row_unknown_to_the_plan_falls_back_to_a_footprint_row(halted):
    reply = _revise(halted, [{"row": "LR99", "path": NEW}])
    assert reply.get("ok") is True, reply
    assert reply["footprint_widened"][0]["paths"] == [NEW]
    assert NEW not in _briefs(halted)["LR1"]
