"""kept_work.held_paths: a halted run that is not live, with no refire pending, past the bound holds nothing."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, halted_run, kept_work, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests._kept_fixture import git, hold_kept

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = "pkg/m.py"
RUN = "ask-20261030T101400-0b0874"
PAST = halted_hold_release.STALE_HOLD_BOUND_SECONDS + 600


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "pkg").mkdir()
    (root / REL).write_bytes(b"pinned\n")
    git(root, "add", REL)
    git(root, "commit", "-q", "-m", "x")
    hold_kept(root, RUN, [REL])
    return root


def _age(root, seconds):
    path = record_dir(root, RUN) / halted_run.HALTED_FILE
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_fresh_halt_holds(tmp_path):
    root = _repo(tmp_path)
    assert kept_work.held_paths(root, [REL]) == {REL: RUN}


def test_halt_past_the_bound_with_no_token_is_not_held(tmp_path):
    root = _repo(tmp_path)
    _age(root, PAST)
    assert kept_work.held_paths(root, [REL]) == {}


def test_halt_past_the_bound_with_its_own_live_token_still_holds(tmp_path):
    root = _repo(tmp_path)
    _age(root, PAST)
    run_token.mint(root, RUN, "wf/y.mjs")
    assert kept_work.held_paths(root, [REL]) == {REL: RUN}


def test_stale_run_does_not_hide_a_fresh_peer(tmp_path):
    root = _repo(tmp_path)
    _age(root, PAST)
    peer = "ask-20261030T223203-bbbbbb"
    hold_kept(root, peer, [REL])
    assert kept_work.held_paths(root, [REL]) == {REL: peer}
