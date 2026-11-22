"""One sshd connection reset must not stale the roster; ssh to one host is capped; IPv4 beats link-local."""
import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def roster(monkeypatch):
    mod = _load(BIN / "unimatrix-roster.py", "unimatrix_roster_reset")
    monkeypatch.setattr(mod, "mux_options", lambda remote, key=None: [])
    monkeypatch.setattr(mod, "lan_reachable", lambda alias: True)
    monkeypatch.setattr(mod, "ensure_key_loaded", lambda key: None)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    return mod


RESET = b"kex_exchange_identification: read: Connection reset by 192.168.0.192 port 22\n"


@pytest.mark.parametrize("win", [False, True])
def test_one_connection_reset_does_not_stale_the_roster(roster, monkeypatch, win):
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: win)
    roster._DIALS.clear()
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 255, b"", RESET)
        return subprocess.CompletedProcess(argv, 0, b'{"entry": {}}\n', b"")

    monkeypatch.setattr(roster.subprocess, "run", run)
    assert roster.fetch_roster("lan", "k", None) == [{"entry": {}}]
    assert len(calls) == 2


@pytest.mark.parametrize("win", [False, True])
def test_persistent_reset_raises_after_three_tries(roster, monkeypatch, win):
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: win)
    roster._DIALS.clear()
    calls = []
    monkeypatch.setattr(roster.subprocess, "run", lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(
        argv, 255, b"", RESET))
    with pytest.raises(OSError, match="rc=255"):
        roster.fetch_roster("lan", "k", None)
    assert len(calls) == 3


def test_host_slot_caps_concurrent_ssh_at_four(monkeypatch):
    mux = _load(BIN / "lib" / "unimatrix_box_mux.py", "unimatrix_box_mux_cap")
    live, peak, lock = 0, 0, threading.Lock()

    def run(argv, **kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(mux.subprocess, "run", run)
    ch = mux.DirectChannel("far", "k", "u@far", mux_options=lambda h: [])
    threads = [threading.Thread(target=ch.run, args=("roster",)) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert 1 < peak <= 4


def test_box_addresses_prefer_ipv4_over_link_local():
    far = _load(BIN / "lib" / "unimatrix_far_addresses.py", "unimatrix_far_addresses_pref")
    got = far.box_addresses("b", lambda k: "fe80::1%eth0, 2001:db8::2, 192.168.0.192")
    assert got == ["192.168.0.192", "2001:db8::2", "fe80::1%eth0"]
