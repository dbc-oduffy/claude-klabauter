"""workflow.revise withhold_paths: recorded to the run's withhold.jsonl, adds no register row."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_withhold
from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import _bound, _rows, repo  # noqa: F401

ENTRY = {"path": "web/cards.tsx", "reason": "peer run hunks"}


def _lines(dir_):
    return [json.loads(x) for x in (dir_ / run_withhold.WITHHOLD_FILE).read_text(encoding="utf-8").splitlines()]


def test_withhold_paths_alone_succeeds_and_writes_the_file(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "withhold_paths": [ENTRY]}, repo_root=repo)
    assert reply["ok"] is True and reply["item"] is None
    assert reply["withhold_paths"] == [ENTRY]
    lines = _lines(rl.run_dir(repo, "run-one"))
    assert [(x["path"], x["reason"]) for x in lines] == [("web/cards.tsx", "peer run hunks")]


def test_withhold_paths_adds_no_register_row(repo):
    _bound(repo, "run-one", ask="first ask")
    before = _rows(repo)
    rl.revise(repo, "run-one", withhold_paths=[ENTRY])
    assert _rows(repo) == before


def test_withhold_paths_beside_a_note_writes_both(repo):
    _bound(repo, "run-one", ask="first ask")
    _, dir_, added = rl.revise(repo, "run-one", note="fyi", withhold_paths=[ENTRY])
    assert added is None
    assert (dir_ / rl.NOTES_FILE).is_file() and (dir_ / run_withhold.WITHHOLD_FILE).is_file()


def test_withhold_paths_needs_no_bound_sizing(repo):
    rl.start(repo, "run-raw")
    _, dir_, added = rl.revise(repo, "run-raw", withhold_paths=[ENTRY])
    assert added is None and (dir_ / run_withhold.WITHHOLD_FILE).is_file()


@pytest.mark.parametrize("bad", [
    [{"path": "../x", "reason": "r"}],
    [{"path": "state/warp/run/run.json", "reason": "r"}],
    [{"path": "a.py", "reason": "  "}],
    [{"path": "", "reason": "r"}],
    ["a.py"],
])
def test_a_bad_entry_is_refused_and_writes_nothing(repo, bad):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "withhold_paths": bad}, repo_root=repo)
    assert "error" in reply
    assert not (rl.run_dir(repo, "run-one") / run_withhold.WITHHOLD_FILE).exists()


def test_a_non_list_is_refused(repo):
    _bound(repo, "run-one", ask="first ask")
    assert "error" in rl._handler({"run_id": "run-one", "withhold_paths": "a.py"}, repo_root=repo)


def test_a_held_run_refuses_and_creates_nothing(repo):
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, "run-never", withhold_paths=[ENTRY])
    assert not rl.run_dir(repo, "run-never").exists()


def test_the_empty_precondition_text_lists_withhold_paths(repo):
    _bound(repo, "run-one", ask="first ask")
    with pytest.raises(rl.LedgerRefused, match="withhold_paths"):
        rl.revise(repo, "run-one")
