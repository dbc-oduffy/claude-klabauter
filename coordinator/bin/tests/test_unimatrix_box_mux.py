"""Per-far-box single ssh carrier: one spawn for concurrent sends, HostKeyAlias + host-name argv, IP refusal."""
import importlib.util
import json
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
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
def mux():
    return _load(BIN / "lib" / "unimatrix_box_mux.py", "unimatrix_box_mux")


FAKE_SSH = r'''
import base64, json, os, sys
log = os.environ["FAKE_SSH_LOG"]
def b64(b):
    return base64.b64encode(b).decode()
if sys.argv[1:2] == ["mux"]:
    if os.environ.get("FAKE_OLD_RELAY"):
        sys.stderr.write("usage: roster | send <pid> | inbox <name>\n")
        sys.exit(2)
    open(log, "a").write("carrier\n")
    for line in sys.stdin:
        req = json.loads(line)
        out = req["cmd"].encode() + b"|" + base64.b64decode(req["data"])
        print(json.dumps({"id": req["id"], "rc": 0, "stdout": b64(out), "stderr": ""}), flush=True)
else:
    open(log, "a").write("oneshot\n")
    sys.stdout.buffer.write(sys.argv[-1].encode() + b"|" + sys.stdin.buffer.read())
'''


@pytest.fixture
def world(tmp_path, monkeypatch, mux):
    script = tmp_path / "fake_ssh.py"
    script.write_text(FAKE_SSH, encoding="utf-8")
    log = tmp_path / "ssh.log"
    log.write_text("")
    home = tmp_path / "settings"
    key = home / ".secrets" / "ssh" / "unimatrix-zero"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("UNIMATRIX_TEST_ALLOWED_BOXES", "farbox")
    brokers = []

    def spawn_for(box):
        def spawn():
            b = mux.Broker(box, str(key), "farbox", home, argv_factory=lambda: [sys.executable, str(script)],
                           restart_floor=0.0)
            brokers.append(b)
            threading.Thread(target=b.serve, daemon=True).start()
        return spawn

    def chan():
        return mux.channel("farbox", key=str(key), host="farbox", home=home, win=True, spawn_broker=spawn_for("farbox"))

    yield types.SimpleNamespace(log=log, home=home, key=key, brokers=brokers, chan=chan)
    for b in brokers:
        b.stopped.set()
    for b in brokers:
        if b._proc is not None and b._proc.poll() is None:
            b._proc.terminate()


def lines(log):
    return log.read_text().split()


def test_eight_concurrent_sends_to_one_box_spawn_one_carrier(world):
    ch = world.chan()
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: ch.run(f"send-session {i}", f"m{i}".encode()), range(8)))
    assert [r.returncode for r in results] == [0] * 8
    assert [r.stdout for r in results] == [f"send-session {i}|m{i}".encode() for i in range(8)]
    assert lines(world.log) == ["carrier"]


def test_an_old_far_relay_without_mux_is_served_one_shot_single_flight(world, monkeypatch):
    monkeypatch.setenv("FAKE_OLD_RELAY", "1")
    ch = world.chan()
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda i: ch.run(f"roster {i}", b"x"), range(4)))
    assert [r.returncode for r in results] == [0] * 4
    assert sorted(r.stdout for r in results) == sorted(f"roster {i}|x".encode() for i in range(4))
    assert lines(world.log).count("oneshot") == 4


def test_a_dead_pid_lock_is_replaced_by_a_fresh_broker(world, mux):
    path = mux.lock_path(world.home, "farbox")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 2 ** 22 - 3, "procStart": "gone", "port": 1, "token": "t"}))
    r = world.chan().run("roster", b"")
    assert r.returncode == 0 and len(world.brokers) == 1


def test_a_lock_naming_a_reused_pid_is_not_a_live_broker(mux):
    import os
    assert not mux.lock_live({"pid": os.getpid(), "procStart": "not-my-start-stamp", "port": 1, "token": "t"})


def test_every_ssh_argv_carries_the_host_key_alias_and_a_host_name(mux):
    argv = mux.ssh_argv("farbox", "k", "me@farbox.local", ["-o", "ConnectTimeout=5"])
    assert "HostKeyAlias=farbox" in argv and argv[-1] == "me@farbox.local" and not mux.is_ip_literal("farbox.local")
    seen = []
    ch = mux.channel("farbox", key="k", host="farbox", direct=True, mux_options=lambda h: [])
    import unittest.mock as m
    with m.patch.object(mux.subprocess, "run", lambda a, **kw: seen.append(a) or subprocess.CompletedProcess(a, 0, b"", b"")):
        ch.run("roster")
    assert "HostKeyAlias=farbox" in seen[0] and seen[0][-2] == "farbox" and seen[0][-1] == "roster"


def test_an_ip_literal_remote_with_no_member_name_is_refused_naming_the_box(mux):
    with pytest.raises(mux.IpLiteralRemote) as ex:
        mux.for_remote("me@192.168.4.7", key="k", ip_hosts={})
    assert ex.value.box == "192.168.4.7"
    with pytest.raises(mux.IpLiteralRemote):
        mux.channel("192", key="k", host="192.168.4.7")


def test_an_ip_remote_with_a_member_name_is_addressed_by_that_name(mux):
    ch = mux.for_remote("me@192.168.4.7", key="k", ip_hosts={"192.168.4.7": "example-fleet-pro.local"}, direct=True,
                        mux_options=lambda h: [])
    assert ch.host == "me@example-fleet-pro.local" and ch.box == "example-fleet-pro"


def test_a_test_home_refuses_a_real_box_and_a_foreign_key_and_spawns_nothing(world, mux):
    spawned = []
    real_box = mux.channel("realbox", key=str(world.key), host="realbox", home=world.home, win=True,
                           spawn_broker=lambda: spawned.append(1)).run("roster")
    foreign_key = mux.channel("farbox", key="C:/Users/someone/.ssh/id", host="farbox", home=world.home, win=True,
                              spawn_broker=lambda: spawned.append(1)).run("roster")
    for r in (real_box, foreign_key):
        assert r.returncode == 255 and b"TestHomeRefusal" in r.stderr
    assert spawned == [] and lines(world.log) == []


def test_the_roster_poll_dials_by_name_with_the_host_key_alias(monkeypatch):
    roster = _load(BIN / "unimatrix-roster.py", "unimatrix_roster_mux_test")
    seen = []
    monkeypatch.setattr(roster.subprocess, "run", lambda a, **kw: seen.append(a) or subprocess.CompletedProcess(a, 0, b"", b""))
    monkeypatch.setattr(roster, "ensure_key_loaded", lambda *a, **k: None)
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: False)
    monkeypatch.setattr(roster, "mux_options", lambda remote: [])
    roster._roster_once("example-fleet-pro", "k", None)
    assert "HostKeyAlias=example-fleet-pro" in seen[0] and seen[0][-2:] == ["example-fleet-pro", "roster"]
