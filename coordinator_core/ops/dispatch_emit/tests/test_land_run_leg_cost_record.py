"""land_run persists its leg ledger to the run record's land-legs.json on a halt and on a landing."""

from __future__ import annotations

import json
import os
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


@pytest.fixture(autouse=True)
def _lock_namespace(tmp_path_factory, monkeypatch):
    """Keep the byte gate's lock sidecar out of the operator's home."""
    locks = tmp_path_factory.mktemp("locks")
    monkeypatch.setattr("coordinator_core.locked_write._machine_lock_dir", lambda: locks)


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


STAT_DIRTY = 6
CONTENT_CHANGED = 2
STAT_CLEAN = 12


def _git(root, *args) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def dirty_tree(repo):
    """Committed files with old mtimes: `STAT_CLEAN` untouched, `STAT_DIRTY` touched forward with the
    same bytes, `CONTENT_CHANGED` rewritten at a new size. Returns `(literals, stat_dirty_bytes)`."""
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "core.autocrlf", "false")
    names = [f"src/f{i:02d}.txt" for i in range(STAT_CLEAN + STAT_DIRTY + CONTENT_CHANGED)]
    old_ns = 1_600_000_000 * 10**9 + 123_456_789  # a zero nanosecond field is always hashed
    for i, rel in enumerate(names):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"base {i}\n" * (20 + i), encoding="utf-8", newline="\n")
        os.utime(path, ns=(old_ns, old_ns))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    touched = names[STAT_CLEAN : STAT_CLEAN + STAT_DIRTY]
    changed = names[STAT_CLEAN + STAT_DIRTY :]
    for rel in touched:
        os.utime(repo / rel, ns=(old_ns, old_ns + 7 * 10**9))
    for rel in changed:
        (repo / rel).write_text("rewritten " * 30 + rel, encoding="utf-8", newline="\n")
    return names, sum((repo / rel).stat().st_size for rel in touched + changed)


def _scan_body(root, literals):
    def body():
        land_deadline.check("land_run.script_read")
        land_deadline.mark("land_run.dirty_scan")
        changed, _ignored, _head = land_run._dirty_among(root, literals)
        land_deadline.mark("land_run.superseded_scan")
        assert len(changed) == CONTENT_CHANGED
        land_deadline.check("land_run.commit")
        return {"committed": True, "sha": "b" * 40}

    return body


def test_product_landing_ledger_counts_bytes_of_stat_dirty_paths_only(repo, clock, monkeypatch, dirty_tree):
    literals, dirty_bytes = dirty_tree
    _mode(monkeypatch, _scan_body(repo, literals))

    reply = land_run._dispatch({"run_id": RUN}, repo)

    assert reply["committed"] is True
    record = json.loads(_legs_file(repo).read_text(encoding="utf-8"))
    assert record["outcome"] == "landed"
    legs = {entry["leg"]: entry for entry in record["legs"]}
    for entry in record["legs"]:
        assert {"elapsed_secs", "bytes_read"} <= set(entry)
    assert legs["land_run.dirty_scan"]["bytes_read"] == dirty_bytes
    assert sum(entry["bytes_read"] for entry in record["legs"]) == dirty_bytes
    assert legs["land_run.commit"]["bytes_read"] == 0


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
