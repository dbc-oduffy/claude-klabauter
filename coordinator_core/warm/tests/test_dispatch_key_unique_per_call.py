"""The shared dispatch-key minter yields a distinct key per call, even on a coarse clock."""

from __future__ import annotations

import time

from coordinator_core.warm import client, dispatch_ack


def test_keys_are_distinct_when_the_monotonic_clock_repeats(monkeypatch):
    monkeypatch.setattr(dispatch_ack.time, "monotonic_ns", lambda: 123456789)
    keys = [dispatch_ack.mint_key() for _ in range(50)]
    assert len(set(keys)) == len(keys)


def test_key_tail_is_on_the_monotonic_clock_boot_ns_uses():
    before = time.monotonic_ns()
    tail = dispatch_ack.mint_ns_of(dispatch_ack.mint_key())
    assert before <= tail <= time.monotonic_ns()


def test_key_stays_a_plain_string_the_poll_command_can_carry():
    key = dispatch_ack.mint_key()
    assert isinstance(key, str)
    assert key in client._poll_command_for(key)


def test_a_fresh_key_is_admitted_by_a_store_booted_before_it():
    """The server reads the key's tail as its mint time; a key minted after
    engine boot must be admitted, not refused as pre-boot (-32009)."""
    store = dispatch_ack.AckStore()
    assert store.admit(dispatch_ack.mint_key(), "plan.tasks.mutate")
    assert store.admit(dispatch_ack.mint_key(), "plan.tasks.mutate")
