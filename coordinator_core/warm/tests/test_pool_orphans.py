"""reap_orphans kills only ledger candidates outside live_pids, never another child."""

from __future__ import annotations

import multiprocessing
import time

import pytest

from coordinator_core.warm import pool_orphans

pytestmark = pytest.mark.spawns_process


def _spawn(n: int):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=time.sleep, args=(120,), daemon=True) for _ in range(n)]
    for p in procs:
        p.start()
    return procs


def _cleanup(procs) -> None:
    for p in procs:
        if p.is_alive():
            p.terminate()
        p.join(5)


def test_orphan_candidate_is_reaped_and_others_survive() -> None:
    orphan, live, stranger = _spawn(3)
    try:
        n = pool_orphans.reap_orphans(frozenset({orphan.pid, live.pid}), frozenset({live.pid}))
        assert n == 1
        assert not orphan.is_alive()
        assert live.is_alive()
        assert stranger.is_alive()
    finally:
        _cleanup([orphan, live, stranger])


def test_unknown_and_empty_inputs_reap_nothing() -> None:
    (child,) = _spawn(1)
    try:
        assert pool_orphans.reap_orphans(frozenset(), frozenset()) == 0
        assert pool_orphans.reap_orphans(frozenset({2**22 + 12345}), frozenset()) == 0
        assert child.is_alive()
    finally:
        _cleanup([child])
