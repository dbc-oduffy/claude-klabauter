"""land_run persists its leg ledger to the run record's land-legs.json on a halt and on a landing."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_deadline, land_leg_record, land_run
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261030T000000-abcdef"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True, **no_console_creationflags())
    monkeypatch.setattr("coordinator_core.lifecycle.main_worktree_root", lambda repo_root: tmp_path)
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(land_deadline, "_now", lambda: now[0])
    monkeypatch.setattr(land_deadline, "_cpu", lambda: now[0] / 1000.0)
    return now


def _mode(monkeypatch, body) -> None:
    monkeypatch.setattr(land_run, "_dispatch_mode", lambda mode, root, repo_root, params: body())


def _legs_file(root, run_id=RUN):
    return root / "state" / "warp" / run_id / land_leg_record.FILE_NAME


def _untracked(root) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain", "-uall"], cwd=str(root), capture_output=True, text=True, **no_console_creationflags()
    ).stdout


def _halting(clock):
    def body():
        land_deadline.check("land_run.script_read")
        clock[0] += 50.0
        land_deadline.check("peer_hunk_gate.sibling_spine")
        raise AssertionError("deadline did not fire")

    return body


def test_halt_leaves_the_ledger_naming_the_halting_leg(repo, clock, monkeypatch):
    _mode(monkeypatch, _halting(clock))

    reply = land_run._dispatch({"run_id": RUN, "script_path": "run.mjs"}, repo)

    assert reply["refused"] == land_deadline.REFUSED
    assert reply["leg_record"] == f"state/warp/{RUN}/land-legs.json"
    record = json.loads(_legs_file(repo).read_text(encoding="utf-8"))
    assert record["outcome"] == "land-deadline"
    assert record["leg"] == "peer_hunk_gate.sibling_spine"
    assert record["bound_by"] == reply["bound_by"]
    names = [entry["leg"] for entry in record["legs"]]
    assert names[-1] == "land_run.script_read"
    for entry in record["legs"]:
        assert {"elapsed_secs", "process_time_ms", "spawns"} <= set(entry)
    assert record["legs"][-1]["elapsed_secs"] == 50.0


def test_landed_call_leaves_the_ledger_and_no_dirt(repo, clock, monkeypatch):
    def body():
        land_deadline.check("land_run.script_read")
        clock[0] += 1.0
        land_deadline.check("land_run.commit")
        return {"committed": True, "sha": "a" * 40}

    _mode(monkeypatch, body)

    reply = land_run._dispatch({"run_id": RUN, "script_path": "run.mjs"}, repo)

    assert reply == {"committed": True, "sha": "a" * 40}
    record = json.loads(_legs_file(repo).read_text(encoding="utf-8"))
    assert record["outcome"] == "landed" and record["leg"] is None
    assert [entry["leg"] for entry in record["legs"]][-1] == "land_run.commit"
    assert "land-legs.json" not in _untracked(repo)


def test_uncommitted_reply_writes_no_ledger(repo, clock, monkeypatch):
    _mode(monkeypatch, lambda: {"committed": False, "error": "refused"})

    land_run._dispatch({"run_id": RUN, "script_path": "run.mjs"}, repo)

    assert not _legs_file(repo).exists()


@pytest.mark.parametrize("run_id", [None, "", "../evil", "a/b", "a\\b"])
def test_absent_or_unsafe_run_id_writes_nothing_and_keeps_the_reply(repo, clock, monkeypatch, run_id):
    _mode(monkeypatch, _halting(clock))
    params = {"script_path": "run.mjs"} if run_id is None else {"script_path": "run.mjs", "run_id": run_id}

    reply = land_run._dispatch(params, repo)

    assert reply["refused"] == land_deadline.REFUSED
    assert "leg_record" not in reply
    assert not (repo / "state").exists()
    assert not (repo.parent / "evil").exists()


def test_a_failing_write_leaves_the_halt_reply_unchanged(repo, clock, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(land_leg_record.os, "replace", boom)
    _mode(monkeypatch, _halting(clock))

    reply = land_run._dispatch({"run_id": RUN, "script_path": "run.mjs"}, repo)

    assert reply["refused"] == land_deadline.REFUSED
    assert reply["leg"] == "peer_hunk_gate.sibling_spine"
    assert "leg_record" not in reply
    assert land_leg_record.write(repo, RUN, outcome="x", leg=None, bound_by=None, legs=[]) is None
