"""inventory_family_landed: one walk maps an inventory family's checkpoints to source-plan chunks."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_rows_landed import _commit, _cp, _git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

DIR = "state/mise-inventory"
BASE = "20261021T182452-4b88be1d"
SPINE = f"{DIR}/{BASE}-t2-p2.spine.md"
PLAN = "docs/plans/nlm.md"
ROWS = {"I10": (PLAN, "C1"), "I12": ("docs/plans/other.md", "C1")}


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "seed")
    return tmp_path


def _landed(root, stop=None, rows=ROWS):
    return cs.inventory_family_landed(root, DIR, BASE, rows, stop)


def test_family_checkpoint_maps_item_chunk_to_source_plan(repo):
    sha = _cp(repo, ["I10.C2", "I12"], plan=SPINE)
    assert _landed(repo) == {(PLAN, "C2"): sha, ("docs/plans/other.md", "C1"): sha}


def test_backslash_plan_and_unsuffixed_spine_match(repo):
    sha = _cp(repo, ["I10.C2"], plan=f"{DIR}\\{BASE}.spine.md")
    assert _landed(repo) == {(PLAN, "C2"): sha}


def test_other_family_and_plan_checkpoints_are_ignored(repo):
    _cp(repo, ["I10.C2"], plan=f"{DIR}/20260101T000000-deadbeef-t1-p1.spine.md")
    _cp(repo, ["I10.C3"], plan=PLAN)
    _cp(repo, ["I10.C4"], plan=f"{DIR}/{BASE}x.spine.md")
    assert _landed(repo) == {}


def test_gate_owed_stop_rule_and_unknown_items_are_skipped(repo):
    _commit(
        repo,
        "checkpoint(wave 0): 4 rows — I10.C2, I10.C3, I10.C4, I99.C1",
        f"Checkpoint-Plan: {SPINE}\nCheckpoint-Gate-Owed: I10.C3\nCheckpoint-Stop-Rule: I10.C4",
    )
    got = _landed(repo)
    assert list(got) == [(PLAN, "C2")]


def test_process_half_and_stamp_commits_land_nothing(repo):
    _cp(repo, ["I10.C2"], plan=SPINE, process=True)
    _commit(
        repo,
        "checkpoint(wave 0): 1 row — I10.C3",
        f"Checkpoint-Plan: {SPINE}\nCheckpoint-Stamp: abc",
    )
    assert _landed(repo) == {}


def test_newest_checkpoint_wins(repo):
    _cp(repo, ["I10.C2"], plan=SPINE)
    newer = _cp(repo, ["I10.C2"], plan=SPINE, wave=1)
    assert _landed(repo) == {(PLAN, "C2"): newer}


def test_walk_stops_at_stop_sha(repo):
    _cp(repo, ["I10.C2"], plan=SPINE)
    stop = _commit(repo, "base")
    kept = _cp(repo, ["I10.C3"], plan=SPINE, wave=1)
    assert _landed(repo, stop) == {(PLAN, "C3"): kept}


def test_empty_rows_or_unborn_head(tmp_path):
    _git(tmp_path, "init", "-q")
    assert _landed(tmp_path) == {}
    assert _landed(tmp_path, rows={}) == {}


def test_no_spawn(repo, monkeypatch):
    sha = _cp(repo, ["I10.C2"], plan=SPINE)

    def _boom(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert _landed(repo) == {(PLAN, "C2"): sha}
