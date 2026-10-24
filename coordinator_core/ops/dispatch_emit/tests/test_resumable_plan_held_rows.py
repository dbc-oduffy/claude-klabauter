"""`resumable_plan`: a row a blocking ledger item names by id skips outside credit, and a named row the gate
does not hand the stage is reported in `ResumePlan.held`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import row_ids_named
from coordinator_core.ops.dispatch_emit.run_credit import CREDIT_FILE
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_refire_blocking_item_row_outside_credit_incident import (
    PLAN_REL,
    SIZING_REL,
    _incident_repo,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _credit(repo, chunk: str) -> None:
    run = repo / "state" / "warp" / "ask-20261024T020000-abcdef"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"sizing_rel": SIZING_REL}), encoding="utf-8")
    (run / CREDIT_FILE).write_text(json.dumps({"chunk": chunk, "evidence": "ruled"}) + "\n", encoding="utf-8")


def _terminal_free(repo) -> None:
    plan = repo / PLAN_REL
    text = plan.read_text(encoding="utf-8").replace("disposition: coded", "disposition: delivered_elsewhere")
    plan.write_text(text, encoding="utf-8", newline="\n")


def test_credited_named_row_is_held_not_dispatched(tmp_path):
    _incident_repo(tmp_path)
    _credit(tmp_path, "X1")
    got = resumable_plan({"plan": PLAN_REL}, SIZING_REL, tmp_path)
    assert "X1" not in got.incomplete
    assert ("X1", "credited") in got.held, got


def test_serving_row_no_remaining_line_names_is_still_outside_credited(tmp_path):
    _incident_repo(tmp_path, remaining="append the paired VERDICT line")
    got = resumable_plan({"plan": PLAN_REL}, SIZING_REL, tmp_path)
    assert "X1" not in got.incomplete, got
    assert "X1" in {i for i, _ in got.landed_open}, got.landed_open
    assert got.held == ()


def test_named_em_row_is_neither_dispatched_nor_held(tmp_path):
    _incident_repo(tmp_path, remaining="R1: send the memo")
    got = resumable_plan({"plan": PLAN_REL}, SIZING_REL, tmp_path)
    assert "X1" in {i for i, _ in got.landed_open}, got.landed_open
    assert all(i != "R1" for i, _ in got.held), got.held


def test_ledger_path_carries_held(tmp_path):
    _incident_repo(tmp_path, stage_x1=True, add_t2=False)
    _terminal_free(tmp_path)
    _credit(tmp_path, "X1")
    got = resumable_plan({"plan": PLAN_REL}, SIZING_REL, tmp_path)
    assert got.ledger is not None, got
    assert ("X1", "credited") in got.held, got


def test_named_row_reaches_ledger_unlanded_when_staged(tmp_path):
    _incident_repo(tmp_path, stage_x1=True, add_t2=False)
    _terminal_free(tmp_path)
    got = resumable_plan({"plan": PLAN_REL}, SIZING_REL, tmp_path)
    assert got.ledger is not None, got
    assert "X1" in got.ledger.unlanded, got
    assert got.held == ()


def test_row_ids_named_matches_edges_and_whole_tokens_only():
    ids = ["X1", "X10", "R1"]
    assert row_ids_named(["X1: append the line"], ids) == ["X1"]
    assert row_ids_named(["finish (X1)."], ids) == ["X1"]
    assert row_ids_named(["X10: other"], ids) == ["X10"]
    assert row_ids_named(["x1: lower", "AX1 prefix"], ids) == []
