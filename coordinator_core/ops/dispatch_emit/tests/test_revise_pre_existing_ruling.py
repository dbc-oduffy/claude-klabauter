"""workflow.revise pre_existing: recorded to the run's pre_existing_ruled.jsonl, adds no row, read back by ruled_pre_existing."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import _bound, _rows, repo  # noqa: F401
from coordinator_core.ops.pre_existing_ruling import PRE_EXISTING_FILE, ruled_pre_existing

NODE = "coordinator_core/ops/tests/test_x.py::test_a"
ENTRY = {"nodeid": NODE, "reason": "fails at the run base too"}


def test_pre_existing_is_recorded_replied_and_read_back(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "pre_existing": [ENTRY]}, repo_root=repo)
    assert reply["ok"] is True and reply["item"] is None
    assert reply["pre_existing"] == [ENTRY]
    assert "pre_existing" in reply["applied"]
    assert (rl.run_dir(repo, "run-one") / PRE_EXISTING_FILE).is_file()
    assert ruled_pre_existing(repo, "run-one", [NODE, "other.py::t"]) == {NODE: ENTRY["reason"]}


def test_pre_existing_adds_no_register_row(repo):
    _bound(repo, "run-one", ask="first ask")
    before = _rows(repo)
    rl.revise(repo, "run-one", pre_existing=[ENTRY])
    assert _rows(repo) == before


@pytest.mark.parametrize("bad", [
    [{"nodeid": "no_separator", "reason": "r"}],
    [{"nodeid": NODE, "reason": "  "}],
    [{"nodeid": NODE}],
    [NODE],
])
def test_a_bad_entry_is_refused_and_writes_nothing(repo, bad):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "pre_existing": bad}, repo_root=repo)
    assert "error" in reply
    assert not (rl.run_dir(repo, "run-one") / PRE_EXISTING_FILE).exists()


def test_a_non_list_is_refused(repo):
    _bound(repo, "run-one", ask="first ask")
    assert "error" in rl._handler({"run_id": "run-one", "pre_existing": NODE}, repo_root=repo)


def test_an_unemitted_run_refuses_and_creates_nothing(repo):
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, "run-never", pre_existing=[ENTRY])
    assert not rl.run_dir(repo, "run-never").exists()
