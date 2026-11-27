"""A declared path the run never rewrote is untouched, not a peer's: neither split nor unsettled, kept in the overlay."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha
from coordinator_core.ops.dispatch_emit import hunk_provenance as hp
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261115T223350-db6c1f"
PATH = "t/test_stamp.py"
HEAD_TEXT = b"a\nb\nc\nd\ne\nf\n"
PRE_RUN = b"A\nb\nc\nd\ne\nf\n"
REWRITTEN = b"A\nb\nc\nd\ne\nF\n"


def _git(root, *args, data=None):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, input=data,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


@pytest.fixture
def state() -> dict:
    return {"declared": True}


@pytest.fixture
def world(tmp_path, monkeypatch, state):
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.com"), ("config", "user.name", "t"),
                 ("config", "core.autocrlf", "false")):
        _git(tmp_path, *args)
    (tmp_path / "t").mkdir()
    (tmp_path / PATH).write_bytes(HEAD_TEXT)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    def baseline(root, run):
        return {PATH: _git(root, "hash-object", "-w", "--stdin", data=PRE_RUN)}

    monkeypatch.setattr(hha, "_baseline", baseline)
    monkeypatch.setattr(
        hha, "_manifest", lambda root, run: {"rows": [{"writes": [PATH] if state["declared"] else []}]},
    )
    monkeypatch.setattr(hp, "_halted_holder", lambda *a, **k: None)
    monkeypatch.setattr(hp, "_owners", lambda *a, **k: [])
    monkeypatch.setattr(hp, "_own_chain", lambda *a, **k: False)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.co_declared_settle.settled_blob", lambda *a, **k: "d" * 40)
    return tmp_path


def test_declared_path_equal_to_its_baseline_is_untouched_never_a_peer(world):
    (world / PATH).write_bytes(PRE_RUN)
    assert hp.untouched_declared(world, RUN, [PATH]) == [PATH]
    assert hp.peer_split(world, RUN, [PATH]) == ({}, {})


def test_an_undeclared_path_equal_to_its_baseline_keeps_the_peer_split(world, state):
    state["declared"] = False
    (world / PATH).write_bytes(PRE_RUN)
    assert hp.untouched_declared(world, RUN, [PATH]) == []
    split, unsettled = hp.peer_split(world, RUN, [PATH])
    assert split == {PATH: {"blob": "d" * 40, "owner": hp.UNKNOWN_OWNER, "lines": "1,1"}} and unsettled == {}


def test_a_declared_path_the_run_rewrote_keeps_todays_split(world):
    (world / PATH).write_bytes(REWRITTEN)
    assert hp.untouched_declared(world, RUN, [PATH]) == []
    split, _ = hp.peer_split(world, RUN, [PATH])
    assert split == {PATH: {"blob": "d" * 40, "owner": hp.UNKNOWN_OWNER, "lines": "1,1"}}


def test_a_path_equal_to_head_is_not_untouched(world):
    (world / PATH).write_bytes(HEAD_TEXT)
    assert hp.untouched_declared(world, RUN, [PATH]) == []


def test_a_kept_blob_matching_the_worktree_means_the_run_wrote_it(world, monkeypatch):
    from coordinator_core.ops.dispatch_emit import kept_work

    (world / PATH).write_bytes(PRE_RUN)
    monkeypatch.setattr(hp.RunInvariants, "own_kept", lambda self: {PATH: frozenset({"k" * 40})})
    monkeypatch.setattr(kept_work, "matches_committed", lambda *a, **k: True)
    assert hp.untouched_declared(world, RUN, [PATH]) == []


def test_a_kept_blob_equal_to_the_baseline_dirt_is_the_snapshot_not_a_rewrite(world, monkeypatch):
    """The witness: baseline, kept blob and worktree are one blob; the run's own declared bytes are not a peer's."""
    from coordinator_core.ops.dispatch_emit import kept_work

    (world / PATH).write_bytes(PRE_RUN)
    blob = _git(world, "hash-object", PATH)
    monkeypatch.setattr(hp.RunInvariants, "foreign_at_baseline", lambda self: {PATH: blob})
    monkeypatch.setattr(hp.RunInvariants, "own_kept", lambda self: {PATH: frozenset({blob})})
    monkeypatch.setattr(kept_work, "matches_committed", lambda *a, **k: True)
    assert hp.untouched_declared(world, RUN, [PATH]) == [PATH]
    assert hp.peer_split(world, RUN, [PATH]) == ({}, {})


def test_a_foreign_at_baseline_path_the_run_never_rewrote_stays_out_of_the_commit_candidate(world, monkeypatch):
    (world / PATH).write_bytes(PRE_RUN)
    blob = _git(world, "hash-object", PATH)
    monkeypatch.setattr(hp.RunInvariants, "foreign_at_baseline", lambda self: {PATH: blob})
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.co_declared_settle.settled_blob", lambda *a, **k: _git(
        world, "rev-parse", "HEAD:" + PATH))
    out = mr.measured_rev(RUN, [PATH], repo_root=world)
    head_blob = _git(world, "rev-parse", "HEAD:" + PATH)
    assert "untouched_declared" not in out
    assert out["peer_hunks"][PATH]["split"] is True
    assert _git(world, "rev-parse", out["overlay_sha"] + ":" + PATH) == head_blob == out["own_blobs"][PATH]


def test_a_rewritten_foreign_at_baseline_path_keeps_the_runs_own_hunks(world, monkeypatch):
    (world / PATH).write_bytes(REWRITTEN)
    foreign = _git(world, "hash-object", "-w", "--stdin", data=PRE_RUN)
    monkeypatch.setattr(hp.RunInvariants, "foreign_at_baseline", lambda self: {PATH: foreign})
    assert hp.untouched_declared(world, RUN, [PATH]) == []
    split, _ = hp.peer_split(world, RUN, [PATH])
    assert split == {PATH: {"blob": "d" * 40, "owner": hp.UNKNOWN_OWNER, "lines": "1,1"}}


def test_measured_rev_records_the_untouched_path_and_the_overlay_keeps_its_bytes(world):
    (world / PATH).write_bytes(PRE_RUN)
    out = mr.measured_rev(RUN, [PATH], repo_root=world)
    assert out["untouched_declared"] == [PATH]
    assert "peer_hunks" not in out and "overlay_sha" not in out
    in_tree = _git(world, "hash-object", PATH)
    assert out["blobs"][PATH] == in_tree != _git(world, "rev-parse", "HEAD:" + PATH)
    assert json.loads(measured_rev_record_path(world, RUN).read_text())["untouched_declared"] == [PATH]


def test_measured_rev_names_no_untouched_path_when_the_run_rewrote_it(world):
    (world / PATH).write_bytes(REWRITTEN)
    out = mr.measured_rev(RUN, [PATH], repo_root=world)
    assert "untouched_declared" not in out
    assert out["peer_hunks"][PATH]["split"] is True
