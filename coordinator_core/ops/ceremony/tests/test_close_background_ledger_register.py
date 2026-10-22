"""The sizing-ship leg settles ledger-met register rows onto the sizing, even on an incomplete run."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.tests.test_register_gate import _PLAN, _SIZING, _wb_row, _wb_sizing_text
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _init_repo

RUN = "run-ledger-1"
PLAN = _PLAN
SIZING = _SIZING
SHA = "a" * 40
_SIZING_TEXT = _wb_sizing_text(
    [_wb_row("R1", claimed=False, anchor="relayed"), _wb_row("R2", claimed=False, anchor="relayed")]
)


def _run(tmp_path: Path, met: str | None) -> dict:
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN).write_text(
        f"---\nplan_id: p\nsizing_object: {SIZING}\nregister_claims:\n  sizing: {SIZING}\n  rows: [R1, R2]\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / SIZING).write_text(_SIZING_TEXT, encoding="utf-8")
    inputs = {"incomplete": ["C12"], "ledger_judged": ["R1", "R2"]}
    if met:
        inputs["ledger_met"] = met
    run_close.write_close_job(
        tmp_path, RUN, ("sizing-ship",), datetime.now().astimezone() + timedelta(minutes=10),
        plan_rel=PLAN, terminal_sha=SHA, criterion=None, inputs=inputs,
    )
    ctx = cb.JobContext(tmp_path, tmp_path / ".git", RUN, (), None, SHA, run_close._load(tmp_path, RUN))
    result = cb._sizing_ship(ctx)
    rows = {r["id"]: r for r in rr.read_register((tmp_path / SIZING).read_text(encoding="utf-8")).rows}
    return {"result": result, "rows": rows, "doc": yaml.safe_load((tmp_path / SIZING).read_text(encoding="utf-8"))}


def test_met_ledger_row_settles_met_with_its_claim_and_unmet_row_stays_open(tmp_path):
    out = _run(tmp_path, "ledger-met: R1")

    r1, r2 = out["rows"]["R1"], out["rows"]["R2"]
    assert (r1["status"], r1["wired"], r1["claimed_by"]) == ("met", True, [PLAN])
    assert r1["met_by"] == [f"{PLAN}@{SHA}"]
    assert r2["status"] == "open" and not r2.get("claimed_by")
    assert out["doc"]["requirement_register"]["rollup"]["met"] == 1
    assert out["result"].status == "ran"
    tally = rr.register_tally(rr.read_register((tmp_path / SIZING).read_text(encoding="utf-8")), ["R1"])
    assert rr.register_owed(tally) == {"unclaimed": 1}  # only the unmet R2 still owes work


def test_no_ledger_met_leaves_register_untouched(tmp_path):
    out = _run(tmp_path, None)

    assert out["rows"]["R1"]["status"] == "open"
    assert out["result"].status == "not-applicable"


def test_reclose_requeues_a_not_applicable_sizing_ship_only_with_plan_reconciliation():
    assert cb.owed_on_reclose({"sizing-ship": "not-applicable", "plan-reconciliation": "failed"}) == [
        "sizing-ship", "plan-reconciliation",
    ]
    assert cb.owed_on_reclose({"sizing-ship": "not-applicable", "plan-reconciliation": "ran"}) == []
