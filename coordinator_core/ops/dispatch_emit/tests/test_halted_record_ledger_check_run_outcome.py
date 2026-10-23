"""A ledger-check halt that carries an inline_review persists run-outcome.json like a commit halt."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_OUTCOME_FILE
from coordinator_core.ops.dispatch_emit.halted_record_commit import _persist_run_outcome

RUN = "ask-20261023T000000-abcdef"
OUTCOME = {"inline_review": {"prep": {"slice_files": ["pkg/a.py"]}}}


def _persist(tmp_path, kind, params):
    return _persist_run_outcome(tmp_path, RUN, {"halted": kind}, params)


@pytest.mark.parametrize("kind", ["commit", "ledger-check"])
def test_outcome_with_inline_review_is_written(tmp_path, kind):
    rel = _persist(tmp_path, kind, {"run_outcome": OUTCOME})

    assert rel == f"{RECORD_DIR_ROOT}/{RUN}/{RUN_OUTCOME_FILE}"
    written = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert written["inline_review"]["prep"]["slice_files"] == ["pkg/a.py"]


def test_ledger_check_without_run_outcome_writes_none(tmp_path):
    assert _persist(tmp_path, "ledger-check", {}) is None
    assert not (tmp_path / RECORD_DIR_ROOT).exists()


def test_incomplete_chunks_guard_still_refuses_ledger_check(tmp_path):
    outcome = {**OUTCOME, "incomplete_chunks": ["C1"]}
    assert _persist(tmp_path, "ledger-check", {"run_outcome": outcome}) is None


@pytest.mark.parametrize("kind", ["dependents-unstarted", "wave-failed", ""])
def test_other_halt_kinds_write_none(tmp_path, kind):
    assert _persist(tmp_path, kind, {"run_outcome": OUTCOME}) is None
    assert not (tmp_path / RECORD_DIR_ROOT).exists()
