"""workflow.revise stop_after="plan": records the park marker on the run, adds no row."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_FILE, STOP_AFTER_PLAN
from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import _bound, _rows, repo  # noqa: F401


def _marker(repo, run_id):
    return json.loads((rl.run_dir(repo, run_id) / STOP_AFTER_FILE).read_text(encoding="utf-8"))


def test_accepts_plan_writes_marker_and_replies(repo):
    _bound(repo, "run-one", ask="first ask")
    rows = _rows(repo)
    reply = rl._handler({"run_id": "run-one", "stop_after": "plan", "from": "em-1"}, repo_root=repo)
    assert reply["ok"] is True and reply["stop_after"] == "plan" and reply["item"] is None
    marker = _marker(repo, "run-one")
    assert marker["stop_after"] == STOP_AFTER_PLAN and marker["from"] == "em-1" and marker["at"]
    assert _rows(repo) == rows


def test_marker_from_is_null_without_a_sender(repo):
    _bound(repo, "run-one", ask="first ask")
    rl.revise(repo, "run-one", stop_after="plan")
    assert _marker(repo, "run-one")["from"] is None


def test_other_value_refuses_and_writes_nothing(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "stop_after": "stage"}, repo_root=repo)
    assert "error" in reply and "'plan'" in reply["error"]
    assert not (rl.run_dir(repo, "run-one") / STOP_AFTER_FILE).exists()


def test_not_emitted_run_refuses_and_writes_nothing(repo):
    with pytest.raises(rl.LedgerRefused, match="emit it first"):
        rl.revise(repo, "run-never", stop_after="plan")
    assert not rl.run_dir(repo, "run-never").exists()


def test_staged_run_refuses_and_writes_nothing(repo):
    _bound(repo, "run-one", ask="first ask")
    (rl.run_dir(repo, "run-one") / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(rl.LedgerRefused, match="the run has staged; stop_after=plan can no longer park it"):
        rl.revise(repo, "run-one", stop_after="plan")
    assert not (rl.run_dir(repo, "run-one") / STOP_AFTER_FILE).exists()


def test_repeat_is_idempotent(repo):
    _bound(repo, "run-one", ask="first ask")
    rows = _rows(repo)
    rl.revise(repo, "run-one", stop_after="plan")
    rl.revise(repo, "run-one", stop_after="plan")
    assert _marker(repo, "run-one")["stop_after"] == "plan"
    assert _rows(repo) == rows


def test_adds_no_register_row_and_satisfies_the_non_empty_precondition(repo):
    _bound(repo, "run-one", ask="first ask")
    ledger, _dir, added = rl.revise(repo, "run-one", stop_after="plan")
    assert added is None and len(_rows(repo)) == 1 and len(ledger.items) == 1


def test_empty_precondition_text_lists_stop_after(repo):
    _bound(repo, "run-one", ask="first ask")
    with pytest.raises(rl.LedgerRefused, match="stop_after"):
        rl.revise(repo, "run-one")
