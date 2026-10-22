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


def _xs_close(tmp_path: Path, extra_rows: tuple = ()) -> dict:
    """The T22 shape: a plan that cites a sizing but claims no register row, a complete run."""
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / PLAN).write_text(f"---\nplan_id: p\nsizing_object: {SIZING}\n---\nbody\n", encoding="utf-8")
    rows = [_wb_row("T22", claimed=False, anchor="relayed"), *extra_rows]
    (tmp_path / SIZING).write_text(_wb_sizing_text(rows), encoding="utf-8")
    run_close.write_close_job(
        tmp_path, RUN, ("sizing-ship",), datetime.now().astimezone() + timedelta(minutes=10),
        plan_rel=PLAN, terminal_sha=SHA, criterion=None,
        inputs={"ledger_met": "ledger-met: T22", "ledger_judged": ["T22"]},
    )
    ctx = cb.JobContext(tmp_path, tmp_path / ".git", RUN, (), None, SHA, run_close._load(tmp_path, RUN))
    result = cb._sizing_ship(ctx)
    text = (tmp_path / SIZING).read_text(encoding="utf-8")
    return {"result": result, "doc": yaml.safe_load(text), "rows": {r["id"]: r for r in rr.read_register(text).rows}}


def test_xs_close_with_unclaimed_ledger_met_row_writes_it_met_and_ships(tmp_path):
    out = _xs_close(tmp_path)

    assert out["rows"]["T22"]["status"] == "met"
    assert out["doc"]["requirement_register"]["rollup"]["open"] == 0
    assert out["result"].status == "ran", out["result"].detail
    assert out["doc"]["status"] == "shipped"


def test_refused_ship_records_failed_with_the_reason(tmp_path):
    out = _xs_close(tmp_path, extra_rows=(_wb_row("T23", claimed=False, anchor="relayed"),))

    assert out["rows"]["T22"]["status"] == "met"
    assert out["result"].status == "failed"
    assert "ship skipped" in out["result"].detail and "T23" in out["result"].detail


def test_reclose_requeues_a_not_applicable_sizing_ship_only_with_plan_reconciliation():
    assert cb.owed_on_reclose({"sizing-ship": "not-applicable", "plan-reconciliation": "failed"}) == [
        "sizing-ship", "plan-reconciliation",
    ]
    assert cb.owed_on_reclose({"sizing-ship": "not-applicable", "plan-reconciliation": "ran"}) == []


WARP_PLAN = "state/warp/warp-x-20261022T000000-abc123/plan.md"


def _warp_close(tmp_path: Path, sizing_text: str) -> dict:
    _init_repo(tmp_path)
    (tmp_path / "state" / "warp" / "warp-x-20261022T000000-abc123").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / WARP_PLAN).write_text(f"---\nplan_id: p\nsizing_object: {SIZING}\n---\nbody\n", encoding="utf-8")
    (tmp_path / SIZING).write_text(sizing_text, encoding="utf-8")
    run_close.write_close_job(
        tmp_path, RUN, ("sizing-ship",), datetime.now().astimezone() + timedelta(minutes=10),
        plan_rel=WARP_PLAN, terminal_sha=SHA, criterion=None,
        inputs={"ledger_met": "ledger-met: T22", "ledger_judged": ["T22"]},
    )
    ctx = cb.JobContext(tmp_path, tmp_path / ".git", RUN, (), None, SHA, run_close._load(tmp_path, RUN))
    result = cb._sizing_ship(ctx)
    text = (tmp_path / SIZING).read_text(encoding="utf-8")
    return {"result": result, "rows": {r["id"]: r for r in rr.read_register(text).rows}}


def test_warp_run_plan_claims_under_the_sizings_docs_plans_edge(tmp_path):
    text = _wb_sizing_text([_wb_row("T22", claimed=False, anchor="relayed")]).replace(
        "status: routed\n", f"status: routed\nplan: {PLAN}\n", 1
    )
    out = _warp_close(tmp_path, text)

    assert out["result"].status == "ran", out["result"].detail
    assert out["rows"]["T22"]["status"] == "met"
    assert out["rows"]["T22"]["claimed_by"] == [PLAN]


def test_warp_run_without_a_docs_plans_edge_writes_no_claim_and_still_lands(tmp_path):
    out = _warp_close(tmp_path, _wb_sizing_text([_wb_row("T22", claimed=False, anchor="relayed")]))

    assert out["result"].status == "ran", out["result"].detail
    assert out["rows"]["T22"]["status"] == "met"
    assert not out["rows"]["T22"].get("claimed_by")


def test_refusal_detail_keeps_field_value_and_reason_past_the_clip(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import register_gate

    reason = (
        "register write-back refused: requirement_register.rows[0].claimed_by[0]: value "
        '"' + "state/warp/" + "x" * 200 + '" does not match pattern "^docs/plans/.+\\.md$"'
    )
    monkeypatch.setattr(register_gate, "register_writeback", lambda *a, **k: {"register_writeback_error": reason})
    out = _warp_close(tmp_path, _wb_sizing_text([_wb_row("T22", claimed=False, anchor="relayed")]))

    detail = out["result"].detail
    assert out["result"].status == "failed"
    assert len(detail) > run_close.DETAIL_CAP
    assert "claimed_by[0]" in detail and "x" * 200 in detail and "does not match pattern" in detail
