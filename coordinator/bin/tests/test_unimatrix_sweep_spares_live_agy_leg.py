"""The stale-socket sweep never unlinks an agy adapter socket a live agy-legs record names.

An agy leg's adapter endpoint is recorded only in `agy-legs/<conversation>.json`, never as any
session entry's `messagingSocketPath`; a sweep keyed on session entries alone unlinked a live one
(Machine-b 2026-11-22 20:16:24Z, cc-msg-366e5085afd23f15a8285125ab2c1834.sock, pid 39575 holding it).
"""
import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
LIVE_SOCK = "cc-msg-366e5085afd23f15a8285125ab2c1834.sock"
DEAD_SOCK = "cc-msg-0123456789abcdef0123456789abcdef.sock"
GONE_PID = 2_000_000_000


@pytest.fixture
def roster(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("unimatrix_roster_sweep", BIN / "unimatrix-roster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    socks = Path(os.environ["UNIMATRIX_LOOPBACK_SOCKS"])
    monkeypatch.setattr(mod, "LOOPBACK_SOCKS", str(socks))
    monkeypatch.setattr(mod, "_is_win", lambda: False)
    state = tmp_path / "unimatrix"
    (state / "agy-legs").mkdir(parents=True)
    monkeypatch.setattr(mod._liveness(), "state_dir", lambda home=None: state)
    return mod, socks, state / "agy-legs"


def _sock(socks, name):
    path = socks / name
    path.write_text("")
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def _leg(legs, conv, endpoint, pid, agy_pid):
    (legs / f"{conv}.json").write_text(json.dumps(
        {"pid": pid, "conversation": conv, "endpoint": endpoint, "ls_address": None, "agy_pid": agy_pid}))


def test_live_agy_leg_socket_survives_the_sweep(roster):
    mod, socks, legs = roster
    live = _sock(socks, LIVE_SOCK)
    _leg(legs, "conv-live", str(live), os.getpid(), GONE_PID)
    mod.reap_stale_sockets([], set(), True, lambda _m: None)
    assert live.exists()


def test_dead_agy_leg_socket_is_still_reaped(roster):
    mod, socks, legs = roster
    dead = _sock(socks, DEAD_SOCK)
    _leg(legs, "conv-dead", str(dead), GONE_PID, GONE_PID)
    mod.reap_stale_sockets([], set(), True, lambda _m: None)
    assert not dead.exists()


def test_unreadable_agy_leg_record_suspends_every_unlink(roster):
    mod, socks, legs = roster
    dead = _sock(socks, DEAD_SOCK)
    (legs / "conv-torn.json").write_text("{not json")
    mod.reap_stale_sockets([], set(), True, lambda _m: None)
    assert dead.exists()
