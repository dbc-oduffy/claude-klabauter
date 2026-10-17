"""A refire of a ledger-check halt that blocked on one row: the rows the halt did not block stay met
without owing work changed since the halt, while the halt-blocking row still does."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUME_LEDGER_HALT_SHA_FIELD,
    UNCHANGED_SINCE_HALT_REASON,
)
from coordinator_core.ops.dispatch_emit.sizing_fire import RESUME_LEDGER_HALT_BLOCKING_FIELD
from coordinator_core.ops.dispatch_emit.tests import test_refire_after_ledger_halt_executes_unmet_rows_incident as inc
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [
        {"id": "R1", "source_text": "carry the unclaimed path", "surface": "internal", "status": "open"},
        {"id": "R2", "source_text": "landed before the halt", "surface": "internal", "status": "open"},
    ],
}
VERDICT = {
    "items": [
        {"id": "R1", "status": "not_met", "wired": True},
        {"id": "R2", "status": "met", "wired": True},
    ]
}


def test_the_gate_names_the_halt_blocking_ids_apart_from_the_rows_judged_after_it(monkeypatch, repo):  # noqa: F811
    monkeypatch.setattr(inc, "REGISTER", REGISTER)
    inc._halted_repo(repo)
    resume = ask_gate._handler({"sizing_path": REL, "run_id": inc.REFIRE_RUN}, repo)["resume_ledger"]
    assert resume["blocking"] == ["R1", "R2"]
    assert resume[RESUME_LEDGER_HALT_BLOCKING_FIELD] == ["R1"]


def test_the_composed_terminal_param_carries_only_the_halt_blocking_ids(monkeypatch):
    script = inc._script(monkeypatch)
    assert (
        f"_nextAction.params.resume_ledger.blocking = _gate.resume_ledger.{RESUME_LEDGER_HALT_BLOCKING_FIELD};"
    ) in script
    assert "label: 'ledger-remaining'" in script


def test_a_met_row_the_halt_did_not_block_stays_met_and_the_blocking_row_stays_not_met(monkeypatch, repo):  # noqa: F811
    monkeypatch.setattr(inc, "REGISTER", REGISTER)
    halt_sha = inc._halted_repo(repo)
    resume = ask_gate._handler({"sizing_path": REL, "run_id": inc.REFIRE_RUN}, repo)["resume_ledger"]
    rl.start(repo, inc.REFIRE_RUN)
    rl.bind(repo, inc.REFIRE_RUN, REL)
    run_dir = repo / RECORD_DIR_ROOT / inc.REFIRE_RUN
    manifest = repo / RECORD_DIR_ROOT / inc.HALTED_RUN / "manifest.json"
    param = {
        "run_id": inc.REFIRE_RUN,
        "halt_sha": resume[RESUME_LEDGER_HALT_SHA_FIELD],
        "blocking": resume[RESUME_LEDGER_HALT_BLOCKING_FIELD],
    }
    assert param["halt_sha"] == halt_sha

    refusal = tc._ledger_gate(repo, run_dir, VERDICT, manifest, resume_ledger=param)[0]
    failures = refusal["ledger_failures"]
    assert all(f["reason"] != UNCHANGED_SINCE_HALT_REASON or f["id"] != "R2" for f in failures), failures
    assert "R2" not in json.dumps([f for f in failures if f["reason"] == UNCHANGED_SINCE_HALT_REASON])
    assert any(f["id"] == "R1" for f in failures), failures

    whole = {**param, "blocking": resume["blocking"]}
    old = tc._ledger_gate(repo, run_dir, VERDICT, manifest, resume_ledger=whole)[0]
    assert {"id": "R2", "reason": UNCHANGED_SINCE_HALT_REASON} in old["ledger_failures"]
