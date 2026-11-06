"""Behavior pins for the vendored unimatrix mirror-all, roster and reshape scripts."""
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]
REPO = BIN.parents[1]
NAMES = ("mirror-all", "roster", "reshape")
FAR_SOCK = "/tmp/cc-socks/4242.sock"


def load(name):
    path = BIN / f"unimatrix-{name}.py"
    spec = importlib.util.spec_from_file_location(f"unimatrix_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, pid, alive=True):
        self.pid = pid
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    return d


def test_endpoint_is_harness_shaped_on_every_platform():
    roster = load("roster")
    hexname = hashlib.md5(f"unimatrix-{FAR_SOCK}".encode()).hexdigest()
    assert roster.local_endpoint(FAR_SOCK, platform="win32") == "\\\\.\\pipe\\LOCAL\\cc-msg-" + hexname
    posix = roster.local_endpoint(FAR_SOCK, platform="linux")
    assert re.fullmatch(r"/tmp/cc-socks/cc-msg-[0-9a-f]{32}\.sock", posix)
    assert roster.local_endpoint(FAR_SOCK, platform="darwin") == posix


def test_key_name_lowercases_only_on_windows():
    roster = load("roster")
    ep = "\\\\.\\pipe\\LOCAL\\cc-msg-ABCDEF"
    assert roster.key_name(9, ep, platform="win32") == f"9.{hashlib.sha256(ep.lower().encode()).hexdigest()}.key"
    assert roster.key_name(9, ep, platform="linux") == f"9.{hashlib.sha256(ep.encode()).hexdigest()}.key"


def test_proc_start_posix_branch_is_the_ps_lstart_string(monkeypatch):
    roster = load("roster")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="Wed Nov  4 01:19:29 2026\n")

    monkeypatch.setattr(roster.subprocess, "run", fake_run)
    # The harness matches ps byte for byte: the padded one-digit day must survive.
    assert roster.proc_start(77, platform="darwin") == "Wed Nov  4 01:19:29 2026"
    assert seen["cmd"] == ["ps", "-o", "lstart=", "-p", "77"]


@pytest.mark.skipif(sys.platform != "win32", reason="GetProcessTimes is Windows-only")
def test_proc_start_windows_is_the_raw_filetime_string():
    roster = load("roster")
    import os
    stamp = roster.proc_start(os.getpid(), platform="win32")
    assert isinstance(stamp, str) and stamp.isdigit()
    # FILETIME counts 100 ns ticks since 1601: any real process is past year 2000.
    assert int(stamp) > 125_000_000_000_000_000


def test_pid_domain_per_platform():
    roster = load("roster")
    assert roster.pid_domain("darwin") == "darwin"
    assert roster.pid_domain("linux") == "linux"
    assert re.fullmatch(r"win32:.+", roster.pid_domain("win32"))


def test_bridge_cmd_is_relay_mode_with_remote_and_key():
    roster = load("roster")
    cmd = roster.bridge_cmd("ep", "b.log", "farbox", "k")
    assert cmd[2:5] == ["ep", "relay", "b.log"] and cmd[-4:] == ["--remote", "farbox", "--key", "k"]
    with_leg = roster.bridge_cmd("ep", "b.log", "farbox", "k", 18991, "inbox-ep")
    assert with_leg[5:7] == ["18991", "inbox-ep"]


def test_tree_kill_reaches_a_grandchild(tmp_path):
    roster = load("roster")
    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(g.pid))\n"
        "time.sleep(60)\n"
    )
    parent = roster.spawn([sys.executable, "-c", code])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.1)
        grandchild = int(pidfile.read_text())
        assert roster.pid_alive(grandchild)
        roster.tree_kill(parent.pid)
        for _ in range(50):
            if not roster.pid_alive(grandchild):
                break
            time.sleep(0.1)
        assert not roster.pid_alive(grandchild)
    finally:
        roster.tree_kill(parent.pid)
        parent.wait(timeout=10)


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX-only")
def test_spawn_starts_children_in_their_own_process_group():
    import os
    roster = load("roster")
    p = roster.spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(p.pid) == p.pid != os.getpgrp()
    finally:
        roster.tree_kill(p.pid)
        p.wait(timeout=10)


def test_bridge_cmd_carries_the_tunnel_alias_only_when_set():
    roster = load("roster")
    cmd = roster.bridge_cmd("ep", "b.log", "farbox", "k", tunnel_remote="farbox-wg")
    assert cmd[-2:] == ["--tunnel-remote", "farbox-wg"] and cmd[-6:-2] == ["--remote", "farbox", "--key", "k"]
    assert "--tunnel-remote" not in roster.bridge_cmd("ep", "b.log", "farbox", "k")


def test_pick_remote_uses_the_lan_alias_when_it_answers():
    roster = load("roster")
    probed = []
    assert roster.pick_remote("lan", "wg", reachable=lambda a: probed.append(a) or True) == "lan"
    assert probed == ["lan"]


def test_pick_remote_falls_back_to_the_tunnel_only_when_the_lan_is_unreachable():
    roster = load("roster")
    assert roster.pick_remote("lan", "wg", reachable=lambda a: False) == "wg"


def test_pick_remote_without_a_tunnel_alias_never_probes_and_never_needs_the_tunnel():
    roster = load("roster")

    def boom(alias):
        raise AssertionError("probed")

    assert roster.pick_remote("lan", None, reachable=boom) == "lan"
    assert roster.pick_remote("lan", "lan", reachable=boom) == "lan"


def test_lan_reachable_is_a_tcp_connect_to_the_endpoint_ssh_resolves():
    roster = load("roster")
    seen = []

    class Sock:
        def close(self):
            seen.append("closed")

    def connect(ep, timeout):
        seen.append((ep, timeout))
        return Sock()

    assert roster.lan_reachable("lan", resolve=lambda a: ("host", 2222), connect=connect)
    assert seen == [(("host", 2222), roster.PROBE_SECONDS), "closed"]

    def refuse(ep, timeout):
        raise OSError("timed out")

    assert not roster.lan_reachable("lan", resolve=lambda a: ("host", 22), connect=refuse)
    assert not roster.lan_reachable("lan", resolve=lambda a: None, connect=connect)


def test_stable_remote_prefers_the_lan_alias_and_skips_mdns_when_it_answers():
    roster = load("roster")

    def boom(*a, **k):
        raise AssertionError("mDNS leg ran")

    assert roster.stable_remote("box", reachable=lambda a: a == "box-lan", fields=boom, connect=boom) == "box-lan"
    assert roster.stable_remote("me@box", reachable=lambda a: a == "me@box-lan", fields=boom, connect=boom) == "me@box-lan"


def test_stable_remote_falls_back_to_dot_local_with_the_resolved_user():
    roster = load("roster")
    dialled = []

    class Sock:
        def close(self):
            pass

    def connect(ep, timeout):
        dialled.append(ep)
        return Sock()

    got = roster.stable_remote("box", reachable=lambda a: False, fields=lambda a: {"user": "kim", "port": "22"},
                               connect=connect)
    assert got == "kim@box.local" and dialled == [("box.local", 22)]


def test_stable_remote_is_none_when_nothing_answers_or_the_port_is_custom():
    roster = load("roster")

    def refuse(ep, timeout):
        raise OSError("no mDNS")

    assert roster.stable_remote("box", reachable=lambda a: False, fields=lambda a: {"port": "22"}, connect=refuse) is None
    assert roster.stable_remote("box", reachable=lambda a: False, fields=lambda a: {"port": "2222"},
                                connect=lambda *a, **k: pytest.fail("dialled a custom port")) is None
    assert roster.stable_remote("box-lan", reachable=lambda a: pytest.fail("probed -lan-lan"),
                                fields=lambda a: {}, connect=refuse) is None


def test_stable_remote_spends_one_ssh_g_across_both_legs():
    roster = load("roster")
    asked = []

    def fields(a):
        asked.append(a)
        return {"hostname": "10.0.0.9", "port": "22", "user": "kim"}

    def connect(ep, timeout):
        if ep[0] != "box.local":
            raise OSError("lan down")

        class Sock:
            def close(self):
                pass

        return Sock()

    assert roster.stable_remote("box", fields=fields, connect=connect) == "kim@box.local"
    assert asked == ["box-lan"]
    asked.clear()
    assert roster.stable_remote("me@box", fields=fields, connect=connect) == "me@box.local"
    assert asked == ["me@box-lan"]


def test_fetch_roster_dials_the_lan_alias_by_default_and_the_tunnel_when_the_lan_is_down(monkeypatch):
    roster = load("roster")
    argvs = []

    def run(argv, **kw):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(roster.subprocess, "run", run)
    monkeypatch.setattr(roster, "lan_reachable", lambda alias: True)
    roster.fetch_roster("lan", "k", "wg")
    assert "lan" in argvs[-1] and "wg" not in argvs[-1]
    monkeypatch.setattr(roster, "lan_reachable", lambda alias: False)
    roster.fetch_roster("lan", "k", "wg")
    assert "wg" in argvs[-1] and "lan" not in argvs[-1]


def test_tunnel_alias_comes_from_the_flag_else_the_environment(monkeypatch):
    roster = load("roster")
    monkeypatch.delenv(roster.TUNNEL_ENV, raising=False)
    assert roster.parse_args(["log.txt", "--remote", "lan"]).tunnel_remote is None
    monkeypatch.setenv(roster.TUNNEL_ENV, "wg-env")
    assert roster.parse_args(["log.txt", "--remote", "lan"]).tunnel_remote == "wg-env"
    assert roster.parse_args(["log.txt", "--remote", "lan", "--tunnel-remote", "wg"]).tunnel_remote == "wg"
    mirror_all = load("mirror-all")
    assert mirror_all.parse_args(["log.txt", "--remote", "lan"]).tunnel_remote == "wg-env"
    cmd = mirror_all.inbox_cmd({"pid": 1, "messagingSocketPath": "ep"}, "n", "d", "lan", "k", "wg")
    assert cmd[-2:] == ["--tunnel-remote", "wg"]


def test_roster_mirror_writes_entry_and_key_the_harness_accepts(sessions, monkeypatch):
    roster = load("roster")
    ep = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "a" * 32
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "spawn", lambda cmd: FakeProc(31337))
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: "132000000000000000")
    monkeypatch.setattr(roster.time, "sleep", lambda s: None)
    monkeypatch.setattr(roster, "local_endpoint", lambda sock, platform=None: ep)
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: True)
    cfg = roster.parse_args(["bridge.log", "--remote", "farbox", "--key", "k"])
    row = {"entry": {"pid": 4242, "name": "far-1", "procStart": "x", "pidDomain": "darwin",
                     "messagingSocketPath": FAR_SOCK}, "key": "SECRETKEY"}
    lines = []
    b = roster.mirror(row, cfg, lines.append)
    assert b.pid == 31337
    entry = json.loads((sessions / "31337.json").read_text())
    assert entry["procStart"] == "132000000000000000" and isinstance(entry["procStart"], str)
    assert entry["messagingSocketPath"] == ep and entry["macSocketPath"] == FAR_SOCK
    key_file = sessions / roster.key_name(31337, ep, platform="win32")
    assert key_file.read_text() == "SECRETKEY"
    assert all("SECRETKEY" not in line for line in lines)


def _old_relay_roster(box):
    """What a relay without its re-export filter emits: every registry entry, mirrors included."""
    return [{"entry": e, "key": "K"} for e in box]


def _fake_box(prefix, n):
    return [{"pid": 1000 + i, "name": f"{prefix}-{i}", "messagingSocketPath": f"/tmp/cc-socks/{prefix}{i}.sock"}
            for i in range(n)]


def test_two_fake_boxes_sync_to_n_plus_m_rows_without_echoes(tmp_path, monkeypatch):
    roster = load("roster")
    n, m = 3, 2
    a_local, b_local = _fake_box("a", n), _fake_box("b", m)
    # Each box holds a mirror of every session on the other box.
    a_mirrors = [{**e, "pid": 5000 + i, "messagingSocketPath": f"/tmp/cc-socks/cc-msg-a{i}.sock",
                  roster.FAR_SOCKET_FIELD: e["messagingSocketPath"], "farPid": e["pid"]}
                 for i, e in enumerate(b_local)]
    b_mirrors = [{**e, "pid": 6000 + i, "messagingSocketPath": f"/tmp/cc-socks/cc-msg-b{i}.sock",
                  roster.FAR_SOCKET_FIELD: e["messagingSocketPath"], "farPid": e["pid"]}
                 for i, e in enumerate(a_local)]
    # A mirror recorded without the far-socket field, only marked by its relay.
    b_mirrors.append({**a_local[0], "pid": 6100, "messagingSocketPath": "pipe-x", "unimatrixMirror": True})
    for_a = roster.mirrorable(_old_relay_roster(b_local + b_mirrors), set(), set())
    assert sorted(r["entry"]["name"] for r in for_a.values()) == sorted(e["name"] for e in b_local)
    for_b = roster.mirrorable(_old_relay_roster(a_local + a_mirrors), set(), set())
    assert len(for_a) + len(a_local) == n + m and len(for_b) + len(b_local) == n + m


def test_mirrorable_drops_skipped_sockets_and_names_a_live_inbound_mirror_reaches():
    roster = load("roster")
    rows = _old_relay_roster(_fake_box("b", 3))
    got = roster.mirrorable(rows, {"/tmp/cc-socks/b0.sock"}, {"b-1"})
    assert [r["entry"]["name"] for r in got.values()] == ["b-2"]


def test_inbound_names_counts_only_live_unimatrix_mirror_entries(sessions, monkeypatch):
    roster = load("roster")
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid != 2)
    for pid, extra in ((1, {"unimatrixMirror": True}), (2, {"unimatrixMirror": True}), (3, {}), (4, {"macSocketPath": "x"})):
        (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "name": f"n{pid}", **extra}))
    (sessions / "5.json").write_text("not json")
    assert roster.inbound_names() == {"n1"}


def test_mirror_records_the_far_pid_for_a_win32_pipe_far_socket(sessions, monkeypatch):
    roster = load("roster")
    pipe = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "c" * 32
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "spawn", lambda cmd: FakeProc(31338))
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: "1")
    monkeypatch.setattr(roster.time, "sleep", lambda s: None)
    monkeypatch.setattr(roster, "_is_win", lambda platform=None: True)
    cfg = roster.parse_args(["bridge.log", "--remote", "farbox", "--key", "k"])
    row = {"entry": {"pid": 7777, "name": "far-w", "procStart": "x", "pidDomain": "win32:box",
                     "messagingSocketPath": pipe}, "key": "K"}
    roster.mirror(row, cfg, lambda m: None)
    assert json.loads((sessions / "31338.json").read_text())["farPid"] == 7777


@pytest.mark.parametrize("far_socket,stored,expect", [
    ("\\\\.\\pipe\\LOCAL\\cc-msg-" + "d" * 32, 7777, "7777"),
    ("/tmp/cc-socks/4242.sock", None, "4242"),
])
def test_relay_send_addresses_the_far_session_by_stored_far_pid(tmp_path, monkeypatch, far_socket, stored, expect):
    import types
    spec = importlib.util.spec_from_file_location("unimatrix_bridge", BIN / "unimatrix-bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    d = tmp_path / "sessions"
    d.mkdir()
    entry = {"messagingSocketPath": "/m.sock", "macSocketPath": far_socket}
    if stored:
        entry["farPid"] = stored
    (d / "1.json").write_text(json.dumps(entry))
    monkeypatch.setattr(bridge, "SESSIONS", str(d))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    bridge.cfg = types.SimpleNamespace(endpoint="/m.sock", key="k", remote="lan", tunnel_remote=None, port=None)
    bridge.log = open(tmp_path / "b.log", "a", encoding="utf-8")
    argvs = []
    monkeypatch.setattr(bridge, "pick_remote", lambda lan, tun: lan)
    monkeypatch.setattr(bridge.subprocess, "run", lambda argv, **kw: argvs.append(argv) or subprocess.CompletedProcess(argv, 0, b"", b""))

    class Conn:
        chunks = [b'{"from": "uds:/s.sock", "message": "hi"}\n', b""]

        def read(self):
            return self.chunks.pop(0)

    bridge.relay_send(Conn())
    assert argvs[-1][-1] == f"send {expect}"


def test_roster_sync_unmirrors_a_vanished_far_session(sessions, monkeypatch):
    roster = load("roster")
    killed = []
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(roster, "tree_kill", killed.append)
    (sessions / "55.json").write_text("{}")
    (sessions / "55.deadbeef.key").write_text("k")
    (sessions / "56.json").write_text("{}")
    bridges = {"/far/a.sock": FakeProc(55)}
    roster.sync(bridges, {}, None, lambda m: None)
    assert bridges == {} and killed == [55]
    assert sorted(p.name for p in sessions.iterdir()) == ["56.json"]


def test_roster_requires_remote():
    with pytest.raises(SystemExit):
        load("roster").parse_args(["log.txt"])


def test_mirror_all_selects_only_live_local_named_sessions(sessions, monkeypatch):
    mirror_all = load("mirror-all")
    monkeypatch.setattr(mirror_all.R, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(mirror_all.R, "pid_alive", lambda pid: pid != 3)

    def put(pid, **kw):
        (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "messagingSocketPath": f"ep{pid}", **kw}))

    put(1, name="local-a")
    put(2, name="skipped")
    put(3, name="dead")
    put(4, name="mirror-of-far", macSocketPath=FAR_SOCK)
    put(5)
    put(6, name="../escape")
    (sessions / "7.json").write_text("not json")
    live = mirror_all.live_sessions({"skipped"})
    assert sorted(live) == ["local-a"]


def test_mirror_all_starts_client_with_deliver_endpoint_and_kills_it_on_exit(tmp_path, monkeypatch):
    mirror_all = load("mirror-all")
    started, killed = [], []

    def fake_spawn(cmd):
        started.append(cmd)
        return FakeProc(900 + len(started))

    monkeypatch.setattr(mirror_all.R, "spawn", fake_spawn)
    monkeypatch.setattr(mirror_all.R, "tree_kill", killed.append)
    cfg = mirror_all.parse_args([str(tmp_path / "m.log"), "--remote", "farbox", "--key", "k"])
    clients = {}
    live = {"local-a": {"pid": 11, "name": "local-a", "messagingSocketPath": "ep11"}}
    mirror_all.sync(clients, live, cfg, lambda m: None)
    cmd = started[0]
    assert cmd[2:4] == ["11", "local-a"] and cmd[5] == "ep11" and cmd[-4:] == ["--remote", "farbox", "--key", "k"]
    mirror_all.sync(clients, live, cfg, lambda m: None)
    assert len(started) == 1
    mirror_all.sync(clients, {}, cfg, lambda m: None)
    assert clients == {} and killed == [901]


def test_mirror_all_restarts_a_client_whose_session_pid_changed(tmp_path, monkeypatch):
    mirror_all = load("mirror-all")
    killed, n = [], [0]

    def fake_spawn(cmd):
        n[0] += 1
        return FakeProc(700 + n[0])

    monkeypatch.setattr(mirror_all.R, "spawn", fake_spawn)
    monkeypatch.setattr(mirror_all.R, "tree_kill", killed.append)
    cfg = mirror_all.parse_args([str(tmp_path / "m.log"), "--remote", "farbox"])
    clients = {}
    mirror_all.sync(clients, {"a": {"pid": 1, "messagingSocketPath": "e"}}, cfg, lambda m: None)
    mirror_all.sync(clients, {"a": {"pid": 2, "messagingSocketPath": "e"}}, cfg, lambda m: None)
    assert killed == [701] and clients["a"][0] == 2


def test_reshape_repoints_entry_key_and_stops_the_old_bridge(sessions, monkeypatch):
    reshape = load("reshape")
    R = reshape.R
    ep = "\\\\.\\pipe\\LOCAL\\cc-msg-" + "b" * 32
    killed, cmds = [], []
    monkeypatch.setattr(R, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(R, "tree_kill", killed.append)
    monkeypatch.setattr(R, "spawn", lambda cmd: cmds.append(cmd) or FakeProc(8080))
    monkeypatch.setattr(R, "proc_start", lambda pid, platform=None: "132111")
    monkeypatch.setattr(R, "local_endpoint", lambda sock, platform=None: ep)
    monkeypatch.setattr(R, "key_name", lambda pid, e, platform=None: f"{pid}.newhash.key")
    monkeypatch.setattr(reshape.sys, "platform", "win32")
    (sessions / "50.json").write_text(json.dumps({"pid": 50, "name": "far-1", "macSocketPath": FAR_SOCK,
                                                  "messagingSocketPath": "old-readable-name"}))
    (sessions / "50.oldhash.key").write_text("KEY")
    cfg = reshape.parse_args(["50", "b.log", "--return-port", "18991", "--inbox", "inbox-ep",
                              "--remote", "farbox", "--key", "k"])
    out = reshape.reshape(cfg, settle_seconds=0)
    assert killed == [50] and "50 -> 8080" in out
    assert cmds[0][2:5] == [ep, "relay", "b.log"] and cmds[0][5:7] == ["18991", "inbox-ep"]
    entry = json.loads((sessions / "8080.json").read_text())
    assert entry["pid"] == 8080 and entry["procStart"] == "132111" and entry["messagingSocketPath"] == ep
    assert (sessions / "8080.newhash.key").read_text() == "KEY"
    assert sorted(p.name for p in sessions.iterdir()) == ["8080.json", "8080.newhash.key"]


def test_reshape_refuses_before_killing_anything(sessions, monkeypatch):
    reshape = load("reshape")
    killed = []
    monkeypatch.setattr(reshape.R, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(reshape.R, "tree_kill", killed.append)
    (sessions / "50.json").write_text(json.dumps({"pid": 50, "macSocketPath": FAR_SOCK}))
    cfg = reshape.parse_args(["50", "b.log", "--remote", "farbox"])
    with pytest.raises(SystemExit, match="key file"):
        reshape.reshape(cfg, settle_seconds=0)
    (sessions / "50.json").write_text(json.dumps({"pid": 50}))
    with pytest.raises(SystemExit, match="not a mirror"):
        reshape.reshape(cfg, settle_seconds=0)
    assert killed == []


def test_reshape_return_leg_flags_go_together():
    with pytest.raises(SystemExit):
        load("reshape").parse_args(["50", "b.log", "--return-port", "1", "--remote", "farbox"])


@pytest.mark.parametrize("name", NAMES)
def test_scripts_have_no_top_level_win32_import_or_powershell(name):
    src = (BIN / f"unimatrix-{name}.py").read_text(encoding="utf-8")
    assert not [l for l in src.splitlines() if re.match(r"(import|from)\s+(pywintypes|win32)", l)]
    assert "powershell" not in src.lower()
    assert not list(BIN.glob("unimatrix-*.ps1"))


IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
USER_AT_HOST = re.compile(r"\b[A-Za-z0-9_.-]+@[A-Za-z0-9][A-Za-z0-9.-]*\b")


@pytest.mark.parametrize("name", NAMES)
def test_no_ip_literal_or_user_at_host(name):
    src = (BIN / f"unimatrix-{name}.py").read_text(encoding="utf-8")
    assert IPV4.findall(src) == []
    assert USER_AT_HOST.findall(src) == []


def test_publish_targets_ship_every_unimatrix_script_and_test():
    targets = REPO / "setup" / "publish-targets.portable"
    if not targets.exists():
        pytest.skip("publish targets are not part of the released plugin")
    row = next(l for l in targets.read_text(encoding="utf-8").splitlines()
               if l.startswith("claude-klabauter-coordinator-bin|mirror|"))
    listed = row.split("|")[-1].split(",")
    assert not [e for e in listed if e.startswith("!") and "unimatrix" in e]
    for p in BIN.glob("unimatrix-*.py"):
        assert p.name in listed, p.name


def test_roster_failure_names_the_ssh_reason(monkeypatch):
    roster = load("roster")
    monkeypatch.setattr(roster, "lan_reachable", lambda alias: True)
    monkeypatch.setattr(roster.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 255, b"", b"debug\nssh: connect to host lan port 22: Operation timed out\n"))
    with pytest.raises(OSError, match=r"rc=255: ssh: connect to host lan port 22: Operation timed out"):
        roster.fetch_roster("lan", "k", None)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM handler is POSIX-only")
@pytest.mark.parametrize("name", ["roster", "mirror-all"])
def test_a_sigterm_stop_is_logged_and_exits_nonzero(tmp_path, name):
    # A clean 0 on SIGTERM dropped every mirror with no trace; the stop must say so and fail.
    log = tmp_path / "sup.log"
    p = subprocess.Popen([sys.executable, str(BIN / f"unimatrix-{name}.py"), str(log),
                          "--remote", "unreachable.invalid", "--key", str(tmp_path / "k")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not log.exists():
            time.sleep(0.05)
        time.sleep(1)  # the handler is installed just after the log opens
        p.terminate()
        assert p.wait(timeout=10) == 143
        assert "stopped by signal 15" in log.read_text(encoding="utf-8")
    finally:
        # The supervisor polls forever: a failed or interrupted assertion must never leave it running.
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=10)
