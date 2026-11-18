"""A refire after a stage-refused run (no halt marker, no manifest, unreviewed plan) names the plan's open rows."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.sizing_fire import _open_plan_row_ids
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

REFUSED = "ask-20261117T090623-d8b354"
REFIRE = "warp-2026-11-17-refire-20261117T093540-d8cb8a"


def _refused_predecessor(repo, rows) -> None:  # noqa: F811
    (repo / ".git").mkdir()
    _plan(repo, rows=rows)
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL)
    rl.start(repo, REFUSED)
    (repo / RECORD_DIR_ROOT / REFUSED / "run.json").write_text(json.dumps({"sizing_rel": REL}), encoding="utf-8")


def test_refire_of_a_stage_refused_run_resumes_with_its_open_rows_named(repo) -> None:  # noqa: F811
    _refused_predecessor(repo, [("C1", "open")])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE}, repo)

    assert out.get("halt") is None and out["resume_plan"] == PLAN_REL, out
    assert out["resume_rows"] == ["C1"], out


def test_refire_of_a_stage_refused_run_leaves_closed_rows_out(repo) -> None:  # noqa: F811
    _refused_predecessor(repo, [("C1", "open"), ("C2", "delivered_elsewhere")])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE}, repo)

    assert out["resume_rows"] == ["C1"], out


def test_an_unreadable_spine_names_no_rows(tmp_path) -> None:
    plan = tmp_path / "plan.md"
    assert _open_plan_row_ids(plan) == ()
    plan.write_text("no spine here\n", encoding="utf-8")
    assert _open_plan_row_ids(plan) == ()
