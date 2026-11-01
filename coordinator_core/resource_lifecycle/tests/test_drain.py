"""AC4: a drain waits only for work admitted before a transition."""

from __future__ import annotations

import threading
import time

from coordinator_core.resource_lifecycle import Drain


def test_wait_with_nothing_admitted_returns_true():
    d = Drain()
    d.transition()
    assert d.wait(0.1) is True


def test_wait_blocks_until_pre_transition_work_finishes():
    d = Drain()
    tok = d.admit()
    d.transition()
    assert d.wait(0.05) is False
    result = []
    t = threading.Thread(target=lambda: result.append(d.wait(5)))
    t.start()
    time.sleep(0.05)
    tok.release()
    t.join(5)
    assert result == [True]


def test_post_transition_work_is_not_waited_on():
    d = Drain()
    old = d.admit()
    d.transition()
    new = d.admit()
    assert new.generation == d.generation == 1
    old.release()
    assert d.wait(0.5) is True
    new.release()


def test_context_manager_releases_on_exception():
    d = Drain()
    try:
        with d.admit():
            d.transition()
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert d.wait(0.5) is True


def test_wait_times_out_while_old_work_in_flight():
    d = Drain()
    d.admit()
    d.transition()
    assert d.wait(0.05) is False


def test_second_transition_still_waits_for_first_generation_work():
    d = Drain()
    a = d.admit()
    d.transition()
    b = d.admit()
    d.transition()
    a.release()
    assert d.wait(0.05) is False
    b.release()
    assert d.wait(0.5) is True


def test_release_is_idempotent():
    d = Drain()
    tok = d.admit()
    d.transition()
    tok.release()
    tok.release()
    assert d.wait(0.5) is True
