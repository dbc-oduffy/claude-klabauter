"""A halted run on the committer's `resumed_from` chain is its own dead predecessor: its kept.json hold
never parks the successor's targets; an unrelated live run's hold stays."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests._kept_fixture import git, hold_kept

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = "pkg/m.py"


def _mint(age: int, tail: str) -> str:
    """Fresh stamps: a halt past the stale bound holds nothing, which would hide the lineage rule."""
    return time.strftime("ask-%Y%m%dT%H%M%S", time.gmtime(time.time() - age)) + "-" + tail


OLDEST = _mint(900, "5e34fc")
MIDDLE = _mint(600, "aaaaaa")
SUCCESSOR = _mint(300, "144312")
PEER = _mint(200, "bbbbbb")


def _link(root: Path, run: str, prev: str | None) -> None:
    rd = record_dir(root, run)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({"resumed_from": prev} if prev else {}), encoding="utf-8")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "pkg").mkdir()
    (root / REL).write_bytes(b"pinned\n")
    git(root, "add", REL)
    git(root, "commit", "-q", "-m", "x")
    return root


def test_transitive_predecessor_hold_is_own(root: Path) -> None:
    hold_kept(root, OLDEST, [REL])
    _link(root, OLDEST, None)
    _link(root, MIDDLE, OLDEST)
    _link(root, SUCCESSOR, MIDDLE)
    assert kept_work.held_paths(root, [REL]) == {REL: OLDEST}
    assert kept_work.held_paths(root, [REL], SUCCESSOR) == {}
    assert halted_hold_release.release_reason(root, OLDEST, SUCCESSOR) == halted_hold_release.RELEASE_OWN_LINEAGE


def test_unrelated_live_run_hold_stays_foreign(root: Path) -> None:
    hold_kept(root, OLDEST, [REL])
    hold_kept(root, PEER, [REL])
    _link(root, OLDEST, None)
    _link(root, SUCCESSOR, OLDEST)
    _link(root, PEER, None)
    assert kept_work.held_paths(root, [REL], SUCCESSOR) == {REL: PEER}
    assert halted_hold_release.release_reason(root, PEER, SUCCESSOR) != halted_hold_release.RELEASE_OWN_LINEAGE


def test_resumed_from_cycle_terminates(root: Path) -> None:
    hold_kept(root, OLDEST, [REL])
    _link(root, OLDEST, SUCCESSOR)
    _link(root, SUCCESSOR, OLDEST)
    assert kept_work.held_paths(root, [REL], SUCCESSOR) == {}
