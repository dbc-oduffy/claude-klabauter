"""A workflow.revise order_rows reaches the refire: dispatch.ask_stage schedules the ordered row
first and the manifest reports applied_order / unhonoured_order."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_order
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-02-p-6fcf46-20261102T200256-bf0de2"
SIZING_REL = "state/sizings/s.yaml"
PLAN_REL = "docs/plans/2026-11-02-p.md"

_PLAN = """---
title: p
created: 2026-11-02
deliverable_id: dlv-order-abc123
sizing_object: state/sizings/s.yaml
grouping_approvals:
  do:
    status: pending
  spun_off:
    status: pending
  defer:
    status: pending
  ruled_out:
    status: pending
---

# p

## Problem

C1 and C3 write the same file, so the default order runs C1 in wave 0 and C3 after it.

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C2
  title: second
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
- id: C3
  title: third
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: C4
  title: fourth, landed
  change_kind: code-edit
  surface: pkg/d.py
  disposition: done
  writes:
    - pkg/d.py
```
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / PLAN_REL).write_text(_PLAN, encoding="utf-8", newline="\n")
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "name": "order fixture", "intent": "exercise order_rows", "plan": PLAN_REL,
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-order-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    write_run_scripts(tmp_path, RUN)
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    return tmp_path


def _wave_of(m: StageManifest) -> dict[str, int]:
    return {r.id: r.wave for r in m.rows}


def test_default_order_runs_c1_first_and_reports_no_order(repo):
    m = stage(repo, run_id=RUN, plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)
    waves = _wave_of(m)
    assert waves["C1"] < waves["C3"]
    assert m.applied_order == () and m.unhonoured_order == ()


def test_a_revised_order_places_c3_first_and_the_manifest_reports_it(repo):
    reply = rl._handler({"run_id": RUN, "order_rows": ["C3"]}, repo_root=repo)
    assert reply["ok"] is True and "order_rows" in reply["applied"]
    m = stage(repo, run_id=RUN, plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)
    waves = _wave_of(m)
    assert waves["C3"] < waves["C1"]
    assert m.applied_order == ("C3",)
    assert m.unhonoured_order == ()


def test_a_landed_ordered_id_lands_in_unhonoured_order(repo):
    run_order.record(rl.run_dir(repo, RUN), ["C3", "C4"], "2026-11-02T20:00:00Z")
    m = stage(repo, run_id=RUN, plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)
    assert _wave_of(m)["C3"] < _wave_of(m)["C1"]
    assert m.applied_order == ("C3",)
    assert [r["id"] for r in m.unhonoured_order] == ["C4"]
    assert m.unhonoured_order[0]["reason"]


def test_the_manifest_round_trips_the_order_fields(repo):
    rl._handler({"run_id": RUN, "order_rows": ["C3"]}, repo_root=repo)
    m = stage(repo, run_id=RUN, plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL)
    import json

    on_disk = json.loads((repo / RECORD_DIR_ROOT / RUN / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk["applied_order"] == ["C3"]
    assert StageManifest.from_json(on_disk) == m
