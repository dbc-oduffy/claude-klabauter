"""The warm client mints a distinct dispatch key per call, even on a coarse clock."""

from __future__ import annotations

from coordinator_core.warm import client


def test_keys_are_distinct_when_the_monotonic_clock_repeats(monkeypatch):
    monkeypatch.setattr(client.time, "monotonic_ns", lambda: 123456789)
    keys = [client._mint_dispatch_key() for _ in range(50)]
    assert len(set(keys)) == len(keys)


def test_key_stays_a_plain_string_the_poll_command_can_carry():
    key = client._mint_dispatch_key()
    assert isinstance(key, str)
    assert key in client._poll_command_for(key)
