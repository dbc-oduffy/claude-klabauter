"""rows_landed_at_head and the status-form in_run_edge_key."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs
from coordinator_core.ops.dispatch_emit.spine_read import _without_in_run_edges, in_run_edge_key
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_rows_landed import (
    OTHER,
    PLAN,
    _commit,
    _cp,
    _git,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "seed")
    return tmp_path


def _at_head(root, ids):
    return cs.rows_landed_at_head(root, PLAN, frozenset(ids))


def test_checkpoint_naming_the_row_lands_it(repo):
    _cp(repo, ["A1", "A2"])
    _commit(repo, "terminal")
    assert _at_head(repo, {"A1", "A2", "A3"}) == {"A1", "A2"}


def test_gate_owed_and_stop_rule_rows_are_not_landed(repo):
    _commit(
        repo,
        "checkpoint(wave 0): 3 rows — A1, A2, A3",
        f"Checkpoint-Plan: {PLAN}\nCheckpoint-Gate-Owed: A2\nCheckpoint-Stop-Rule: A3",
    )
    assert _at_head(repo, {"A1", "A2", "A3"}) == {"A1"}


def test_another_plans_checkpoint_does_not_land(repo):
    _cp(repo, ["A1"], plan=OTHER)
    assert _at_head(repo, {"A1"}) == frozenset()


def test_process_half_child_is_skipped(repo):
    _cp(repo, ["A1"], process=True)
    assert _at_head(repo, {"A1"}) == frozenset()


def test_unborn_head_is_empty(tmp_path):
    _git(tmp_path, "init", "-q")
    assert _at_head(tmp_path, {"A1"}) == frozenset()


def test_no_spawn(repo, monkeypatch):
    _cp(repo, ["A1"])

    def _boom(*a, **kw):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    monkeypatch.setattr(subprocess, "run", _boom)
    assert _at_head(repo, {"A1"}) == {"A1"}


def test_status_edge_key_names_the_whole_plan():
    assert in_run_edge_key({"plan": "docs\\p.md", "status": "reviewed"}) == ("docs/p.md", "*")
    assert in_run_edge_key({"plan": "docs/p.md", "chunk": "C1"}) == ("docs/p.md", "C1")
    assert in_run_edge_key({"plan": "docs/p.md"}) is None


def test_status_edge_dropped_when_plan_in_run():
    raw = {"depends_on_plan": [{"plan": "docs/p.md", "status": "reviewed"}]}
    assert _without_in_run_edges(raw, frozenset({("docs/p.md", "*")}))["depends_on_plan"] == []
