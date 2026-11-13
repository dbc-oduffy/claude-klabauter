"""review_passed_chunks: ledger-check halt with passing tests keeps finished chunks."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit.halt_review_passed import review_passed_chunks

LEDGER = {"halted": ask_contract.LEDGER_HALT_KIND}
FINISHED = ["C0", "C1"]


@pytest.mark.parametrize("check", ["pass", "pass-on-retest"])
def test_ledger_check_with_passing_tests_keeps_all_finished(check):
    assert review_passed_chunks(LEDGER, check, FINISHED) == frozenset(FINISHED)


@pytest.mark.parametrize("check", ["failed", "fail", "", "no-verdict", "refusal"])
def test_non_passing_check_keeps_nothing(check):
    assert review_passed_chunks(LEDGER, check, FINISHED) == frozenset()


def test_other_halt_kind_keeps_nothing():
    assert review_passed_chunks({"halted": "commit"}, "pass", FINISHED) == frozenset()


def test_missing_halt_kind_keeps_nothing():
    assert review_passed_chunks({}, "pass", FINISHED) == frozenset()


def test_stopped_rows_excluded():
    halted = {**LEDGER, "stopped_rows": ["C1"]}
    assert review_passed_chunks(halted, "pass", FINISHED) == frozenset({"C0"})


def test_empty_finished():
    assert review_passed_chunks(LEDGER, "pass", []) == frozenset()
