"""The gate resumes an all-landed plan with a commit-halt pointer through `resume_ledger`, carrying the
run's recorded `inline_review`."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    RESUMED_RUN_ID_KEY,
    LedgerHalt,
    ledger_halt_path,
    write_ledger_halt,
    write_run_outcome,
)
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import (
    ALREADY_RAN,
    _landed_repo,
    _put_s,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REVIEW = {"integration_stem": "int-1", "slices": 1, "fixes": 1}


def _commit_pointer(repo, outcome=True):
    write_ledger_halt(
        ledger_halt_path(repo, PLAN_REL),
        LedgerHalt("run-1", PLAN_REL, REL, (), halted="commit", outcome="x/run-outcome.json"),
    )
    if outcome:
        write_run_outcome(repo, "run-1", {"inline_review": REVIEW, "incomplete_chunks": []})


def test_all_landed_commit_pointer_resumes_with_the_recorded_inline_review(repo):  # noqa: F811
    _put_s(repo)
    _landed_repo(repo)
    _commit_pointer(repo)
    v = gate(repo, REL)
    assert v.halt is None
    assert v.resume_ledger["inline_review"] == {**REVIEW, RESUMED_RUN_ID_KEY: "run-1"}
    assert v.resume_ledger["run_id"] == "run-1"
    assert v.resume_rows is None and v.resume_plan is None


def test_no_pointer_still_refuses_already_ran(repo):  # noqa: F811
    _put_s(repo)
    _landed_repo(repo)
    v = gate(repo, REL)
    assert v.halt is not None and ALREADY_RAN in v.halt["reason"]


def test_commit_pointer_without_a_recorded_outcome_refuses_naming_the_record(repo):  # noqa: F811
    _put_s(repo)
    _landed_repo(repo)
    _commit_pointer(repo, outcome=False)
    v = gate(repo, REL)
    assert v.halt is not None and "run-outcome.json" in v.halt["reason"]
    assert v.resume_ledger is None


def test_commit_pointer_with_an_open_row_gives_resume_rows(repo):  # noqa: F811
    _put_s(repo)
    _landed_repo(repo, landed={"A1"})
    _commit_pointer(repo)
    v = gate(repo, REL)
    assert v.halt is None
    assert v.resume_ledger is None
    assert v.resume_rows == ["A2"]


def test_commit_pointer_with_a_coded_row_is_stale_and_refuses(repo):  # noqa: F811
    _put_s(repo)
    _landed_repo(repo, rows=(("A1", "coded"), ("A2", "open")))
    _commit_pointer(repo)
    v = gate(repo, REL)
    assert v.halt is not None and ALREADY_RAN in v.halt["reason"]
