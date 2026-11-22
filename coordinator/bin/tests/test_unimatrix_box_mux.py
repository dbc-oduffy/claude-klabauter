"""Per-far-box single ssh carrier: one spawn for concurrent sends, HostKeyAlias + host-name argv, IP refusal."""
import base64
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


def test_a_caller_keepalive_is_the_first_value_ssh_reads_so_it_overrides_the_default(mux):
    # ssh keeps the first of a repeated -o: with the defaults first, the roster's 5s x 2 keepalive was dead
    # and a half-dead link held a call to the default 90s, past the roster's 20s budget.
    argv = mux.ssh_argv("farbox", "k", "farbox", ["-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"])
    first = {}
    for opt in (a for a in argv if "=" in a):
        first.setdefault(opt.split("=")[0], opt)
    assert first["ServerAliveInterval"] == "ServerAliveInterval=5" and first["ServerAliveCountMax"] == "ServerAliveCountMax=2"
    assert argv[0] == "ssh" and argv[-1] == "farbox"
    bare = mux.ssh_argv("farbox", "k", "farbox")
    assert "ServerAliveInterval=30" in bare


def test_an_ip_literal_remote_with_no_member_name_stays_on_the_ip_with_the_ip_as_host_key_alias(mux):
    ch = mux.for_remote("me@192.168.4.7", key="k", ip_hosts={}, direct=True, mux_options=lambda h: [])
    assert ch.host == "me@192.168.4.7" and ch.box == "192.168.4.7"
    assert "HostKeyAlias=192.168.4.7" in mux.ssh_argv(ch.box, "k", ch.host, [])
    assert mux.box_of("fe80::1") == "fe80--1"


def test_an_ip_remote_with_a_member_name_is_addressed_by_that_name(mux):
    ch = mux.for_remote("me@192.168.4.7", key="k", ip_hosts={"192.168.4.7": "example-fleet-pro.local"}, direct=True,
                        mux_options=lambda h: [], connect=lambda h: True)
    assert ch.host == "me@example-fleet-pro.local" and ch.box == "example-fleet-pro"


def _fallback(mux, *, answering, reads=None, statuses=None):
    """for_remote with an unreachable name; `answering` are the addresses that pass TCP, `statuses` their verify result."""
    mux._VERIFIED.clear()
    probed, read = [], []

    def read_key(name):
        read.append(name)
        return "192.0.2.1, 192.0.2.2, 192.0.2.3" if reads is None else reads

    def verify(target):
        probed.append(target)
        return (statuses or {}).get(target.rpartition("@")[2], ("ok", ""))

    ch = mux.for_remote("me@farbox.local", key="k", ip_hosts={}, direct=True, mux_options=lambda h: [],
                        connect=lambda h: h in answering, read_key=read_key, verify=verify)
    return ch, probed, read


def test_a_reachable_host_name_is_dialled_with_no_key_read_and_no_probe(mux):
    mux._VERIFIED.clear()
    read, probed = [], []
    ch = mux.for_remote("me@farbox.local", key="k", ip_hosts={}, direct=True, mux_options=lambda h: [],
                        connect=lambda h: True, read_key=lambda n: read.append(n), verify=lambda t: probed.append(t))
    assert ch.host == "me@farbox.local" and read == [] and probed == []


def test_an_unreachable_name_dials_the_first_verified_listed_address_with_the_box_host_key_alias(mux):
    ch, probed, read = _fallback(mux, answering={"192.0.2.2", "192.0.2.3"})
    assert read == ["unimatrix.addresses.farbox"]
    assert ch.host == "me@192.0.2.2" and ch.box == "farbox" and probed == ["me@192.0.2.2"]
    seen = []
    import unittest.mock as m
    with m.patch.object(mux.subprocess, "run", lambda a, **kw: seen.append(a) or subprocess.CompletedProcess(a, 0, b"sent", b"")):
        r = ch.run("send hello")
    assert r.returncode == 0 and r.stdout == b"sent"
    assert "HostKeyAlias=farbox" in seen[0] and seen[0][-2:] == ["me@192.0.2.2", "send hello"]


def test_an_older_far_addresses_without_tcp_connect_names_the_skew_and_degrades_to_the_host(mux, monkeypatch, capsys):
    mux._VERIFIED.clear()
    monkeypatch.setattr(mux, "_far_addresses", lambda: types.SimpleNamespace())
    assert mux.verified_host("me@farbox.local", "farbox", "k") == "me@farbox.local"
    err = capsys.readouterr().err
    assert "lacks tcp_connect" in err and "version skew" in err


def test_a_caller_supplied_connect_still_works_when_far_addresses_lacks_tcp_connect(mux, monkeypatch, capsys):
    mux._VERIFIED.clear()
    monkeypatch.setattr(mux, "_far_addresses", lambda: types.SimpleNamespace())
    assert mux.verified_host("me@farbox.local", "farbox", "k", connect=lambda h: True) == "me@farbox.local"
    assert capsys.readouterr().err == ""


def test_an_address_held_by_a_different_machine_is_never_the_box(mux):
    ch, probed, _ = _fallback(mux, answering={"192.0.2.1", "192.0.2.3"},
                              statuses={"192.0.2.1": ("not-this-box", "key mismatch")})
    assert probed == ["me@192.0.2.1", "me@192.0.2.3"] and ch.host == "me@192.0.2.3"
    ch, _, _ = _fallback(mux, answering={"192.0.2.1"}, statuses={"192.0.2.1": ("not-this-box", "key mismatch")})
    assert ch.host == "me@farbox.local"


def test_an_unreachable_name_with_no_list_or_no_answering_address_keeps_the_name(mux):
    assert _fallback(mux, answering=set(), reads="")[0].host == "me@farbox.local"
    ch, probed, _ = _fallback(mux, answering=set())
    assert ch.host == "me@farbox.local" and probed == []


def test_a_verified_address_is_cached_so_the_next_dial_does_not_probe_again(mux):
    ch, probed, read = _fallback(mux, answering={"192.0.2.2"})
    again = mux.for_remote("me@farbox.local", key="k", ip_hosts={}, direct=True, mux_options=lambda h: [],
                           connect=lambda h: False, read_key=lambda n: read.append(n) or "", verify=lambda t: probed.append(t))
    assert again.host == "me@192.0.2.2" and len(read) == 1 and probed == ["me@192.0.2.2"]


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
    monkeypatch.setattr(roster, "mux_options", lambda remote, key=None: [])
    roster._roster_once("example-fleet-pro", "k", None)
    assert "HostKeyAlias=example-fleet-pro" in seen[0] and seen[0][-2:] == ["example-fleet-pro", "roster"]


def test_each_identity_key_gets_its_own_control_master(tmp_path, monkeypatch):
    # Machine-b 2026-11-15: the roster poll's master (roster key) carried every bridge send-session,
    # so the far relay answered with a roster dump and no cross-box message landed.
    roster = _load(BIN / "unimatrix-roster.py", "unimatrix_roster_ctl_test")
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: False)
    monkeypatch.setattr(roster, "settings_home", lambda: tmp_path)
    poll, send = roster.mux_options("machine-a-lan", "roster-key"), roster.mux_options("machine-a-lan", "send-key")
    assert [o for o in poll if o.startswith("ControlPath=")] != [o for o in send if o.startswith("ControlPath=")]
    assert roster.control_path("machine-a-lan", key="send-key") == roster.control_path("machine-a-lan", key="send-key")


def _inbox_client():
    return _load(BIN / "unimatrix-inbox.py", "unimatrix_inbox_under_test")


def _inbox_sessions(tmp_path, client, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1.json").write_text('{"pid":1}')
    (sessions / "1.test.key").write_text("test-token")
    monkeypatch.setattr(client, "SESSIONS", str(sessions))


def test_an_inbox_client_given_an_unmapped_ip_remote_with_no_membership_connects_on_the_ip(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    client = _inbox_client()
    _inbox_sessions(tmp_path, client, monkeypatch)
    client._box_mux()
    monkeypatch.setattr(sys.modules["unimatrix_box_mux"], "member_ip_hosts", lambda: {})
    monkeypatch.setattr(client, "pick_remote", lambda *a: "me@192.168.0.116")
    argv = []
    proc = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(), pid=7, wait=lambda: 0)
    monkeypatch.setattr(client.subprocess, "Popen", lambda cmd, **kw: argv.append(cmd) or proc)
    try:
        client.main(["1", "temporary", str(tmp_path / "log"), "--remote", "me@192.168.0.116"])
    finally:
        client.log.close()
    assert "HostKeyAlias=192.168.0.116" in argv[0] and "me@192.168.0.116" in argv[0]
    assert "up, ssh pid 7" in (tmp_path / "log").read_text()


def test_an_inbox_startup_exception_is_logged_before_it_propagates(tmp_path, monkeypatch):
    client = _inbox_client()
    _inbox_sessions(tmp_path, client, monkeypatch)

    def boom(*a):
        raise OSError("no route to far box")

    monkeypatch.setattr(client, "pick_remote", boom)
    try:
        with pytest.raises(OSError):
            client.main(["1", "temporary", str(tmp_path / "log"), "--remote", "me@192.168.0.116"])
    finally:
        client.log.close()
    text = (tmp_path / "log").read_text()
    assert "inbox failed" in text and "no route to far box" in text


def test_a_crlf_signer_pin_has_the_same_fingerprint_as_the_lf_pin():
    membership = _load(BIN / "unimatrix-membership.py", "unimatrix_membership_crlf")
    body = base64.b64encode(b"\x00" * 32).decode()
    line = f"ssh-ed25519 {body} signer"
    lf = membership.fingerprint(line + "\n")
    assert membership.fingerprint(line + "\r\n") == lf
    with pytest.raises(membership.MembershipError):
        membership.fingerprint(line + "\r\n" + line + "\r\n")


MUX_OPTIONS = lambda h: ["-o", "ControlMaster=auto", "-o", "ControlPath=/ctl/r-key"]


@pytest.fixture
def far_homes(tmp_path, monkeypatch, mux):
    """A tmp settings home and a tmp session registry; no far platform recorded yet."""
    home = tmp_path / "settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    mux._FAR_PLATFORM.clear()

    def record(box, platform):
        path = home / "unimatrix" / "control-far" / f"{box}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"platform": platform}), encoding="utf-8")

    def session(name, **entry):
        (tmp_path / ".claude" / "sessions" / name).write_text(json.dumps(entry), encoding="utf-8")

    return types.SimpleNamespace(record=record, session=session)


def _argv(mux, box="machine-a", **kw):
    ch = mux.channel(box, key="k", host=box, direct=True, mux_options=MUX_OPTIONS, **kw)
    return ch._argv()


def test_a_windows_far_box_gets_no_control_option(mux, far_homes):
    far_homes.record("machine-a", "win32")
    assert not any("Control" in a for a in _argv(mux))


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_posix_far_box_keeps_its_control_options(mux, far_homes, platform):
    far_homes.record("machine-a", platform)
    argv = _argv(mux)
    assert "ControlMaster=auto" in argv and "ControlPath=/ctl/r-key" in argv


@pytest.mark.parametrize("platform", [None, "freebsd", ""])
def test_an_unknown_far_box_gets_no_control_option(mux, far_homes, platform):
    if platform is not None:
        far_homes.record("machine-a", platform)
    assert not any("Control" in a for a in _argv(mux))


def test_the_default_roster_mux_options_are_gated_too(mux, far_homes, monkeypatch):
    roster = mux._roster()
    monkeypatch.setattr(roster, "mux_options", lambda remote, key=None: ["-o", "ControlMaster=auto"])
    ch = mux.channel("machine-a", key="k", host="machine-a", direct=True)
    assert "ControlMaster=auto" not in ch._argv()
    far_homes.record("other", "linux")
    mux._FAR_PLATFORM.clear()
    assert "ControlMaster=auto" in mux.channel("other", key="k", host="other", direct=True)._argv()


def test_a_mirror_entrys_far_pid_domain_decides_when_no_record_exists(mux, far_homes):
    far_homes.session("1.json", box="machine-a", pidDomain="darwin", farPidDomain="win32:machine-a")
    assert mux.far_platform("machine-a") == "win32"
    far_homes.session("2.json", box="posixbox", pidDomain="win32:me", farPidDomain="linux")
    assert mux.far_platform("posixbox") == "posix"
    far_homes.session("3.json", box="nodomain", pidDomain="linux")
    assert mux.far_platform("nodomain") is None


def test_the_record_outranks_a_session_entry_and_the_answer_is_cached(mux, far_homes):
    far_homes.record("machine-a", "win32")
    far_homes.session("1.json", box="machine-a", farPidDomain="linux")
    assert mux.far_platform("machine-a") == "win32"
    far_homes.record("machine-a", "linux")
    assert mux.far_platform("machine-a") == "win32"


def test_an_unknown_answer_is_retried_after_its_ttl(mux, far_homes):
    assert mux.far_platform("late") is None
    far_homes.record("late", "linux")
    assert mux.far_platform("late") is None
    mux._FAR_PLATFORM["late"] = (None, 0.0)
    assert mux.far_platform("late") == "posix"


def test_the_record_file_name_is_sanitised_like_unimatrix_install(mux, far_homes):
    far_homes.record("a-b", "linux")
    assert mux.far_platform("a b") == "posix"


@pytest.mark.parametrize("box", ["a b", "a/b", "dp:1", "ünï", "plain-1.x_y"])
def test_the_record_file_name_matches_unimatrix_install_control_far_record_path(mux, far_homes, box):
    install = _load(BIN / "unimatrix-install.py", "unimatrix_install_parity")
    path = install.control_far_record_path(box)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"platform": "linux"}), encoding="utf-8")
    assert mux.far_platform(box) == "posix"


def test_broker_spawn_is_windowless_never_detached(mux, monkeypatch, tmp_path):
    # DETACHED_PROCESS overrides CREATE_NO_WINDOW: the broker loses its console and every console
    # grandchild opens a visible window on the operator's screen.
    seen = {}
    monkeypatch.setattr(mux.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(mux.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    ch = mux.BrokerChannel("box", "key", "host", tmp_path, spawn_broker=None)
    ch._spawn_detached()
    assert not seen["creationflags"] & 0x8
    assert seen["creationflags"] & mux._no_window() == mux._no_window()
