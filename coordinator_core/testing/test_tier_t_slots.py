"""Tests for the machine-wide Tier-T slot semaphore.

The invariant these exist to hold is NOT "the bound reads K" -- that reads
correctly even when the semaphore has silently stopped counting. It is:
  1. at most K slots are ever held at once,
  2. every acquisition that succeeds occupies a DISTINCT slot, and
  3. nothing is lost -- a caller that waits eventually runs.
(2) is the one that failed during development: passing the caller's owner
string straight to ``suite_mutex.acquire`` hit that module's same-owner
re-entrancy, so one session's second acquisition returned the first slot's
index while occupying one slot. The bound still read K. Only an explicit
distinctness assertion catches it.
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.testing import tier_t_slots


@pytest.fixture(autouse=True)
def isolated_settings_home(tmp_path, monkeypatch):
    """Point the semaphore at a per-test settings home.

    Without this every test in this file contends with the real box-wide
    semaphore -- and, worse, with whatever else is running on the developer's
    machine, which is exactly the kind of shared-state coupling this module
    exists to bound.
    """
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    yield


def _take(n, owner="owner"):
    return [tier_t_slots.acquire_slot(f"{owner}-{i}", f"pytest f{i}.py") for i in range(n)]


def test_cap_is_derived_not_constant():
    """K comes from the repo's worker-cap formula, so it adapts per machine.

    Imported from ``install``, not ``diagnostics``: the latter is absent from
    the published engine's restricted tree, where this code actually runs. See
    ``tier_t_slots``' module docstring.
    """
    from coordinator_core.install.derive_worker_cap import derive_cap

    assert tier_t_slots.slot_cap() == max(1, int(derive_cap()))
    assert tier_t_slots.slot_cap() >= 1


def test_semaphore_admits_exactly_k_then_refuses():
    cap = tier_t_slots.slot_cap()
    slots = _take(cap)
    assert all(s is not None for s in slots)
    assert tier_t_slots.free_slots() == 0
    assert tier_t_slots.acquire_slot("over", "pytest x.py") is None


def test_concurrent_acquisitions_occupy_distinct_slots():
    """The regression that reads correct: a bound of K over one occupied slot."""
    cap = tier_t_slots.slot_cap()
    slots = _take(cap)
    assert len({s.index for s in slots}) == cap


def test_one_owner_may_hold_several_slots_and_each_is_counted():
    """Same-owner re-entrancy would undercount the box; it must not apply here."""
    if tier_t_slots.slot_cap() < 2:
        pytest.skip("needs a cap of at least 2")
    a = tier_t_slots.acquire_slot("same", "pytest a.py")
    b = tier_t_slots.acquire_slot("same", "pytest b.py")
    assert a is not None and b is not None
    assert a.index != b.index
    assert a.token != b.token
    assert tier_t_slots.free_slots() == tier_t_slots.slot_cap() - 2


def test_release_frees_exactly_one_slot_and_it_is_reusable():
    cap = tier_t_slots.slot_cap()
    slots = _take(cap)
    target = slots[cap // 2]
    tier_t_slots.release_slot(target)
    assert tier_t_slots.free_slots() == 1
    assert tier_t_slots.acquire_slot("new", "pytest y.py").index == target.index


def test_release_by_a_foreign_lease_does_not_free_the_real_holder():
    """One caller must not be able to free another's slot."""
    real = tier_t_slots.acquire_slot("real", "pytest a.py")
    forged = tier_t_slots.Slot(index=real.index, token="forged#deadbeef", owner="forged")
    tier_t_slots.release_slot(forged)
    assert tier_t_slots.free_slots() == tier_t_slots.slot_cap() - 1


def test_dead_holder_is_reclaimed_so_a_crash_cannot_wedge_the_box():
    slot = tier_t_slots.acquire_slot("ghost", "pytest z.py", pid=999_999)
    assert slot is not None
    assert tier_t_slots.free_slots() == tier_t_slots.slot_cap()


def test_held_slot_releases_on_exception():
    cap = tier_t_slots.slot_cap()
    with pytest.raises(RuntimeError):
        with tier_t_slots.held_slot("cm", "pytest c.py") as slot:
            assert slot is not None
            assert tier_t_slots.free_slots() == cap - 1
            raise RuntimeError("boom")
    assert tier_t_slots.free_slots() == cap


def test_held_slot_yields_none_when_full_and_releases_nothing():
    cap = tier_t_slots.slot_cap()
    _take(cap)
    with tier_t_slots.held_slot("late", "pytest d.py") as slot:
        assert slot is None
    assert tier_t_slots.free_slots() == 0


def test_occupancy_names_who_is_holding():
    """A deny that can only report a count cannot tell an operator what to do."""
    tier_t_slots.acquire_slot("session-abc", "pytest a.py")
    taken, cap, holders = tier_t_slots.occupancy()
    assert taken == 1 and cap == tier_t_slots.slot_cap()
    assert holders[0]["owner"].startswith("session-abc#")
    assert holders[0]["cmd"] == "pytest a.py"


def test_read_path_never_raises_on_a_broken_state_dir(monkeypatch):
    """A resource control that can explode takes the guard down with it."""
    monkeypatch.setattr(tier_t_slots, "slot_path", lambda i: (_ for _ in ()).throw(OSError("boom")))
    assert tier_t_slots.free_slots() >= 0
    assert tier_t_slots.occupancy()[0] == 0
