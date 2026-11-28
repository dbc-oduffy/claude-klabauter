"""A refire's ledger gate refuses a row that blocked the halted pass and comes back not_met essential:false, on both land paths."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import land_run, terminal_commit_ledger
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halt_landing import (
    DOWNGRADED_REASON,
    NON_ESSENTIAL_OPEN_REASON,
    blocking_rows_downgraded,
)

RUN = "ask-20261118T000000-abc123"
REL = "state/sizings/2026-11-18-refire.yaml"


def _land_run_gate(root, run_dir, verdict, resume):
    return land_run._ledger_gate(root, run_dir, verdict, None, (), {}, None, {}, resume, None)


def _commit_gate(root, run_dir, verdict, resume):
    return terminal_commit_ledger._ledger_gate(root, run_dir, verdict, None, resume_ledger=resume)


GATES = [pytest.param(_land_run_gate, id="land_run"), pytest.param(_commit_gate, id="terminal_commit")]


@pytest.fixture
def run(tmp_path: Path) -> tuple[Path, Path, list]:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN, all_of_pm_ask=False)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    ledger = rl.read_ledger(tmp_path, run_dir)
    return tmp_path, run_dir, [i.id for i in ledger.items]


def _verdict(ids: list, **extra) -> dict:
    """The first register row judged not_met with ``extra``; every other row met."""
    return {"items": [
        {"id": i, "status": "not_met", **extra} if n == 0 else {"id": i, "status": "met", "evidence": "verified"}
        for n, i in enumerate(ids)
    ]}


@pytest.mark.parametrize("gate", GATES)
def test_refire_refuses_a_blocking_row_downgraded_to_non_essential(run, gate) -> None:
    root, run_dir, ids = run
    rid = ids[0]
    refusal, _, _ = gate(root, run_dir, _verdict(ids, essential=False), {"blocking": [rid]})
    assert refusal is not None and refusal["refused"] == "ledger-unmet"
    assert not refusal.get("committed")
    assert {"id": rid, "reason": DOWNGRADED_REASON} in refusal["ledger_failures"]


@pytest.mark.parametrize("gate", GATES)
def test_refire_refuses_an_essential_not_met_row(run, gate) -> None:
    root, run_dir, ids = run
    rid = ids[0]
    refusal, _, _ = gate(root, run_dir, _verdict(ids), {"blocking": [rid]})
    assert refusal is not None and refusal["refused"] == "ledger-unmet"


@pytest.mark.parametrize("gate", GATES)
def test_first_pass_still_lets_a_non_essential_row_through(run, gate) -> None:
    root, run_dir, ids = run
    refusal, _, open_rows = gate(root, run_dir, _verdict(ids, essential=False), None)
    assert refusal is None
    assert [r["reason"] for r in open_rows] == [NON_ESSENTIAL_OPEN_REASON]


@pytest.mark.parametrize("gate", GATES)
def test_refire_lets_a_row_that_never_blocked_stay_non_essential(run, gate) -> None:
    root, run_dir, ids = run
    refusal, _, open_rows = gate(root, run_dir, _verdict(ids, essential=False), {"blocking": ["R-other"]})
    assert refusal is None
    assert [r["reason"] for r in open_rows] == [NON_ESSENTIAL_OPEN_REASON]


def test_helper_matches_only_non_essential_open_blocking_rows() -> None:
    result = {"open": [
        {"id": "R1", "reason": NON_ESSENTIAL_OPEN_REASON},
        {"id": "R2", "reason": "withheld_by C2"},
        {"id": "R3", "reason": NON_ESSENTIAL_OPEN_REASON},
    ]}
    assert blocking_rows_downgraded({"blocking": ["R1", "R2"]}, result) == [{"id": "R1", "reason": DOWNGRADED_REASON}]
    assert blocking_rows_downgraded({"blocking": []}, result) == []
    assert blocking_rows_downgraded({}, result) == []
    assert blocking_rows_downgraded(None, result) == []
