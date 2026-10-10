"""Tests for the heavy_admission seed: derivation, never-overwrite, no spawn."""

from __future__ import annotations

import subprocess

from coordinator_core.bash_guards import _heavy_admission_seed as seed
from coordinator_core.bash_guards._heavy_admission_contract import (
    KEY_FREE_RAM_FLOOR_MB,
    KEY_LEASE_RESERVE_MB,
    KEY_SESSION_BACKGROUND_CAP,
    KEY_SESSION_HEAVY_CAP,
    MACHINE_LOCAL_KEYS,
)


def test_small_laptop_total():
    d = seed.derive_defaults(16 * 1024)
    assert set(d) == set(MACHINE_LOCAL_KEYS)
    assert d[KEY_FREE_RAM_FLOOR_MB] == 1638
    assert d[KEY_SESSION_HEAVY_CAP] == 1
    assert d[KEY_SESSION_BACKGROUND_CAP] == 3
    assert d[KEY_LEASE_RESERVE_MB] == 1024


def test_tiny_total_floors_at_minimum():
    assert seed.derive_defaults(4 * 1024)[KEY_FREE_RAM_FLOOR_MB] == 1536


def test_96gb_total():
    d = seed.derive_defaults(96 * 1024)
    assert d[KEY_FREE_RAM_FLOOR_MB] == 8192
    assert d[KEY_SESSION_HEAVY_CAP] == 3
    assert d[KEY_SESSION_BACKGROUND_CAP] == 5
    assert d[KEY_LEASE_RESERVE_MB] == 2048


def test_seeds_all_when_absent():
    store: dict = {}
    written = seed.seed_if_absent(32 * 1024, get=lambda: dict(store), put=lambda k, v: store.update({k: v}))
    assert set(written) == set(MACHINE_LOCAL_KEYS)
    assert store[KEY_SESSION_HEAVY_CAP] == "2"


def test_never_overwrites_operator_value():
    store = {KEY_FREE_RAM_FLOOR_MB: "99999"}
    written = seed.seed_if_absent(32 * 1024, get=lambda: dict(store), put=lambda k, v: store.update({k: v}))
    assert KEY_FREE_RAM_FLOOR_MB not in written
    assert store[KEY_FREE_RAM_FLOOR_MB] == "99999"
    assert set(written) == set(MACHINE_LOCAL_KEYS) - {KEY_FREE_RAM_FLOOR_MB}


def test_unreadable_total_writes_nothing(monkeypatch):
    monkeypatch.setattr(seed, "_host_total_mb", lambda: None)
    calls: list = []
    assert seed.seed_if_absent(get=dict, put=lambda k, v: calls.append(k)) == {}
    assert calls == []


def test_write_failure_skips_key():
    def put(k, v):
        raise OSError("ro")

    assert seed.seed_if_absent(16 * 1024, get=dict, put=put) == {}


def test_no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    seed.seed_if_absent(16 * 1024, get=dict, put=lambda k, v: None)
    assert isinstance(seed._host_total_mb(), (int, type(None)))
