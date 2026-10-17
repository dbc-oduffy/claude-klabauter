"""workflow.revise credit_chunks: recorded to the run's credit.jsonl, adds no register row."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_credit
from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import _bound, _rows, repo  # noqa: F401

ENTRY = {"chunk": "C1", "evidence": "ledger R1 met; tests pass"}


def test_credit_chunks_alone_succeeds_and_round_trips(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "credit_chunks": [ENTRY]}, repo_root=repo)
    assert reply["ok"] is True and reply["item"] is None
    assert reply["credit_chunks"] == [ENTRY]
    assert run_credit.read([rl.run_dir(repo, "run-one")]) == {"C1": ENTRY["evidence"]}


def test_credit_chunks_adds_no_register_row(repo):
    _bound(repo, "run-one", ask="first ask")
    before = _rows(repo)
    rl.revise(repo, "run-one", credit_chunks=[ENTRY])
    assert _rows(repo) == before


@pytest.mark.parametrize("bad", [
    [{"chunk": "../x", "evidence": "e"}],
    [{"chunk": "C1", "evidence": "  "}],
    [{"chunk": "", "evidence": "e"}],
    ["C1"],
])
def test_a_bad_entry_is_refused_and_writes_nothing(repo, bad):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "credit_chunks": bad}, repo_root=repo)
    assert "error" in reply
    assert not (rl.run_dir(repo, "run-one") / run_credit.CREDIT_FILE).exists()


def test_a_non_list_is_refused(repo):
    _bound(repo, "run-one", ask="first ask")
    assert "error" in rl._handler({"run_id": "run-one", "credit_chunks": "C1"}, repo_root=repo)


def test_an_unemitted_run_refuses_and_creates_nothing(repo):
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, "run-never", credit_chunks=[ENTRY])
    assert not rl.run_dir(repo, "run-never").exists()


def test_the_empty_precondition_text_lists_credit_chunks(repo):
    _bound(repo, "run-one", ask="first ask")
    with pytest.raises(rl.LedgerRefused, match="credit_chunks"):
        rl.revise(repo, "run-one")
