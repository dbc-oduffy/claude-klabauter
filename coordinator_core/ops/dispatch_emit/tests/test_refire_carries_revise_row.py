"""A `workflow.revise` register row on files outside the run's original paths is executed on the refire:
the ledger-resume stage the re-plan leg calls carries that row's text in the serving row's brief and
widens the manifest's review_declared_paths to the row's files."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import in_run_widen
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_stage import _handler, stage
from coordinator_core.ops.dispatch_emit.ask_stage_blocks import SERVED_ROWS_HEADING
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

pytestmark = [pytest.mark.cadence]

RUN = "ask-20261114T000000-aaaaaa"
SIZING_REL = "state/sizings/2026-11/2026-11-14-revise-row.yaml"
PLAN_REL = "docs/plans/2026-11-14-revise-row.md"
R2_TEXT = "R2-MARK add the groom slot check to pkg/b.py and pkg/c.py"

PLAN = f"""---
deliverable_id: dlv-reviserow-abc123
sizing_object: {SIZING_REL}
---
# Revise row

## Problem

Row C1 landed; the revise added R2 on new files and the re-plan added C2 for it.

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
  register_rows: ["R1"]
  disposition: open
- id: C2
  title: serves R2
  change_kind: code-edit
  surface: pkg/b.py
  writes:
    - pkg/b.py
    - pkg/c.py
  register_rows: ["R2"]
  disposition: open
```
"""


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "2026-11").mkdir(parents=True)
    doc = {
        "schema": "sizing-object",
        "name": "revise row fixture",
        "intent": "Fix pkg/a.py",
        "estimate": {"tshirt": "S", "provisional": True},
        "route": "plan",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 0, "text": "fix pkg/a.py"}],
        "exit_criterion": {"statement": "it works", "accepted": None},
        "deliverable_id": "dlv-reviserow-abc123",
        "plan": PLAN_REL,
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    (tmp_path / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    write_run_scripts(tmp_path, RUN)
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, SIZING_REL)
    rl.revise(tmp_path, RUN, verbatim=R2_TEXT, sender="peer-session-1")
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C1"], ["C2"], reasons={"C1": "landed"})
    return tmp_path


def _replan_params() -> dict:
    return {
        "run_id": RUN,
        "plan_path": PLAN_REL,
        "ledger_resume": True,
        "claim_register_rows": True,
        "only_rows": ["C2"],
        "commit_sizing_path": SIZING_REL,
    }


def _stage_via_function(root):
    return stage(
        root,
        run_id=RUN,
        plan_rel=PLAN_REL,
        commit_sizing_rel=SIZING_REL,
        only_rows=["C2"],
        ledger_resume=True,
        claim_register_rows=True,
    )


def _stage_via_op(root):
    reply = read_stage_reply(root, _handler(_replan_params(), repo_root=root))
    assert "error" not in reply, reply
    return reply


def test_the_register_row_is_in_the_ledger_before_the_refire(run):
    ledger = rl.read_ledger(run, rl.run_dir(run, RUN))
    assert "R2" in [i.id for i in ledger.items]


def test_stage_function_declares_the_revise_rows_files(run):
    manifest = _stage_via_function(run)
    declared = set(manifest.review_declared_paths)
    assert {"pkg/b.py", "pkg/c.py"} <= declared, f"review_declared_paths lacks R2's files: {sorted(declared)}"


def test_stage_function_brief_carries_the_revise_row_text(run):
    manifest = _stage_via_function(run)
    row = next(r for r in manifest.rows if r.id == "C2")
    brief = (run / row.brief_path).read_text(encoding="utf-8")
    assert R2_TEXT in brief, "the brief for C2 lacks R2's register text"


def test_the_op_the_replan_leg_calls_declares_the_revise_rows_files(run):
    reply = _stage_via_op(run)
    declared = set(reply["review_declared_paths"])
    assert {"pkg/b.py", "pkg/c.py"} <= declared, f"review_declared_paths lacks R2's files: {sorted(declared)}"


def test_the_op_the_replan_leg_calls_briefs_the_revise_row(run):
    reply = _stage_via_op(run)
    row = next(r for r in reply["rows"] if r["id"] == "C2")
    brief = (run / row["brief_path"]).read_text(encoding="utf-8")
    assert R2_TEXT in brief, "the brief for C2 lacks R2's register text"


def test_a_landed_row_stays_out_of_the_declared_paths(run):
    assert "pkg/a.py" not in set(_stage_via_function(run).review_declared_paths)


def test_the_brief_leads_with_the_served_rows_section_naming_r2(run):
    manifest = _stage_via_function(run)
    row = next(r for r in manifest.rows if r.id == "C2")
    brief = (run / row.brief_path).read_text(encoding="utf-8")
    assert SERVED_ROWS_HEADING in brief
    assert "R2:" in brief.split(SERVED_ROWS_HEADING, 1)[1]


def test_the_served_row_files_are_recorded_as_widens(run):
    _stage_via_function(run)
    widened = in_run_widen.widened_by_row(run / "scratch" / "warp" / RUN)
    assert {"pkg/b.py", "pkg/c.py"} <= set(widened.get("C2", ()))
    assert "C1" not in widened


def test_a_fresh_stage_without_ledger_resume_gets_no_served_section_and_no_widen(run):
    manifest = stage(
        run, run_id=RUN, plan_rel=PLAN_REL, commit_sizing_rel=SIZING_REL, only_rows=["C2"], claim_register_rows=True
    )
    row = next(r for r in manifest.rows if r.id == "C2")
    brief = (run / row.brief_path).read_text(encoding="utf-8")
    assert SERVED_ROWS_HEADING not in brief
    assert in_run_widen.widened_by_row(run / "scratch" / "warp" / RUN) == {}
