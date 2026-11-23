"""Behavior pins for the unimatrix far-box autolink launcher: ssh and process spawns are faked."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProc:
    def __init__(self, pid):
        self.pid = pid


class Spawner:
    def __init__(self):
        self.cmds = []
        self.next_pid = 1000

    def __call__(self, cmd):
        self.cmds.append(cmd)
        self.next_pid += 1
        return FakeProc(self.next_pid)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    (home / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    engine = tmp_path / "engine"
    (engine / "coordinator_core").mkdir(parents=True)
    (engine / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(engine))
    monkeypatch.delenv("UNIMATRIX_TUNNEL_REMOTE", raising=False)
    monkeypatch.setenv("UNIMATRIX_AUTOLINK_NO_ROUTER", "1")
    monkeypatch.setenv("UNIMATRIX_TEST_ALLOWED_BOXES", "machine-a a@b fallbackbox")
    al = load("unimatrix-autolink")
    real_ssh = al.ssh_identity_file
    real_pull = al.pull_membership
    monkeypatch.setattr(al, "pull_membership", lambda *a, **k: False)
    monkeypatch.setattr(al, "live_name", lambda *a, **k: None)
    monkeypatch.setattr(al, "ssh_config_aliases", lambda *a, **k: [])
    monkeypatch.setattr(al, "_known_hosts_entries", lambda *a, **k: [])
    monkeypatch.setattr(al, "ssh_identity_file", lambda roster, alias, run=None: None)
    monkeypatch.setattr(al, "pin_key_value", lambda roster, run=None: None)
    monkeypatch.setattr(al, "ssh_hostname", lambda roster, alias, run=None: None)
    roster = al._roster()
    live = set()
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(roster, "proc_start", lambda pid: f"start-{pid}")
    liveness = al._liveness()
    monkeypatch.setattr(liveness, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(liveness, "proc_start", lambda pid, platform=None: roster.proc_start(pid))

    class E:
        pass

    e = E()
    e.real_ssh_identity_file = real_ssh
    e.real_pull_membership = real_pull
    e.home, e.al, e.roster, e.live = home, al, roster, live
    e.sdir = home / "unimatrix"
    e.key = Path(roster.default_key())
    return e


def add_key(e):
    e.key.parent.mkdir(parents=True)
    e.key.write_text("k")


def ok(roster, alias, key):
    return None


def log_lines(env):
    return (env.sdir / "autolink.log").read_text().splitlines()


def test_unconfigured_prints_nothing_and_logs_the_reason(env, capsys):
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok)
    out = capsys.readouterr()
    assert out.out == "" and out.err == "" and sp.cmds == []
    lines = log_lines(env)
    assert len(lines) == 1 and "unimatrix.far_boxes unreadable" in lines[0] and "no supervisors started" in lines[0]


def test_registry_key_unset_logs_the_no_alias_line(env):
    reader = env.home / "bin" / "_machine_local.py"
    reader.write_text("import sys\nsys.exit(1)\n")
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok)
    lines = log_lines(env)
    assert len(lines) == 1 and lines[0].endswith(" no unimatrix.far_boxes configured; no supervisors started")
    assert json.loads((env.sdir / "autolink-boxes.json").read_text()) == {"aliases": []}


def test_real_reader_not_found_message_still_writes_the_empty_cache(env):
    reader = env.home / "bin" / "_machine_local.py"
    reader.write_text("import sys\nsys.stderr.write(\"machine-local: key 'unimatrix.far_boxes' not found in registry\\n\")\nsys.exit(1)\n")
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok)
    assert json.loads((env.sdir / "autolink-boxes.json").read_text()) == {"aliases": []}


def test_reader_crash_exiting_1_with_traceback_writes_no_cache(env):
    reader = env.home / "bin" / "_machine_local.py"
    reader.write_text("import sys\nsys.stderr.write('Traceback: boom')\nsys.exit(1)\n")
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok)
    assert "rc=1" in log_lines(env)[0] and "boom" in log_lines(env)[0]
    assert not (env.sdir / "autolink-boxes.json").exists()


def test_reader_failure_logs_rc_and_stderr_and_writes_no_cache(env):
    reader = env.home / "bin" / "_machine_local.py"
    reader.write_text("import sys\nsys.stderr.write('registry boom')\nsys.exit(2)\n")
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok)
    lines = log_lines(env)
    assert len(lines) == 1 and "rc=2" in lines[0] and "registry boom" in lines[0]
    assert not (env.sdir / "autolink-boxes.json").exists()


def test_reader_timeout_writes_no_cache(env, monkeypatch):
    (env.home / "bin" / "_machine_local.py").write_text("")

    def slow(*a, **kw):
        raise subprocess.TimeoutExpired("reader", 30)

    monkeypatch.setattr(env.al.subprocess, "run", slow)
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok)
    assert "unreadable (TimeoutExpired)" in log_lines(env)[0]
    assert not (env.sdir / "autolink-boxes.json").exists()


def test_missing_reader_writes_no_cache(env):
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok)
    assert not (env.sdir / "autolink-boxes.json").exists()


def test_registry_value_with_only_unsafe_aliases_logs_it(env):
    reader = env.home / "bin" / "_machine_local.py"
    reader.write_text("print('-oProxyCommand=x')\n")
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok)
    lines = log_lines(env)
    assert sp.cmds == [] and len(lines) == 1 and "lists no valid alias" in lines[0]


def test_no_far_box_with_key_present_logs_and_spawns_nothing(env, capsys):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=[], spawn=sp, probe_fn=ok)
    assert sp.cmds == [] and capsys.readouterr().out == ""
    assert "no unimatrix.far_boxes configured; no supervisors started" in log_lines(env)[0]


def test_far_box_without_relay_key_logs_one_line_and_prints_nothing(env, capsys):
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert sp.cmds == [] and capsys.readouterr().out == ""
    lines = (env.sdir / "autolink.log").read_text().splitlines()
    assert len(lines) == 1 and "no Unimatrix-Zero key" in lines[0]


def test_default_key_falls_back_to_the_group_em_home(env):
    gem = env.home / "group-em" / ".secrets" / "ssh" / "unimatrix-zero"
    gem.parent.mkdir(parents=True)
    gem.write_text("k")
    assert env.roster.default_key() == str(gem)
    add_key(env)  # the settings-home key wins when both exist
    assert env.roster.default_key() == str(env.key)


def test_main_exits_zero_and_prints_nothing_when_unconfigured(env, capsys):
    assert env.al.main() == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_reader_missing_key_yields_no_boxes(env):
    assert env.al.read_far_boxes(env.roster) == []


def test_reader_parses_comma_list_and_drops_unsafe_aliases(env):
    reader = env.home / "bin" / "_machine_local.py"
    reader.write_text("import sys\nprint('machine-a, -oProxyCommand=x, a@b ,machine-a')\n")
    assert env.al.read_far_boxes(env.roster) == ["machine-a", "a@b"]


def test_configured_box_gets_one_bridge_and_one_inbox_with_relay_key_only(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert len(sp.cmds) == 2
    scripts = sorted(Path(c[1]).name for c in sp.cmds)
    assert scripts == ["unimatrix-mirror-all.py", "unimatrix-roster.py"]
    for c in sp.cmds:
        assert c[c.index("--key") + 1] == str(env.key)
        assert c[c.index("--remote") + 1] == "machine-a"
        assert c.count("--key") == 1
    rec = json.loads((env.sdir / "autolink-machine-a.json").read_text())
    assert set(rec) == {"bridge", "inbox", "box"} and rec["box"] == "machine-a"
    assert not (env.sdir / "autolink-machine-a.lock").exists()


def test_second_run_with_live_supervisors_spawns_nothing(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    env.live.update({1001, 1002})
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert len(sp.cmds) == 2


def test_dead_supervisor_is_replaced_and_live_one_is_kept(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    rec = json.loads((env.sdir / "autolink-machine-a.json").read_text())
    env.live.add(rec["bridge"]["pid"])
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert len(sp.cmds) == 3
    assert Path(sp.cmds[2][1]).name == "unimatrix-mirror-all.py"


def test_reused_pid_with_a_different_start_stamp_is_not_live(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    env.live.update({1001, 1002})
    env.roster.proc_start = lambda pid: "someone-else"
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert len(sp.cmds) == 4


def test_unreachable_box_logs_one_line_spawns_nothing_and_prints_nothing(env, capsys):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, stable_fn=lambda a: None,
               probe_fn=lambda r, a, k, remote=None: "rc=255: Connection timed out")
    assert sp.cmds == [] and capsys.readouterr().out == ""
    lines = (env.sdir / "autolink.log").read_text().splitlines()
    assert len(lines) == 1 and "machine-a unreachable" in lines[0]
    assert not (env.sdir / "autolink-machine-a.json").exists()


def stale_ip_probe(good):
    """Probe fake: only the names in `good` answer; records every remote dialled."""
    seen = []

    def fn(roster, alias, key, remote=None):
        seen.append(remote or alias)
        return None if (remote or alias) in good else "rc=255: No route to host"

    fn.seen = seen
    return fn


class _Sock:
    def close(self):
        pass


def test_stale_pinned_ip_links_through_the_first_stable_name_that_answers(env):
    add_key(env)
    sp = Spawner()
    probe_fn = stale_ip_probe({"machine-a-lan"})
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn,
               stable_fn=lambda a: "machine-a-lan")
    assert probe_fn.seen == ["machine-a", "machine-a-lan"]
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"machine-a-lan"} and len(sp.cmds) == 2
    assert "linked via machine-a-lan" in (env.sdir / "autolink.log").read_text()


def test_stable_name_that_fails_the_ssh_probe_is_not_linked(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=stale_ip_probe(set()),
               stable_fn=lambda a: "machine-a.local")
    assert sp.cmds == []
    assert "machine-a unreachable" in (env.sdir / "autolink.log").read_text()


def test_healthy_primary_never_resolves_a_stable_name(env):
    add_key(env)
    sp = Spawner()

    def boom(alias):
        raise AssertionError("stable-name resolution ran on the healthy path")

    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=stale_ip_probe({"machine-a"}), stable_fn=boom)
    assert len(sp.cmds) == 2


def test_default_run_resolves_stale_alias_through_roster_stable_remote(env, monkeypatch):
    add_key(env)
    sp = Spawner()
    monkeypatch.setattr(env.roster, "ssh_fields", lambda a: {"hostname": "10.1.1.1", "port": "22"})
    monkeypatch.setattr(env.roster.socket, "create_connection",
                        lambda ep, timeout: pytest.fail("mDNS leg ran") if ep[0].endswith(".local") else _Sock())
    probe_fn = stale_ip_probe({"machine-a-lan"})
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn)
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"machine-a-lan"}


def test_default_run_falls_through_to_dot_local_when_the_lan_alias_is_down(env, monkeypatch):
    add_key(env)
    sp = Spawner()
    monkeypatch.setattr(env.roster, "ssh_fields", lambda a: {"hostname": "10.1.1.1", "port": "22", "user": "kim"})

    def connect(ep, timeout):
        if ep[0] == "machine-a.local":
            return _Sock()
        raise OSError("down")

    monkeypatch.setattr(env.roster.socket, "create_connection", connect)
    probe_fn = stale_ip_probe({"kim@machine-a.local"})
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn)
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"kim@machine-a.local"}


def test_one_peer_failure_does_not_stop_the_next(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["down", "up"], spawn=sp, stable_fn=lambda a: None,
               probe_fn=lambda r, a, k, remote=None: "rc=255" if a == "down" else None)
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"up"}


def test_spawn_exception_is_logged_not_raised(env):
    add_key(env)

    def boom(cmd):
        raise OSError("nope")

    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=boom, probe_fn=ok)
    assert "link failed" in (env.sdir / "autolink.log").read_text()
    assert not (env.sdir / "autolink-machine-a.lock").exists()


def test_held_lock_blocks_a_concurrent_spawn(env):
    add_key(env)
    env.sdir.mkdir(parents=True)
    (env.sdir / "autolink-machine-a.lock").write_text("")
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert sp.cmds == []
    assert "lock held" in log_lines(env)[0]


def test_stale_lock_is_taken_over(env):
    add_key(env)
    env.sdir.mkdir(parents=True)
    lock = env.sdir / "autolink-machine-a.lock"
    lock.write_text("")
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert len(sp.cmds) == 2


def test_probe_ssh_argv_carries_only_the_relay_key(env):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    assert env.al.probe(env.roster, "machine-a", "/k/relay", run=fake_run) is None
    cmd = seen[0]
    # -n: a pwsh-DefaultShell far side never answers while the caller's stdin stays open
    assert cmd[:4] == ["ssh", "-n", "-i", "/k/relay"]
    assert cmd.count("-i") == 1
    assert ["-o", "IdentitiesOnly=yes"] == cmd[4:6]
    assert "BatchMode=yes" in cmd and cmd[-2:] == ["machine-a", "roster"]


def test_probe_reports_failure_reason(env):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 255, b"", b"ssh: connect refused\n")

    assert "rc=255" in env.al.probe(env.roster, "machine-a", "/k", run=fake_run)

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert env.al.probe(env.roster, "machine-a", "/k", run=timeout) == "TimeoutExpired"


def test_tunnel_alias_is_forwarded_to_supervisors(env, monkeypatch):
    add_key(env)
    monkeypatch.setenv("UNIMATRIX_TUNNEL_REMOTE", "machine-a-wg")
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    for c in sp.cmds:
        assert c[c.index("--tunnel-remote") + 1] == "machine-a-wg"


def write_key(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("k")
    return str(path)


def key_of(cmd):
    return cmd[cmd.index("--key") + 1]


def test_each_alias_gets_its_own_key_in_probe_and_both_supervisors(env, tmp_path):
    ka, kb = write_key(tmp_path / "ka"), write_key(tmp_path / "kb")
    keys = {"alpha": ka, "beta": kb}
    probed = []
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["alpha", "beta"], spawn=sp,
               probe_fn=lambda r, a, k: probed.append((a, k)),
               key_fn=lambda r, a: keys[a])
    assert probed == [("alpha", ka), ("beta", kb)]
    got = {(c[c.index("--remote") + 1], key_of(c)) for c in sp.cmds}
    assert got == {("alpha", ka), ("beta", kb)} and len(sp.cmds) == 4


def dns_reply(name, addrs, pointer=True):
    """A DNS response for `name`; answers use a compression pointer to the question name when `pointer`."""
    import struct
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    out = struct.pack(">6H", 0, 0x8400, 1, len(addrs), 0, 0) + q + struct.pack(">HH", 1, 1)
    for a in addrs:
        out += (b"\xc0\x0c" if pointer else q) + struct.pack(">HHIH", 1, 0x8001, 120, 4) + bytes(map(int, a.split(".")))
    return out


def test_parse_a_records_reads_compressed_and_plain_names_and_ignores_others():
    al = load("unimatrix-autolink")
    assert al.parse_a_records(dns_reply("Mac-mini.local", ["192.0.2.7", "192.0.2.8"]), "mac-mini.local") == ["192.0.2.7", "192.0.2.8"]
    assert al.parse_a_records(dns_reply("Mac-mini.local", ["192.0.2.7"], pointer=False), "Mac-mini.local") == ["192.0.2.7"]
    assert al.parse_a_records(dns_reply("other.local", ["192.0.2.9"]), "mac-mini.local") == []
    assert al.parse_a_records(b"\x00\x01garbage", "mac-mini.local") == []
    assert al.parse_a_records(dns_reply("a.local", ["192.0.2.7"])[:-2], "a.local") == []


def test_mdns_a_records_sends_one_query_to_the_group_and_returns_the_answer(env, monkeypatch):
    import socket as real
    sent = []

    class FakeSock:
        def __init__(self, *a):
            self.replies = [dns_reply("Mac-mini.local", ["192.0.2.5"])]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def settimeout(self, t):
            assert 0 < t <= env.al.MDNS_SECONDS

        def sendto(self, data, dest):
            sent.append((data, dest))

        def recv(self, n):
            if self.replies:
                return self.replies.pop()
            raise real.timeout()

    monkeypatch.setattr(env.al.socket, "socket", FakeSock)
    assert env.al.mdns_a_records("Mac-mini.local") == ["192.0.2.5"]
    assert len(sent) == 1 and sent[0][1] == ("224.0.0.251", 5353) and b"\x08Mac-mini\x05local\x00" in sent[0][0]


def test_mdns_a_records_with_no_answer_returns_empty_within_the_timeout(env, monkeypatch):
    import socket as real

    class Silent:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def settimeout(self, t):
            pass

        def sendto(self, *a):
            pass

        def recv(self, n):
            raise real.timeout()

    monkeypatch.setattr(env.al.socket, "socket", Silent)
    assert env.al.mdns_a_records("gone.local", timeout=0.05) == []


def test_live_name_takes_a_local_host_or_a_bare_hostname_and_refuses_an_ip():
    al = load("unimatrix-autolink")
    r = None
    assert al.live_name(r, "example-operator@Mac-mini.local") == "Mac-mini.local"
    assert al.live_name(r, "um01-macmini", lambda *a: "Mac-mini.local") == "Mac-mini.local"
    assert al.live_name(r, "machine-a", lambda *a: "machine-a") == "machine-a.local"
    assert al.live_name(r, "um01-macmini", lambda *a: "192.0.2.4") is None
    assert al.live_name(r, "x", lambda *a: "box.example.com") is None


@pytest.fixture
def live_env(env, monkeypatch):
    add_key(env)
    monkeypatch.setattr(env.al, "live_name", lambda *a, **k: "machine-a.local")
    monkeypatch.setattr(env.al, "_resolved_v4", lambda name: "192.0.2.1")
    monkeypatch.setattr(env.roster, "ssh_fields", lambda a: {"user": "kim"})
    return env


def test_probe_timeout_then_mdns_live_address_answers_and_both_addresses_are_logged(live_env):
    env, sp = live_env, Spawner()
    probe_fn = stale_ip_probe({"kim@192.0.2.3"})
    asked = []
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn, stable_fn=lambda a: None,
               mdns_fn=lambda name: asked.append(name) or ["192.0.2.1", "192.0.2.2", "192.0.2.3"])
    assert asked == ["machine-a.local"]
    assert probe_fn.seen == ["machine-a", "kim@192.0.2.2", "kim@192.0.2.3"]
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"kim@192.0.2.3"} and len(sp.cmds) == 2
    log = (env.sdir / "autolink.log").read_text()
    assert "192.0.2.1" in log and "192.0.2.3" in log and "linked via kim@192.0.2.3" in log


def test_every_live_address_timing_out_returns_the_original_reason_and_links_nothing(live_env):
    env, sp = live_env, Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=stale_ip_probe(set()),
               stable_fn=lambda a: None, mdns_fn=lambda name: ["192.0.2.2", "192.0.2.3"])
    assert sp.cmds == []
    log = (env.sdir / "autolink.log").read_text()
    assert "machine-a unreachable: rc=255: No route to host" in log and "192.0.2.1" in log and "192.0.2.3" in log


def test_mdns_is_asked_fresh_on_every_failed_attempt(live_env):
    env, asked = live_env, []

    def mdns(name):
        asked.append(name)
        return []

    for _ in range(2):
        env.al.mdns_fallback(env.roster, "machine-a", "/k", "rc=255", stale_ip_probe(set()), mdns)
    assert asked == ["machine-a.local", "machine-a.local"]


def _alias_env(env, monkeypatch, hostnames):
    add_key(env)
    monkeypatch.setattr(env.al, "ssh_config_aliases", lambda *a, **k: list(hostnames))
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: hostnames.get(a))
    return env


def test_recorded_name_fails_and_the_ssh_alias_naming_the_box_links_both_supervisors(env, monkeypatch):
    _alias_env(env, monkeypatch, {"other": "elsewhere.local", "boxalias": "Mini-Box.local"})
    sp, probe_fn = Spawner(), stale_ip_probe({"kim@boxalias"})
    env.al.run(roster=env.roster, boxes=["kim@mini-box.local"], spawn=sp, probe_fn=probe_fn,
               stable_fn=boom, mdns_fn=lambda name: [])
    assert probe_fn.seen == ["kim@mini-box.local", "kim@boxalias"]
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"kim@boxalias"} and len(sp.cmds) == 2
    assert "linked via kim@boxalias" in (env.sdir / "autolink.log").read_text()


def test_ssh_alias_whose_hostname_resolves_to_a_live_mdns_address_is_taken(env, monkeypatch):
    _alias_env(env, monkeypatch, {"boxalias": "box.lan"})
    monkeypatch.setattr(env.al, "live_name", lambda *a, **k: "mini-box.local")
    monkeypatch.setattr(env.roster, "ssh_fields", lambda a: {"user": ""})
    monkeypatch.setattr(env.al, "_resolved_v4", lambda name: "192.0.2.9" if name == "box.lan" else "192.0.2.1")
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["mini-box.local"], spawn=sp, probe_fn=stale_ip_probe({"boxalias"}),
               stable_fn=boom, mdns_fn=lambda name: ["192.0.2.9"] if name == "mini-box.local" else [])
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"boxalias"} and len(sp.cmds) == 2


def test_no_ssh_alias_naming_the_box_keeps_the_original_reason(env, monkeypatch):
    _alias_env(env, monkeypatch, {"other": "elsewhere.local"})
    sp, probe_fn = Spawner(), stale_ip_probe(set())
    env.al.run(roster=env.roster, boxes=["mini-box.local"], spawn=sp, probe_fn=probe_fn,
               stable_fn=lambda a: None, mdns_fn=lambda name: [])
    assert sp.cmds == [] and probe_fn.seen == ["mini-box.local"]
    assert "mini-box.local unreachable: rc=255: No route to host" in (env.sdir / "autolink.log").read_text()


def test_ssh_alias_that_also_fails_links_nothing_and_says_so(env, monkeypatch):
    _alias_env(env, monkeypatch, {"boxalias": "mini-box.local"})
    sp, probe_fn = Spawner(), stale_ip_probe(set())
    env.al.run(roster=env.roster, boxes=["mini-box.local"], spawn=sp, probe_fn=probe_fn,
               stable_fn=lambda a: None, mdns_fn=lambda name: [])
    assert sp.cmds == [] and probe_fn.seen == ["mini-box.local", "boxalias"]
    assert "ssh alias boxalias named the same box and did not answer" in (env.sdir / "autolink.log").read_text()


def _hostkey_env(env, monkeypatch, alias_key="ssh-ed25519 AAAAmini", known_name="Mac-mini.local"):
    _alias_env(env, monkeypatch, {"macmini": "192.168.0.175"})
    monkeypatch.setattr(env.al, "_known_hosts_entries", lambda *a, **k: [
        ([known_name], "ssh-ed25519 AAAAmini"), (["192.168.0.175", "macmini"], alias_key)])
    own = str(env.home / "unimatrix-zero-machine-a-to-macmini")
    monkeypatch.setattr(env.al, "ssh_identity_file", lambda roster, a, run=None: own if a == "macmini" else None)
    return own


def _keyed_probe(good):
    seen = []

    def fn(roster, alias, key, remote=None):
        seen.append((remote or alias, key))
        return None if (remote or alias) in good else "rc=255: Connection timed out"

    fn.seen = seen
    return fn


def test_ip_hostname_alias_with_the_recorded_host_key_links_with_its_own_key(env, monkeypatch):
    own = _hostkey_env(env, monkeypatch)
    sp, probe_fn = Spawner(), _keyed_probe({"example-operator@macmini"})
    env.al.run(roster=env.roster, boxes=["example-operator@Mac-mini.local"], spawn=sp, probe_fn=probe_fn,
               stable_fn=boom, mdns_fn=lambda name: [])
    assert probe_fn.seen[-1] == ("example-operator@macmini", own)
    assert len(sp.cmds) == 2
    for c in sp.cmds:
        assert c[c.index("--remote") + 1] == "example-operator@macmini" and own in c
    assert "ssh alias macmini names the same box and answered" in (env.sdir / "autolink.log").read_text()


def test_alias_without_an_identity_file_is_probed_with_the_recorded_key(env, monkeypatch):
    _hostkey_env(env, monkeypatch)
    monkeypatch.setattr(env.al, "ssh_identity_file", lambda roster, a, run=None: None)
    probe_fn = _keyed_probe({"macmini"})
    remote, why, note, key = env.al.ssh_alias_fallback(env.roster, "Mac-mini.local", "/rec", "rc=255", probe_fn,
                                                       mdns_fn=lambda n: [])
    assert (remote, why, key) == ("macmini", None, "/rec") and probe_fn.seen[-1] == ("macmini", "/rec")


def test_alias_with_a_different_host_key_is_never_used(env, monkeypatch):
    _hostkey_env(env, monkeypatch, alias_key="ssh-ed25519 AAAAother")
    sp, probe_fn = Spawner(), _keyed_probe({"example-operator@macmini"})
    env.al.run(roster=env.roster, boxes=["example-operator@Mac-mini.local"], spawn=sp, probe_fn=probe_fn,
               stable_fn=lambda a: None, mdns_fn=lambda name: [])
    assert sp.cmds == [] and [r for r, _ in probe_fn.seen] == ["example-operator@Mac-mini.local"]


def _identity_env(env, monkeypatch, record="autolink-example_operator_at_macmini.json"):
    _alias_env(env, monkeypatch, {"macmini": "192.168.0.175", "other": "elsewhere.lan"})
    monkeypatch.setattr(env.al, "_known_hosts_entries", lambda *a, **k: [(["192.168.0.175", "macmini"], "k1")])
    own = str(env.home / "unimatrix-zero-machine-a-to-macmini")
    monkeypatch.setattr(env.al, "ssh_identity_file", lambda roster, a, run=None: own if a == "macmini" else None)
    if record:
        env.sdir.mkdir(parents=True, exist_ok=True)
        (env.sdir / record).write_text("{}", encoding="utf-8")
    return own


def _boxes_fn(reported):
    seen = []

    def fn(roster, remote, key, run=None):
        seen.append((remote, key))
        return reported.get(remote)

    fn.seen = seen
    return fn


def test_alias_with_a_link_record_and_the_recorded_box_in_its_roster_links_without_known_hosts(env, monkeypatch):
    own = _identity_env(env, monkeypatch)
    boxes_fn, probe_fn = _boxes_fn({"example-operator@macmini": {"mac-mini"}}), _keyed_probe(set())
    remote, why, note, key = env.al.ssh_alias_fallback(
        env.roster, "example-operator@Mac-mini.local", "/rec", "rc=255", probe_fn, mdns_fn=lambda n: [], boxes_fn=boxes_fn)
    assert (remote, why, key) == ("example-operator@macmini", None, own)
    assert "ssh alias macmini names the same box and answered" in note
    assert boxes_fn.seen == [("example-operator@macmini", own)] and probe_fn.seen == []


def test_alias_whose_roster_reports_another_box_is_never_used(env, monkeypatch):
    _identity_env(env, monkeypatch)
    boxes_fn, probe_fn = _boxes_fn({"example-operator@macmini": {"machine-b"}}), _keyed_probe({"example-operator@macmini"})
    remote, why, note, key = env.al.ssh_alias_fallback(
        env.roster, "example-operator@Mac-mini.local", "/rec", "rc=255", probe_fn, mdns_fn=lambda n: [], boxes_fn=boxes_fn)
    assert remote == "example-operator@Mac-mini.local" and why == "rc=255" and probe_fn.seen == []


def test_alias_without_a_link_record_is_never_asked_for_its_roster(env, monkeypatch):
    _identity_env(env, monkeypatch, record=None)
    boxes_fn = _boxes_fn({"example-operator@macmini": {"mac-mini"}})
    remote, why, _, _ = env.al.ssh_alias_fallback(
        env.roster, "example-operator@Mac-mini.local", "/rec", "rc=255", _keyed_probe(set()), mdns_fn=lambda n: [],
        boxes_fn=boxes_fn)
    assert why == "rc=255" and boxes_fn.seen == []


def test_run_links_the_mini_through_the_alias_when_known_hosts_has_no_entry_for_the_recorded_name(env, monkeypatch):
    own = _identity_env(env, monkeypatch)
    monkeypatch.setattr(env.al, "relay_boxes", _boxes_fn({"example-operator@macmini": {"mac-mini"}}))
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["example-operator@Mac-mini.local"], spawn=sp, probe_fn=_keyed_probe(set()),
               stable_fn=boom, mdns_fn=lambda name: [])
    assert len(sp.cmds) == 2, (env.sdir / "autolink.log").read_text()
    for c in sp.cmds:
        assert c[c.index("--remote") + 1] == "example-operator@macmini" and own in c


def test_relay_boxes_reads_the_box_of_each_roster_entry(env):
    out = b'{"entry": {"box": "Mac-Mini.local"}}\nnot json\n{"entry": {"box": "mac-mini"}}\n{"entry": {}}\n'
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, b"")
    assert env.al.relay_boxes(env.roster, "example-operator@macmini", "/k", run=run) == {"mac-mini"}
    fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 255, b"", b"x")
    assert env.al.relay_boxes(env.roster, "example-operator@macmini", "/k", run=fail) is None


def test_hashed_known_hosts_entry_for_the_recorded_name_matches(env):
    import base64, hashlib, hmac
    salt = b"0123456789abcdefghij"
    mac = base64.b64encode(hmac.new(salt, b"mac-mini.local", hashlib.sha1).digest()).decode()
    pat = f"|1|{base64.b64encode(salt).decode()}|{mac}"
    entries = [([pat], "ssh-ed25519 AAAAmini"), (["macmini"], "ssh-ed25519 AAAAmini")]
    assert env.al.known_host_keys(["Mac-mini.local"], entries) == {"ssh-ed25519 AAAAmini"}
    assert env.al.known_host_keys(["other.local"], entries) == set()


def test_bracketed_port_known_hosts_pattern_matches_the_bare_name(env):
    entries = [(["[mac-mini.local]:2222"], "ssh-ed25519 AAAAmini")]
    assert env.al.known_host_keys(["Mac-mini.local"], entries) == {"ssh-ed25519 AAAAmini"}


def test_healthy_primary_never_looks_up_ssh_aliases(env, monkeypatch):
    add_key(env)

    def boom_aliases(*a, **k):
        raise AssertionError("alias lookup on the healthy path")

    monkeypatch.setattr(env.al, "ssh_config_aliases", boom_aliases)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["mini-box.local"], spawn=sp, probe_fn=ok, stable_fn=boom)
    assert len(sp.cmds) == 2


def test_ssh_config_aliases_reads_concrete_hosts_and_follows_include(env, tmp_path):
    inc = tmp_path / "extra.conf"
    inc.write_text("Host viainc\n  HostName x\n", encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.write_text(f"Host *\n  ForwardAgent no\nHost macalias other # c\n  HostName y\nHost !neg\n"
                   f"Include {inc.as_posix()}\n", encoding="utf-8")
    real = load("unimatrix-autolink")
    assert real.ssh_config_aliases(cfg) == ["macalias", "other", "viainc"]


def test_healthy_primary_never_asks_mdns(live_env):
    def boom_mdns(name):
        raise AssertionError("mDNS asked on the healthy path")

    sp = Spawner()
    live_env.al.run(roster=live_env.roster, boxes=["machine-a"], spawn=sp, probe_fn=stale_ip_probe({"machine-a"}),
                    stable_fn=boom, mdns_fn=boom_mdns)
    assert len(sp.cmds) == 2


def test_resolve_key_never_returns_the_hands_on_unimatrix_01_key(env):
    hands_on = str(env.home / ".secrets" / "ssh" / "unimatrix-01")
    other = str(env.home / ".secrets" / "ssh" / "relay-key")
    assert env.al.resolve_key(env.roster, "a", lambda r, a: hands_on, lambda r, a: None) == str(env.key)
    assert env.al.resolve_key(env.roster, "a", lambda r, a: None, lambda r, a: hands_on) == str(env.key)
    assert env.al.resolve_key(env.roster, "a", lambda r, a: hands_on, lambda r, a: other) == other


def test_ssh_identity_file_skips_the_unimatrix_01_file_for_the_next_one(env, tmp_path):
    first, second = tmp_path / "unimatrix-01", tmp_path / "unimatrix-zero"
    first.write_text("k")
    second.write_text("k")
    out = f"hostname h\nidentityfile {first}\nidentityfile {second}\n"
    run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, "")
    assert env.real_ssh_identity_file(env.roster, "um01-macmini", run=run) == str(second)


def test_resolution_order_machine_local_then_ssh_config_then_default(env, tmp_path):
    local, ssh = write_key(env.home / "local"), write_key(env.home / "ssh")
    add_key(env)
    r = env.roster
    assert env.al.resolve_key(r, "a", lambda r, a: local, lambda r, a: ssh) == local
    assert env.al.resolve_key(r, "a", lambda r, a: None, lambda r, a: ssh) == ssh
    assert env.al.resolve_key(r, "a", lambda r, a: None, lambda r, a: None) == str(env.key)


def test_test_home_refuses_a_machine_local_key_outside_the_home_and_spawns_no_ssh(env, tmp_path):
    outside = write_key(tmp_path / "real-key")
    ssh_calls = []
    with pytest.raises(RuntimeError, match="test home refuses real key_path"):
        env.al.resolve_key(env.roster, "a", lambda r, a: outside, lambda r, a: ssh_calls.append(a))
    assert ssh_calls == []


def test_test_home_refuses_an_ssh_config_key_outside_the_home(env, tmp_path):
    outside = write_key(tmp_path / "real-ssh-key")
    with pytest.raises(RuntimeError, match="test home refuses real key_path"):
        env.al.resolve_key(env.roster, "a", lambda r, a: None, lambda r, a: outside)


def test_test_home_refuses_the_default_key_when_the_settings_home_is_unset(env, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME")
    with pytest.raises(env.roster.MissingSettingsError, match="COORDINATOR_SETTINGS_HOME"):
        env.roster.default_key()


def test_keys_outside_a_test_home_resolve_when_the_marker_is_absent(env, monkeypatch, tmp_path):
    monkeypatch.delenv("UNIMATRIX_TEST_HOME")
    outside = write_key(tmp_path / "real-key")
    assert env.al.resolve_key(env.roster, "a", lambda r, a: outside, lambda r, a: None) == outside


def test_test_home_refuses_a_real_far_box_and_keeps_an_allowlisted_one(env, monkeypatch):
    reader = env.home / "bin" / "_machine_local.py"
    reader.write_text("print('machine-b')\n")
    with pytest.raises(RuntimeError, match="test home refuses real far_box: 'machine-b'"):
        env.al.far_boxes_status(env.roster, guard=True)
    assert env.al.far_boxes_status(env.roster) == (["machine-b"], None)
    reader.write_text("print('machine-a')\n")
    assert env.al.far_boxes_status(env.roster, guard=True) == (["machine-a"], None)


def test_pin_key_set_to_an_existing_interpreter_is_used_at_every_spawn_site(env, tmp_path, monkeypatch):
    pin = tmp_path / "pinned-python"
    pin.write_text("")
    monkeypatch.setattr(env.al, "pin_key_value", lambda roster, run=None: str(pin))
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert len(sp.cmds) == 2 and all(c[0] == str(pin) for c in sp.cmds)


def test_pin_key_naming_a_missing_interpreter_refuses_naming_the_key_and_spawns_nothing(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env.al, "pin_key_value", lambda roster, run=None: str(tmp_path / "gone"))
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert sp.cmds == []
    assert "unimatrix.python names a missing interpreter" in "\n".join(log_lines(env))
    with pytest.raises(env.al.InterpreterPinError, match="unimatrix.python"):
        env.al.pinned_python(env.roster)


def test_unset_pin_resolves_the_console_interpreter_not_a_pythonw_or_shim(env, monkeypatch):
    fake = types.SimpleNamespace(unimatrix_python=lambda pin=None: pin or "C:/py/python.exe")  # abs-path-ok: synthetic interpreter path fixture, never opened
    monkeypatch.setattr(env.al, "_interp", lambda: fake)
    assert env.al.pinned_python(env.roster) == "C:/py/python.exe"  # abs-path-ok: synthetic interpreter path fixture, never opened
    sp = Spawner()
    add_key(env)
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok)
    assert all(c[0] == "C:/py/python.exe" for c in sp.cmds)  # abs-path-ok: synthetic interpreter path fixture, never opened


def test_unset_pin_on_darwin_equals_the_installers_forced_command_interpreter(env, monkeypatch):
    brew = "/opt/homebrew/bin/python3"  # abs-path-ok: synthetic fake-filesystem path, never opened
    fake_os = types.SimpleNamespace(
        name="posix",
        path=types.SimpleNamespace(isfile=lambda p: p == brew),
        access=lambda p, mode: p == brew,
        X_OK=os.X_OK,
    )
    fake_sys = types.SimpleNamespace(platform="darwin", executable="/usr/bin/python3")  # abs-path-ok: synthetic fixture
    inst = load("unimatrix-install")
    interp = env.al._interp()
    for mod in (inst, interp):
        monkeypatch.setattr(mod, "os", fake_os)
        monkeypatch.setattr(mod, "sys", fake_sys)
    monkeypatch.setattr(env.al, "_interp", lambda: interp)
    monkeypatch.setattr(inst, "_pin_value", lambda: None)
    monkeypatch.setattr(inst, "_lib", lambda name: interp)
    assert env.al.pinned_python(env.roster) == inst.forced_command_interpreter() == brew


def test_pin_value_is_read_from_the_machine_local_key(env):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "~/py\n", "")

    (env.home / "bin" / "_machine_local.py").write_text("")
    real = load("unimatrix-autolink")
    assert real.pin_key_value(env.roster, run=fake_run) == os.path.expanduser("~/py")
    assert seen[0][-2:] == ["get", "unimatrix.python"]


def test_machine_local_key_reads_the_per_alias_registry_key(env, tmp_path):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "~/k-machine-a\n", "")

    (env.home / "bin" / "_machine_local.py").write_text("")
    assert env.al.machine_local_key(env.roster, "machine-a", run=fake_run) == os.path.expanduser("~/k-machine-a")
    assert seen[0][-2:] == ["get", "unimatrix.keys.machine-a"]
    unset = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "")
    assert env.al.machine_local_key(env.roster, "machine-a", run=unset) is None


def test_ssh_identity_file_takes_existing_file_and_ignores_builtin_defaults(env, tmp_path):
    real = write_key(tmp_path / "unimatrix-zero-a-to-b")
    default = write_key(tmp_path / "id_ed25519")
    missing = str(tmp_path / "gone")

    def out(*files):
        text = "".join(f"identityfile {f}\n" for f in files)
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, text, "")

    ssh = env.real_ssh_identity_file
    assert ssh(env.roster, "a", run=out(default, missing, real)) == real
    assert ssh(env.roster, "a", run=out(default, missing)) is None
    assert ssh(env.roster, "a", run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 255, "", "")) is None


def test_alias_with_missing_key_logs_one_line_and_next_alias_still_links(env, tmp_path):
    kb = write_key(tmp_path / "kb")
    keys = {"alpha": str(tmp_path / "absent"), "beta": kb}
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["alpha", "beta"], spawn=sp, probe_fn=ok, key_fn=lambda r, a: keys[a])
    assert {c[c.index("--remote") + 1] for c in sp.cmds} == {"beta"}
    lines = (env.sdir / "autolink.log").read_text().splitlines()
    assert len(lines) == 1 and "alpha" in lines[0] and "no Unimatrix-Zero key" in lines[0]


def test_default_resolution_path_matches_todays_shared_key(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a", "other"], spawn=sp, probe_fn=ok)
    assert {key_of(c) for c in sp.cmds} == {str(env.key)} and len(sp.cmds) == 4


def router_cmds(sp):
    return [c for c in sp.cmds if Path(c[1]).name == "unimatrix-peer-router.py"]


@pytest.fixture
def router_on(env, monkeypatch):
    monkeypatch.delenv("UNIMATRIX_AUTOLINK_NO_ROUTER")
    monkeypatch.setattr(env.al, "router_lock_held", lambda sdir: False)
    return env


def test_dead_router_record_spawns_exactly_one_router_with_no_aliases_configured(router_on):
    e, sp = router_on, Spawner()
    e.al.run(roster=e.roster, boxes=[], spawn=sp, probe_fn=ok)
    # The router runs the one unimatrix interpreter (c6bb93e2b3), not the test's own sys.executable.
    assert len(sp.cmds) == 1 and len(router_cmds(sp)) == 1 and sp.cmds[0][0] == e.al.pinned_python(e.roster)
    rec = json.loads((e.sdir / "autolink-router.json").read_text())
    assert rec == {"pid": 1001, "procStart": "start-1001"}


def test_live_router_record_spawns_nothing(router_on):
    e, sp = router_on, Spawner()
    e.sdir.mkdir(parents=True)
    e.live.add(3001)
    (e.sdir / "autolink-router.json").write_text(json.dumps({"pid": 3001, "procStart": "start-3001"}))
    e.al.run(roster=e.roster, boxes=[], spawn=sp, probe_fn=ok)
    assert sp.cmds == []


def test_killed_router_is_respawned_on_the_next_run_even_when_the_far_cache_is_healthy(router_on):
    e, sp = router_on, Spawner()
    e.al.run(roster=e.roster, boxes=[], spawn=sp, probe_fn=ok)
    e.live.add(1001)
    e.al.run(roster=e.roster, boxes=[], spawn=sp, probe_fn=ok)
    assert len(router_cmds(sp)) == 1
    e.live.discard(1001)
    e.al.run(roster=e.roster, boxes=[], spawn=sp, probe_fn=ok)
    assert len(router_cmds(sp)) == 2


def test_hand_started_router_holding_its_lease_is_not_double_spawned(router_on, monkeypatch):
    e, sp = router_on, Spawner()
    monkeypatch.setattr(e.al, "router_lock_held", lambda sdir: True)
    e.al.run(roster=e.roster, boxes=[], spawn=sp, probe_fn=ok)
    assert sp.cmds == [] and not (e.sdir / "autolink-router.json").exists()


def test_router_lease_probe_reads_a_really_held_lock(env):
    transport = load("unimatrix-local-transport")
    lock = env.sdir / "router" / "router.lock"
    lock.parent.mkdir(parents=True)
    assert env.al.router_lock_held(env.sdir) is False
    with open(lock, "a+b") as held:
        assert transport.lock_file(held)
        assert env.al.router_lock_held(env.sdir) is True
        transport.unlock_file(held)
    assert env.al.router_lock_held(env.sdir) is False


def test_router_is_not_a_per_alias_supervisor(env):
    assert all(script != "unimatrix-peer-router.py" for _, script in env.al.SUPERVISORS)


def test_held_router_spawn_lock_blocks_a_concurrent_router_spawn(router_on):
    e, sp = router_on, Spawner()
    e.sdir.mkdir(parents=True)
    (e.sdir / "autolink-router.lock").write_text("")
    e.al.run(roster=e.roster, boxes=[], spawn=sp, probe_fn=ok)
    assert sp.cmds == []


def test_router_spawn_failure_is_logged_and_far_links_still_run(router_on):
    e = router_on
    add_key(e)
    sp = Spawner()

    def spawn(cmd):
        if Path(cmd[1]).name == "unimatrix-peer-router.py":
            raise OSError("no exec")
        return sp(cmd)

    e.al.run(roster=e.roster, boxes=["machine-a"], spawn=spawn, probe_fn=ok)
    assert len(sp.cmds) == 2
    assert any("peer router supervision failed: OSError" in line for line in log_lines(e))


def seed_live_pair(e, alias, pids=(2001, 2002)):
    e.sdir.mkdir(parents=True, exist_ok=True)
    e.live.update(pids)
    rec = {"bridge": {"pid": pids[0], "procStart": f"start-{pids[0]}"},
           "inbox": {"pid": pids[1], "procStart": f"start-{pids[1]}"}}
    (e.sdir / f"autolink-{alias}.json").write_text(json.dumps(rec))


def boom(*a, **kw):
    raise AssertionError("called on the healthy path")


def stamp_pull(env, at=None):
    env.sdir.mkdir(parents=True, exist_ok=True)
    (env.sdir / env.al.PULL_STAMP_NAME).write_text(json.dumps({"at": time.time() if at is None else at}))


def test_healthy_cache_makes_no_key_probe_or_spawn_call_and_logs_nothing(env):
    stamp_pull(env)
    seed_live_pair(env, "machine-a")
    (env.sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": ["machine-a"]}))
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, stable_fn=boom)
    assert not (env.sdir / "autolink.log").exists()


def test_live_record_skips_key_resolution_for_that_alias_only(env, tmp_path):
    seed_live_pair(env, "up")
    kd = write_key(tmp_path / "kd")
    keyed = []

    def key_fn(r, a):
        keyed.append(a)
        return kd

    env.al.run(roster=env.roster, boxes=["up", "down"], spawn=Spawner(), probe_fn=ok, key_fn=key_fn)
    assert keyed == ["down"]


def test_failing_probe_twice_inside_the_window_probes_once(env):
    add_key(env)
    calls = []

    def fail(r, a, k, remote=None):
        calls.append(a)
        return "rc=255"

    kw = dict(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), probe_fn=fail, stable_fn=lambda a: None)
    env.al.run(now=1000.0, **kw)
    env.al.run(now=1030.0, **kw)
    assert calls == ["machine-a"]
    env.al.run(now=1000.0 + 61, **kw)
    assert calls == ["machine-a", "machine-a"]
    assert len(log_lines(env)) == 2


def test_successful_probe_clears_the_backoff(env):
    add_key(env)
    env.sdir.mkdir(parents=True, exist_ok=True)
    live = env.al._liveness()
    live.note_probe(env.sdir, "machine-a", False, 1000.0)
    assert live.backoff_until(env.sdir, "machine-a") > 1000.0
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), probe_fn=ok, now=1000.0 + 61)
    assert live.backoff_until(env.sdir, "machine-a") == 0.0


def test_full_run_writes_the_boxes_cache(env):
    add_key(env)
    env.al.run(roster=env.roster, boxes=["machine-a", "other"], spawn=Spawner(), probe_fn=ok)
    assert json.loads((env.sdir / "autolink-boxes.json").read_text()) == {"aliases": ["machine-a", "other"]}


def test_zero_alias_run_writes_an_empty_cache_so_the_next_start_is_silent(env):
    env.al.run(roster=env.roster, boxes=[], spawn=boom, probe_fn=boom)
    assert json.loads((env.sdir / "autolink-boxes.json").read_text()) == {"aliases": []}
    n = len(log_lines(env))
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom)
    assert len(log_lines(env)) == n


def test_unreadable_registry_does_not_write_a_cache(env):
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom)
    assert not (env.sdir / "autolink-boxes.json").exists()
    assert "unreadable" in log_lines(env)[0]


# ---- membership snapshot as the member source ----------------------------------------------------

import base64  # noqa: E402
import shutil  # noqa: E402

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen required")


def _keygen(path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True, capture_output=True)
    return path, Path(str(path) + ".pub").read_text().strip()


class Snap:
    """A signed membership state builder over the real membership library and a real ed25519 signer key."""

    def __init__(self, env, tmp_path):
        self.m = env.al._membership()
        self.env = env
        self.signer, self.pub = _keygen(tmp_path / "signer")
        self.relay_pub = _keygen(tmp_path / "relay")[1]
        self.snap = None

    def member(self, box, user="kim"):
        return {"box": box, "host": f"{box}.local", "user": user, "relay_pub": self.relay_pub, "from_cidrs": []}

    def sign(self, change, key=None):
        snap = self.m.next_snapshot(self.snap, change)
        sig = self.m.FakeSigner(key or self.signer, environ={}).sign(self.m.canonical_bytes(snap))
        return snap, sig

    def adopt(self, *boxes):
        if self.m.load_pin(self.env.home) is None:
            self.m.write_pin(self.env.home, self.pub)
        for b in boxes:
            snap, sig = self.sign({"op": "join", "member": self.member(b)})
            self.m.adopt(self.env.home, snap, sig, notifier=lambda *a, **k: None)
            self.snap = snap


@pytest.fixture
def snap(env, tmp_path):
    return Snap(env, tmp_path)


def remotes(sp):
    return {c[c.index("--remote") + 1] for c in sp.cmds}


@needs_keygen
def test_snapshot_members_link_by_record_host_minus_self_with_far_boxes_unset(env, snap, capsys):
    add_key(env)
    snap.adopt("me", "machine-a", "bishop")
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names={"me"}, pull_fn=lambda *a: False)
    assert remotes(sp) == {"kim@machine-a.local", "kim@bishop.local"} and len(sp.cmds) == 4
    assert capsys.readouterr().out == ""
    assert json.loads((env.sdir / "autolink-boxes.json").read_text()) == {
        "aliases": ["kim@bishop.local", "kim@machine-a.local"], "epoch": 3}


@needs_keygen
def test_snapshot_member_key_is_resolved_by_box_name_and_no_stable_name_is_tried(env, snap):
    add_key(env)
    snap.adopt("machine-a")
    seen = []

    def key_fn(r, name):
        seen.append(name)
        return str(env.key)

    def probe_fail(roster, alias, key, remote=None):
        return "rc=255"

    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=probe_fail, key_fn=key_fn, self_names=set(),
               pull_fn=lambda *a: False)
    assert seen == ["machine-a"]
    assert "kim@machine-a.local unreachable" in log_lines(env)[0] and "pinned address stale" not in "".join(log_lines(env))


@needs_keygen
def test_snapshot_member_key_is_resolved_by_the_ssh_alias_whose_hostname_matches_the_member_host(env, snap, monkeypatch):
    add_key(env)
    snap.adopt("Mac-mini")
    (env.home / "bin" / "_machine_local.py").write_text(
        "import sys\nk = sys.argv[-1]\nprint('macmini-lan other' if k == 'unimatrix.far_boxes' else '')\n"
        "sys.exit(0 if k == 'unimatrix.far_boxes' else 1)\n")
    hosts = {"macmini-lan": "mac-mini.local", "other": "100.64.0.9"}
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, alias, run=None: hosts.get(alias))
    seen = []

    def key_fn(r, name):
        seen.append(name)
        return str(env.key)

    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok, key_fn=key_fn, self_names=set(),
               pull_fn=lambda *a: False)
    assert seen == ["other", "macmini-lan"]


@needs_keygen
def test_snapshot_run_reads_far_boxes_once_for_all_pending_aliases(env, snap, monkeypatch):
    add_key(env)
    snap.adopt("machine-a", "bishop", "rook")
    calls = []
    real = env.al.far_boxes_status

    def counting(roster, guard=False):
        calls.append(guard)
        return real(roster, guard=guard)

    monkeypatch.setattr(env.al, "far_boxes_status", counting)
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(), pull_fn=lambda *a: False)
    assert len(remotes(sp)) == 3 and calls == [False]


def test_member_alias_falls_back_to_the_box_name_when_no_alias_matches_the_host(env):
    assert env.al.member_alias(env.roster, "Mac-mini", "Mac-mini.local", [], {}) == "Mac-mini"
    assert env.al.member_alias(env.roster, "Mac-mini", "", ["macmini-lan"], {}) == "Mac-mini"


def test_member_alias_matches_an_ip_pinned_alias_by_its_known_hosts_key(env):
    entries = [(["Mac-mini.local"], "ssh-ed25519 AAAAmini"), (["192.168.0.175"], "ssh-ed25519 AAAAmini")]
    hosts = {"macmini-lan": "192.168.0.175"}
    assert env.al.member_alias(env.roster, "Mac-mini", "Mac-mini.local", ["macmini-lan"], hosts,
                               entries=entries) == "macmini-lan"
    other = [(["Mac-mini.local"], "ssh-ed25519 AAAAmini"), (["192.168.0.175"], "ssh-ed25519 AAAAother")]
    assert env.al.member_alias(env.roster, "Mac-mini", "Mac-mini.local", ["macmini-lan"], hosts,
                               entries=other) == "Mac-mini"


@needs_keygen
def test_unverifiable_snapshot_links_nothing_logs_one_line_and_ignores_far_boxes(env, snap, tmp_path, capsys):
    add_key(env)
    snap.adopt("machine-a")
    d = env.sdir / "membership"
    other, _ = _keygen(tmp_path / "forger")
    forged, sig = snap.sign({"op": "join", "member": snap.member("evil")}, key=other)
    (d / "snapshot.json").write_bytes(snap.m.canonical_bytes(forged))
    (d / "snapshot.sig").write_bytes(sig)
    (d / "verified.json").unlink()
    (env.home / "bin" / "_machine_local.py").write_text("print('fallbackbox')\n")
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set())
    assert sp.cmds == [] and capsys.readouterr().out == ""
    lines = log_lines(env)
    assert len(lines) == 1 and "nothing linked from it" in lines[0]


def test_no_snapshot_falls_back_to_far_boxes_and_logs_that_doctor_flags_it(env):
    add_key(env)
    (env.home / "bin" / "_machine_local.py").write_text("print('machine-a')\n")
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(), key_fn=lambda r, a: str(env.key))
    assert remotes(sp) == {"machine-a"}
    assert "unimatrix doctor will flag it" in log_lines(env)[0]


@needs_keygen
def test_a_verified_snapshot_links_its_members_and_never_the_far_boxes_key(env, snap):
    add_key(env)
    snap.adopt("me", "machine-a", "bishop")
    (env.home / "bin" / "_machine_local.py").write_text(
        "import sys\nk = sys.argv[-1]\nprint('fallbackbox' if k == 'unimatrix.far_boxes' else '')\n"
        "sys.exit(0 if k == 'unimatrix.far_boxes' else 1)\n")
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names={"me"}, pull_fn=lambda *a: False,
               key_fn=lambda r, a: str(env.key))
    assert remotes(sp) == {"kim@machine-a.local", "kim@bishop.local"} and len(sp.cmds) == 4


@needs_keygen
def test_no_signer_is_constructed_by_a_snapshot_run(env, snap, monkeypatch):
    add_key(env)
    snap.adopt("machine-a")

    def refuse(*a, **kw):
        raise AssertionError("a signer was constructed")

    monkeypatch.setattr(snap.m, "FakeSigner", refuse)
    monkeypatch.setattr(snap.m, "AgentSigner", refuse)
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok, self_names=set(), pull_fn=lambda *a: False)
    assert env.al.record_path(env.sdir, "kim@machine-a.local").is_file()


@needs_keygen
def test_unchanged_snapshot_second_start_spawns_no_keygen(env, snap, monkeypatch):
    add_key(env)
    snap.adopt("machine-a")
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok, self_names=set(), pull_fn=lambda *a: False)

    def no_run(*a, **kw):
        raise AssertionError("ssh-keygen spawned for an unchanged snapshot")

    # the verified-digest cache answers an unchanged snapshot without any spawn
    status, targets, epoch, _ = env.al.member_targets(env.roster, None, set(), run=no_run)
    assert status == "ok" and epoch == 1 and list(targets) == ["kim@machine-a.local"]


@needs_keygen
def test_pull_adopts_a_higher_epoch_from_an_answering_member_and_links_the_new_member(env, snap, tmp_path):
    add_key(env)
    snap.adopt("machine-a")
    held = dict(snap.snap)
    snap.adopt("bishop")
    higher, sig = snap.snap, (env.sdir / "membership" / "snapshot.sig").read_bytes()
    # roll the box back to epoch 1, then let a peer answer `membership get` with epoch 2
    d = env.sdir / "membership"
    m = snap.m
    (d / "snapshot.json").write_bytes(m.canonical_bytes(held))
    first_sig = m.FakeSigner(snap.signer, environ={}).sign(m.canonical_bytes(held))
    (d / "snapshot.sig").write_bytes(first_sig)
    (d / "verified.json").unlink()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = m.canonical_bytes(higher) + b"\n" + base64.b64encode(sig) + b"\n"
        return subprocess.CompletedProcess(cmd, 0, out, b"")

    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(),
               pull_fn=lambda roster, remote, key: env.real_pull_membership(roster, remote, key, run=fake_run))
    assert calls and calls[0][-2:] == ["membership", "get"] and "kim@machine-a.local" in calls[0]
    assert m.load_snapshot(env.home)[0]["epoch"] == 2
    assert remotes(sp) == {"kim@machine-a.local", "kim@bishop.local"}
    assert json.loads((env.sdir / "autolink-boxes.json").read_text())["epoch"] == 2


@needs_keygen
def test_pull_ignores_an_equal_or_lower_epoch_and_a_failed_ask(env, snap):
    snap.adopt("machine-a")
    m = snap.m
    sig = (env.sdir / "membership" / "snapshot.sig").read_bytes()
    out = m.canonical_bytes(snap.snap) + b"\n" + base64.b64encode(sig) + b"\n"
    same = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, out, b"")
    failed = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"no membership snapshot held")
    assert env.real_pull_membership(env.roster, "kim@x", "/k", run=same) is False
    assert env.real_pull_membership(env.roster, "kim@x", "/k", run=failed) is False
    assert not (env.sdir / "membership" / "events.log").exists() or "refused" not in (env.sdir / "membership" / "events.log").read_text()


@needs_keygen
def test_a_moved_epoch_defeats_the_healthy_cache_so_a_new_member_links(env, snap):
    add_key(env)
    snap.adopt("machine-a")
    seed_live_pair(env, env.al.slug("kim@machine-a.local"))
    (env.sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": ["kim@machine-a.local"], "epoch": 1}))
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=boom, key_fn=boom, self_names=set())
    assert sp.cmds == []
    snap.adopt("bishop")
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(), pull_fn=lambda *a: False)
    assert remotes(sp) == {"kim@bishop.local"}


@needs_keygen
def test_a_member_whose_dial_name_fails_the_alias_check_is_surfaced_not_dropped_silently(env, snap, monkeypatch):
    add_key(env)
    snap.adopt("machine-a", "bishop")
    monkeypatch.setattr(env.al, "ALIAS_RE", re.compile(r"[a-z@.]+machine-a\.local"))
    status, targets, skipped, _, _ = env.al.split_members(env.roster, None, set())
    assert status == "ok" and list(targets) == ["kim@machine-a.local"]
    assert skipped == [("bishop", "kim@bishop.local")]
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(), pull_fn=lambda *a: False)
    assert remotes(sp) == {"kim@machine-a.local"}
    assert any("bishop" in ln and "not linked" in ln for ln in log_lines(env))


def test_an_ip_literal_member_host_is_skipped_never_dialled_and_a_hostname_member_is(env):
    real = env.al._membership()

    class Stub:
        MembershipError = real.MembershipError
        is_ip_literal = staticmethod(real.is_ip_literal)

        @staticmethod
        def load_snapshot(home):
            return {"epoch": 3}, b""

        @staticmethod
        def verified_members(home, run=None):
            return [{"box": "mini", "host": "mac-mini.local", "user": "kim"},
                    {"box": "pinned", "host": "192.168.1.9", "user": "kim"}]

    status, targets, skipped, epoch, _ = env.al.split_members(env.roster, Stub, set())
    assert status == "ok" and epoch == 3
    assert targets == {"kim@mac-mini.local": "mini"}
    assert skipped == [("pinned", "kim@192.168.1.9")]


def test_missing_membership_library_means_transition_fallback(env, monkeypatch):
    def gone():
        raise FileNotFoundError("unimatrix-membership.py")

    monkeypatch.setattr(env.al, "_membership", gone)
    assert env.al.member_targets(env.roster)[0] == "none"


def test_locked_1password_waits_by_name_without_probe_or_backoff(env):
    add_key(env)
    calls = []

    def probe_fn(r, a, k, remote=None):
        calls.append(a)
        return None

    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn, agent_fn=lambda key: "locked",
               now=1000.0)
    assert calls == [] and sp.cmds == []
    lines = log_lines(env)
    assert lines == [lines[0]] and lines[0].endswith("far box machine-a: waiting: 1Password locked")
    assert "unreachable" not in lines[0]
    assert env.al._liveness().backoff_until(env.sdir, "machine-a") <= 1000.0


def test_missing_op_names_the_passphrase_source_and_does_not_probe(env):
    add_key(env)
    calls = []
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), agent_fn=lambda key: "missing",
               probe_fn=lambda r, a, k, remote=None: calls.append(a))
    assert calls == [] and "passphrase_source_missing" in log_lines(env)[-1]


def test_loaded_key_links_as_before(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok, agent_fn=lambda key: "loaded")
    assert len(sp.cmds) == 2


def test_default_agent_state_skips_an_unencrypted_key_without_a_spawn(env):
    add_key(env)

    def boom(*a, **k):
        raise AssertionError("no subprocess for an unencrypted key")

    assert env.al.agent_state(str(env.key), run=boom) == "unencrypted"


def _stub_passphrase(tmp_path, verdict_file):
    (tmp_path / "unimatrix-passphrase.py").write_text(
        "from pathlib import Path\n"
        f"V = Path({str(verdict_file)!r})\n"
        "def ensure_agent(key):\n"
        "    with open(str(V) + '.calls', 'a') as f:\n"
        "        f.write('x')\n"
        "    return V.read_text()\n", encoding="utf-8")


def test_roster_waits_on_a_locked_key_and_caches_a_loaded_verdict(env, tmp_path, monkeypatch):
    verdict = tmp_path / "verdict"
    _stub_passphrase(tmp_path, verdict)
    monkeypatch.setattr(env.roster, "HERE", tmp_path)
    monkeypatch.setattr(env.roster, "_AGENT_OK", {})
    verdict.write_text("locked")
    with pytest.raises(OSError, match="waiting: 1Password locked"):
        env.roster.ensure_key_loaded("k", now=1000.0)
    verdict.write_text("missing")
    with pytest.raises(OSError, match="passphrase_source_missing"):
        env.roster.ensure_key_loaded("k", now=1001.0)
    verdict.write_text("loaded")
    env.roster.ensure_key_loaded("k", now=1002.0)
    env.roster.ensure_key_loaded("k", now=1003.0)
    assert Path(str(verdict) + ".calls").read_text() == "xxx"


class Lease:
    """lock_held_fn double: held until `release()`; `polls` counts checks."""

    def __init__(self, held=True):
        self.held, self.polls = held, 0

    def __call__(self, sdir):
        self.polls += 1
        return self.held

    def release(self):
        self.held = False


@pytest.fixture
def restartable(router_on, tmp_path, monkeypatch):
    e = router_on
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(e.roster, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(e.al, "run_launchctl", lambda *a: None)  # never reach the real launchd
    (e.sdir / "router").mkdir(parents=True)
    e.sessions = sessions
    e.say_lines = []
    e.say = e.say_lines.append
    return e


def write_pid_json(e, pid, stamp=None):
    (e.sdir / "router" / "router.pid.json").write_text(
        json.dumps({"pid": pid, "procStart": stamp or f"start-{pid}"}))


def test_restart_handshake_waits_for_the_lease_then_starts_the_installed_router(restartable):
    e, sp, lease, asked = restartable, Spawner(), Lease(), []
    write_pid_json(e, 4001)
    e.live.add(4001)

    def request(req):
        asked.append(req)
        lease.release()
        return {"ok": True, "pid": 4001, "stopping": True}

    def kill(pid):
        raise AssertionError("handshake must not kill")

    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, request=request, kill=kill, lock_held_fn=lease)
    assert asked == [{"op": "shutdown"}]
    assert res == {"stopped": "handshake", "old_pid": 4001, "new_pid": 1001}
    assert len(router_cmds(sp)) == 1 and Path(router_cmds(sp)[0][1]).name == "unimatrix-peer-router.py"
    assert json.loads((e.sdir / "autolink-router.json").read_text()) == {"pid": 1001, "procStart": "start-1001"}


def test_restart_unknown_op_tree_kills_the_recorded_pid_only_when_its_start_matches(restartable):
    e, sp, lease, killed = restartable, Spawner(), Lease(), []
    write_pid_json(e, 4002)
    e.live.add(4002)

    def kill(pid):
        killed.append(pid)
        lease.release()

    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, request=lambda r: {"ok": False, "error": "unknown op"},
                              kill=kill, lock_held_fn=lease)
    assert killed == [4002]
    assert res == {"stopped": "tree_kill", "old_pid": 4002, "new_pid": 1001}


def test_restart_falls_back_to_the_autolink_record_for_a_router_with_no_pid_file(restartable):
    e, sp, lease, killed = restartable, Spawner(), Lease(), []
    (e.sdir / "autolink-router.json").write_text(json.dumps({"pid": 4003, "procStart": "start-4003"}))
    e.live.add(4003)
    e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, request=lambda r: {"ok": False, "error": "unknown op"},
                        kill=lambda pid: (killed.append(pid), lease.release()), lock_held_fn=lease)
    assert killed == [4003]


def test_restart_reads_the_router_stamp_off_a_face_registration(restartable):
    e, sp, lease, killed = restartable, Spawner(), Lease(), []
    (e.sessions / "5001.json").write_text(json.dumps({"pid": 5001, "unimatrixRouterPid": 4004,
                                                      "unimatrixRouterStart": "start-4004"}))
    e.live.add(4004)
    e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, request=lambda r: {"ok": False, "error": "unknown op"},
                        kill=lambda pid: (killed.append(pid), lease.release()), lock_held_fn=lease)
    assert killed == [4004]


def test_restart_unreachable_control_endpoint_takes_the_same_verified_kill(restartable):
    e, sp, lease, killed = restartable, Spawner(), Lease(), []
    write_pid_json(e, 4005)
    e.live.add(4005)

    def request(req):
        raise ConnectionRefusedError("no endpoint")

    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, request=request,
                              kill=lambda pid: (killed.append(pid), lease.release()), lock_held_fn=lease)
    assert killed == [4005] and res["stopped"] == "tree_kill"


def test_restart_never_kills_a_pid_whose_process_start_differs_from_the_stamp(restartable):
    e, sp, lease = restartable, Spawner(), Lease()
    write_pid_json(e, 4006, stamp="a-different-birth")
    e.live.add(4006)

    def kill(pid):
        raise AssertionError("recycled pid must not be killed")

    with pytest.raises(e.al.RouterRestartError) as ei:
        e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, request=lambda r: {"ok": False, "error": "unknown op"},
                            kill=kill, lock_held_fn=lease)
    assert "4006" in str(ei.value) and "router.lock" in str(ei.value)
    assert sp.cmds == []


def test_restart_raises_when_the_lease_stays_held_after_the_kill(restartable):
    e, sp, lease = restartable, Spawner(), Lease()
    write_pid_json(e, 4007)
    e.live.add(4007)
    with pytest.raises(e.al.RouterRestartError, match="4007.*router.lock"):
        e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, request=lambda r: {"ok": False},
                            kill=lambda pid: None, lock_held_fn=lease, wait_seconds=0.2)
    assert sp.cmds == []


def test_restart_with_no_live_router_just_starts_one_and_drops_a_stale_record(restartable):
    e, sp = restartable, Spawner()
    (e.sdir / "autolink-router.json").write_text(json.dumps({"pid": 4008, "procStart": "start-4008"}))  # not alive
    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp,
                              request=lambda r: (_ for _ in ()).throw(AssertionError("no router to ask")),
                              kill=boom, lock_held_fn=Lease(held=False))
    assert res == {"stopped": "none", "old_pid": None, "new_pid": 1001}


def test_restart_starts_a_router_even_with_the_no_router_env_set_and_restores_it(restartable, monkeypatch):
    e, sp = restartable, Spawner()
    monkeypatch.setenv("UNIMATRIX_AUTOLINK_NO_ROUTER", "1")
    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, lock_held_fn=Lease(held=False))
    assert res["new_pid"] == 1001 and os.environ["UNIMATRIX_AUTOLINK_NO_ROUTER"] == "1"


def test_restart_raises_when_no_router_record_is_written(restartable):
    e = restartable
    (e.sdir / "autolink-router.lock").write_text("")  # another start holds the spawn lock
    with pytest.raises(e.al.RouterRestartError, match="no router was started"):
        e.al.restart_router(e.roster, e.sdir, e.say, spawn=Spawner(), lock_held_fn=Lease(held=False))


class Launchctl:
    """launchctl runner double: `print` answers `loaded`; `kickstart` runs `on_kickstart` (writes the record)."""

    def __init__(self, loaded=True, kick_rc=0, on_kickstart=None):
        self.loaded, self.kick_rc, self.on_kickstart, self.calls = loaded, kick_rc, on_kickstart, []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "print":
            return types.SimpleNamespace(returncode=0 if self.loaded else 113)
        if args[0] == "kickstart" and self.on_kickstart and self.kick_rc == 0:
            self.on_kickstart()
        return types.SimpleNamespace(returncode=self.kick_rc)


def _agent_writes_record(e, pid=5001):
    def write():
        e.live.add(pid)
        (e.sdir / "autolink-router.json").write_text(json.dumps({"pid": pid, "procStart": f"start-{pid}"}))
    return write


def test_restart_on_darwin_with_the_agent_loaded_kickstarts_it_and_never_spawns(restartable):
    e, sp = restartable, Spawner()
    lc = Launchctl(on_kickstart=_agent_writes_record(e))
    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, lock_held_fn=Lease(held=False),
                              launchctl=lc, platform="darwin", uid=501)
    target = "gui/501/com.unimatrix.autolink"
    assert lc.calls == [("print", target), ("kickstart", target)]
    assert sp.cmds == [] and res == {"stopped": "none", "old_pid": None, "new_pid": 5001}


def test_login_agent_label_matches_the_installer_label():
    assert load("unimatrix-autolink").LOGIN_AGENT_LABEL == load("unimatrix-install").LOGIN_AGENT_LABEL


def test_restart_on_darwin_with_the_agent_unloaded_keeps_the_detached_spawn(restartable):
    e, sp = restartable, Spawner()
    lc = Launchctl(loaded=False)
    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, lock_held_fn=Lease(held=False),
                              launchctl=lc, platform="darwin", uid=501)
    assert [c[0] for c in lc.calls] == ["print"] and len(router_cmds(sp)) == 1 and res["new_pid"] == 1001


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_restart_off_darwin_never_asks_launchctl_and_spawns_detached(restartable, platform):
    e, sp = restartable, Spawner()
    lc = Launchctl()
    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, lock_held_fn=Lease(held=False),
                              launchctl=lc, platform=platform, uid=501)
    assert lc.calls == [] and len(router_cmds(sp)) == 1 and res["new_pid"] == 1001


def test_restart_falls_back_to_the_detached_spawn_when_the_kickstarted_agent_writes_no_record(restartable):
    e, sp = restartable, Spawner()
    lc = Launchctl()
    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, lock_held_fn=Lease(held=False),
                              launchctl=lc, platform="darwin", uid=501, wait_seconds=0.2)
    assert [c[0] for c in lc.calls] == ["print", "kickstart"] and len(router_cmds(sp)) == 1 and res["new_pid"] == 1001


def test_restart_falls_back_to_the_detached_spawn_when_kickstart_is_refused(restartable):
    e, sp = restartable, Spawner()
    lc = Launchctl(kick_rc=1)
    res = e.al.restart_router(e.roster, e.sdir, e.say, spawn=sp, lock_held_fn=Lease(held=False),
                              launchctl=lc, platform="darwin", uid=501)
    assert len(router_cmds(sp)) == 1 and res["new_pid"] == 1001


def test_main_restart_prints_the_result_and_exits_zero_and_one_on_refusal(restartable, monkeypatch, capsys):
    e = restartable
    monkeypatch.setattr(e.al, "_roster", lambda: e.roster)
    monkeypatch.setattr(e.al, "restart_router", lambda *a, **k: {"stopped": "handshake", "old_pid": 7, "new_pid": 8})
    assert e.al.main(["restart"]) == 0
    assert "router restarted: handshake old pid 7 new pid 8" in capsys.readouterr().out

    def refuse(*a, **k):
        raise e.al.RouterRestartError("lease held")

    monkeypatch.setattr(e.al, "restart_router", refuse)
    assert e.al.main(["restart"]) == 1
    assert "lease held" in capsys.readouterr().err



@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_main_help_flag_prints_usage_and_runs_no_link_pass(flag, monkeypatch, capsys):
    al = load("unimatrix-autolink")

    def boom(*a, **k):
        raise AssertionError("a link pass ran")

    monkeypatch.setattr(al, "run", boom)
    assert al.main([flag]) == 0
    assert "unimatrix-autolink.py" in capsys.readouterr().out


def test_probe_and_pull_dial_ipv4_only(env):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    env.al.probe(env.roster, "machine-a", "/k/relay", run=fake_run)
    env.real_pull_membership(env.roster, "kim@x", "/k/relay", run=fake_run)
    assert len(seen) == 2 and all("AddressFamily=inet" in c for c in seen)


def test_link_key_collapses_case_local_and_trailing_dot():
    al = load("unimatrix-autolink")
    keys = {al.link_key(n) for n in ("example-operator@machine-b", "example-operator@Machine-b.local", "example-operator@MACHINE-B.local.")}
    assert keys == {"example-operator@machine-b"}


def test_link_key_distinguishes_user_and_host_and_allows_no_user():
    al = load("unimatrix-autolink")
    assert al.link_key("alice@machine-b") != al.link_key("example-operator@machine-b")
    assert al.link_key("example-operator@machine-b") != al.link_key("example-operator@machine-a")
    assert al.link_key("Machine-b.local") == "machine-b"


def test_pull_sources_drops_members_dedupes_by_link_key_and_keeps_registry_before_records():
    al = load("unimatrix-autolink")
    got = al.pull_sources({"kim@machine-a.local": "machine-a"}, ["example-operator@machine-b", "machine-a", "kim@Machine-a.local"],
                          ["example-operator@Machine-b.local", "example-operator@bishop"])
    assert got == ["example-operator@machine-b", "example-operator@bishop"]


def test_record_aliases_reads_link_records_and_skips_cache_router_and_backoff_files(env):
    env.sdir.mkdir(parents=True)
    for n in ("autolink-kim_at_bishop.json", "autolink-machine-a.json", "autolink-boxes.json",
              "autolink-router.json", "autolink-machine-a.backoff.json", "autolink-machine-a.lock"):
        (env.sdir / n).write_text("{}")
    assert env.al.record_aliases(env.sdir) == ["kim@bishop", "machine-a"]


def far_boxes_reader(env, aliases):
    (env.home / "bin" / "_machine_local.py").write_text(
        f"import sys\nk = sys.argv[-1]\nprint({aliases!r} if k == 'unimatrix.far_boxes' else '')\n"
        "sys.exit(0 if k == 'unimatrix.far_boxes' else 1)\n")


def roll_back(env, snap, first, second, signer=None):
    """Store epoch 1 (`first` only) and return (epoch 2 snapshot, its signature by `signer`, default the pinned one)."""
    snap.adopt(first)
    held = dict(snap.snap)
    snap.adopt(second)
    higher = snap.snap
    m, d = snap.m, env.sdir / "membership"
    sig = m.FakeSigner(signer or snap.signer, environ={}).sign(m.canonical_bytes(higher))
    (d / "snapshot.json").write_bytes(m.canonical_bytes(held))
    (d / "snapshot.sig").write_bytes(m.FakeSigner(snap.signer, environ={}).sign(m.canonical_bytes(held)))
    (d / "verified.json").unlink()
    return higher, sig


def serving(env, snap, higher, sig):
    asked = []

    def fake_run(cmd, **kw):
        out = snap.m.canonical_bytes(higher) + b"\n" + base64.b64encode(sig) + b"\n"
        return subprocess.CompletedProcess(cmd, 0, out, b"")

    def pull(roster, remote, key):
        asked.append(remote)
        return env.real_pull_membership(roster, remote, key, run=fake_run)

    return asked, pull


@needs_keygen
def test_a_registry_only_alias_serving_a_signed_higher_epoch_is_adopted_and_linked_on_a_healthy_cache(env, snap):
    add_key(env)
    higher, sig = roll_back(env, snap, "machine-a", "bishop")
    far_boxes_reader(env, "boxb")
    seed_live_pair(env, env.al.slug("kim@machine-a.local"))
    (env.sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": ["kim@machine-a.local"], "epoch": 1}))
    asked, pull = serving(env, snap, higher, sig)
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(), key_fn=lambda r, a: str(env.key),
               agent_fn=lambda k: "unencrypted", pull_fn=pull)
    assert asked[0] == "boxb"
    assert snap.m.load_snapshot(env.home)[0]["epoch"] == 2
    assert remotes(sp) == {"kim@bishop.local"}
    assert json.loads((env.sdir / "autolink-boxes.json").read_text())["epoch"] == 2


@needs_keygen
def test_a_registry_only_alias_is_asked_when_the_snapshot_lists_no_other_member(env, snap):
    add_key(env)
    higher, sig = roll_back(env, snap, "me", "bishop")
    far_boxes_reader(env, "boxb")
    asked, pull = serving(env, snap, higher, sig)
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names={"me"}, key_fn=lambda r, a: str(env.key),
               agent_fn=lambda k: "unencrypted", pull_fn=pull)
    assert asked[0] == "boxb" and snap.m.load_snapshot(env.home)[0]["epoch"] == 2
    assert remotes(sp) == {"kim@bishop.local"}


@needs_keygen
def test_an_unsigned_higher_epoch_from_a_registry_only_alias_is_refused_and_logged(env, snap, tmp_path):
    add_key(env)
    higher, sig = roll_back(env, snap, "machine-a", "bishop", signer=_keygen(tmp_path / "rogue")[0])
    far_boxes_reader(env, "boxb")
    asked, pull = serving(env, snap, higher, sig)
    sp = Spawner()
    env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(), key_fn=lambda r, a: str(env.key),
               agent_fn=lambda k: "unencrypted", pull_fn=pull)
    assert asked[0] == "boxb" and snap.m.load_snapshot(env.home)[0]["epoch"] == 1
    assert remotes(sp) == {"kim@machine-a.local"}
    assert any("far box boxb: membership pull skipped" in ln for ln in log_lines(env))


def test_without_a_snapshot_each_configured_alias_is_asked_once(env):
    add_key(env)
    far_boxes_reader(env, "machine-a")
    asked = []
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok, self_names=set(), key_fn=lambda r, a: str(env.key),
               agent_fn=lambda k: "unencrypted", pull_fn=lambda roster, remote, key: asked.append(remote) or False)
    assert asked == ["machine-a"]


def test_the_off_snapshot_pull_is_throttled_and_the_healthy_path_inside_the_interval_spawns_nothing(env, monkeypatch):
    add_key(env)
    seed_live_pair(env, "machine-a")
    (env.sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": ["machine-a"]}))
    spawned = []
    stamp_pull(env)
    with monkeypatch.context() as mp:
        mp.setattr(env.al.subprocess, "run", lambda *a, **k: spawned.append(a) or boom())
        env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, pull_fn=boom)
    assert spawned == [] and not (env.sdir / "autolink.log").exists()
    stamp_pull(env, at=time.time() - env.al.PULL_SOURCES_INTERVAL_SECONDS - 1)
    asked = []
    pull = lambda roster, remote, key: asked.append(remote) or False
    for _ in range(2):
        env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=lambda r, a: str(env.key),
                   agent_fn=lambda k: "unencrypted", pull_fn=pull)
    assert asked == ["machine-a"]
def test_a_niced_fallback_router_is_named_loudly_with_the_launchd_fix():
    al = load("unimatrix-autolink")
    line = al.niced_router_warning(4242, al.LOGIN_AGENT_LABEL, nice_fn=lambda: 10)
    assert "4242" in line and "nice 10" in line and "launchctl kickstart" in line and al.LOGIN_AGENT_LABEL in line
    assert "launchctl" not in al.niced_router_warning(4242, None, nice_fn=lambda: 20)


def test_a_nice_zero_fallback_router_draws_no_warning():
    al = load("unimatrix-autolink")
    assert al.niced_router_warning(4242, al.LOGIN_AGENT_LABEL, nice_fn=lambda: 0) is None


def seed_pair(e, name, pids, box=None):
    e.sdir.mkdir(parents=True, exist_ok=True)
    e.live.update(pids)
    rec = {"bridge": {"pid": pids[0], "procStart": f"start-{pids[0]}"},
           "inbox": {"pid": pids[1], "procStart": f"start-{pids[1]}"}}
    if box:
        rec["box"] = box
    path = e.sdir / f"autolink-{name}.json"
    path.write_text(json.dumps(rec))
    return path


def healthy_cache(e, aliases):
    (e.sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": aliases}))
    stamp_pull(e)


def test_one_pair_per_box_two_spellings(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"farbox-lan": "farbox.local"}.get(a))
    alias_rec = seed_pair(env, "farbox-lan", (2001, 2002))
    member_rec = seed_pair(env, "kim_at_farbox.local", (2003, 2004))
    healthy_cache(env, ["kim@farbox.local"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [2001, 2002]
    assert member_rec.is_file() and not alias_rec.exists()


def test_one_pair_per_box_keeps_the_live_pair_when_only_the_alias_spelling_lives(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"farbox-lan": "farbox.local"}.get(a))
    alias_rec = seed_pair(env, "farbox-lan", (2001, 2002))
    member_rec = seed_pair(env, "kim_at_farbox.local", (2003, 2004))
    env.live.difference_update({2003, 2004})
    healthy_cache(env, ["kim@farbox.local"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [] and alias_rec.is_file() and not member_rec.exists()


def test_one_pair_per_box_never_merges_two_boxes(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"farbox-lan": "elsewhere.local"}.get(a))
    seed_pair(env, "farbox-lan", (2001, 2002))
    seed_pair(env, "kim_at_farbox.local", (2003, 2004))
    healthy_cache(env, ["kim@farbox.local", "farbox-lan"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == []
    assert len(list(env.sdir.glob("autolink-*.json"))) == 3  # two records plus the boxes cache


def test_a_healthy_run_over_stamped_records_spawns_no_ssh(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: calls.append(a))
    seed_pair(env, "farbox-lan", (2001, 2002), box="farbox")
    seed_pair(env, "kim_at_farbox.local", (2003, 2004), box="farbox")
    healthy_cache(env, ["kim@farbox.local"])
    with monkeypatch.context() as mp:
        mp.setattr(env.al.subprocess, "run", lambda *a, **k: calls.append(a) or boom())
        env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=lambda pid: None)
    assert calls == []


def test_a_legacy_record_is_resolved_once_and_stamped_for_the_next_run(env, monkeypatch):
    calls = []
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: calls.append(a) or "farbox.local")
    rec_path = seed_pair(env, "farbox-lan", (2001, 2002))
    healthy_cache(env, ["kim@farbox.local"])
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=lambda pid: None)
    assert calls == ["farbox-lan"] and json.loads(rec_path.read_text())["box"] == "farbox"
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=lambda pid: None)
    assert calls == ["farbox-lan"]


def test_the_link_lock_is_one_file_per_box_across_spellings(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"farbox-lan": "farbox.local"}.get(a))
    env.sdir.mkdir(parents=True)
    (env.sdir / "autolink-farbox.lock").write_text("")
    said = []
    for alias in ("farbox-lan", "kim@farbox.local"):
        env.al.link_peer(env.roster, alias, str(env.key), said.append, spawn=boom, probe_fn=boom)
    assert len(said) == 2 and all("lock held" in line for line in said)


def test_link_peer_folds_the_other_spelling_record_into_one_stamped_record(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"farbox-lan": "farbox.local"}.get(a))
    old = seed_pair(env, "farbox-lan", (2001, 2002))
    env.live.clear()
    sp = Spawner()
    env.al.link_peer(env.roster, "kim@farbox.local", str(env.key), lambda m: None, spawn=sp, probe_fn=ok)
    assert not old.exists()
    rec = json.loads(env.al.record_path(env.sdir, "kim@farbox.local").read_text())
    assert rec["box"] == "farbox" and len(sp.cmds) == 2


def test_run_does_not_spawn_a_second_pair_for_a_box_whose_other_spelling_lives(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"farbox-lan": "farbox.local"}.get(a))
    seed_pair(env, "farbox-lan", (2001, 2002))
    env.al.run(roster=env.roster, boxes=["kim@farbox.local"], spawn=boom, probe_fn=boom, key_fn=boom)
    log = env.sdir / "autolink.log"
    assert "link failed" not in (log.read_text() if log.exists() else "")


def test_retire_non_target_record(env):
    gone = seed_pair(env, "kim_at_oldbox.local", (2001, 2002))
    kept = seed_pair(env, "kim_at_farbox.local", (2003, 2004))
    healthy_cache(env, ["kim@farbox.local"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [2001, 2002]
    assert not gone.exists() and (env.sdir / "retired" / gone.name).is_file() and kept.is_file()


def test_retire_non_target_record_in_a_live_snapshot_run(env, snap):
    snap.adopt("me", "farbox")
    gone = seed_pair(env, "kim_at_oldbox.local", (2001, 2002))
    seed_pair(env, "kim_at_farbox.local", (2003, 2004))
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=ok, key_fn=boom, self_names={"me"},
               pull_fn=lambda *a: False, kill=killed.append)
    assert killed == [2001, 2002] and (env.sdir / "retired" / gone.name).is_file()


def test_retire_spares_a_bare_alias_record_whose_hostname_did_not_resolve(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: None)
    flaky = seed_pair(env, "flaky-lan", (2001, 2002))
    seed_pair(env, "kim_at_farbox.local", (2003, 2004))
    healthy_cache(env, ["kim@farbox.local"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [] and flaky.is_file() and "box" not in json.loads(flaky.read_text())


def test_retire_still_retires_a_bare_alias_record_whose_hostname_resolved_elsewhere(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"flaky-lan": "oldbox.local"}.get(a))
    gone = seed_pair(env, "flaky-lan", (2001, 2002))
    seed_pair(env, "kim_at_farbox.local", (2003, 2004))
    healthy_cache(env, ["kim@farbox.local"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [2001, 2002] and not gone.exists()


def test_retire_skipped_on_failed_target_read(env):
    rec = seed_pair(env, "kim_at_oldbox.local", (2001, 2002))
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)  # reader missing
    assert killed == [] and rec.is_file() and not (env.sdir / "retired").exists()


def test_retire_needs_a_readable_cache_on_the_healthy_path(env):
    rec = seed_pair(env, "example_operator_at_oldbox.local", (2001, 2002))
    (env.sdir / "autolink-boxes.json").write_text("not json")
    stamp_pull(env)
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [] and rec.is_file()


def test_explicit_boxes_never_retire(env):
    rec = seed_pair(env, "example_operator_at_oldbox.local", (2001, 2002))
    seed_pair(env, "example_operator_at_machine_a.local", (2003, 2004))
    env.al.run(roster=env.roster, boxes=["example-operator@machine-a.local"], spawn=boom, probe_fn=boom, key_fn=boom,
               kill=boom)
    assert rec.is_file()


def test_retire_stops_a_live_supervisor_only_on_a_matching_start_stamp(env):
    rec = seed_pair(env, "example_operator_at_oldbox.local", (2001, 2002))
    data = json.loads(rec.read_text())
    data["bridge"]["procStart"] = "reused"
    rec.write_text(json.dumps(data))
    killed = []
    env.al.retire_non_targets(env.roster, env.sdir, lambda m: None, ["example-operator@machine-a.local"], killed.append, {})
    assert killed == [2002] and not rec.exists()


def seed_stamp(e, pid, remote, start=None):
    e.sdir.mkdir(parents=True, exist_ok=True)
    e.live.add(pid)
    stamp = e.sdir / f"supervisor-{pid}.json"
    stamp.write_text(json.dumps({"pid": pid, "procStart": start or f"start-{pid}", "remote": remote}))
    return stamp


def test_unrecorded_live_supervisor_is_stopped_by_a_default_run(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, a, run=None: {"machine-a-lan": "machine-a.local"}.get(a))
    kept = seed_pair(env, "example_operator_at_machine_a.local", (2001, 2002), box="machine-a")
    orphan = seed_stamp(env, 7496, "machine-a-lan")
    recorded = [seed_stamp(env, 2001, "example-operator@machine-a.local"), seed_stamp(env, 2002, "example-operator@machine-a.local")]
    healthy_cache(env, ["example-operator@machine-a.local"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [7496] and not orphan.exists() and kept.is_file() and all(s.is_file() for s in recorded)
    assert any("pid 7496 outside any record" in line for line in log_lines(env))


def test_unrecorded_supervisor_of_a_box_with_no_record_is_stopped(env):
    stamp = seed_stamp(env, 7496, "example-operator@oldbox.local")
    healthy_cache(env, ["example-operator@machine-a.local"])
    killed = []
    env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, kill=killed.append)
    assert killed == [7496] and not stamp.exists()


def test_stamp_with_a_reused_pid_or_a_dead_pid_is_dropped_without_a_kill(env):
    reused = seed_stamp(env, 7496, "example-operator@oldbox.local", start="reused")
    dead = seed_stamp(env, 7497, "example-operator@oldbox.local")
    env.live.discard(7497)
    killed = []
    env.al.reap_unrecorded(env.roster, env.sdir, lambda m: None, killed.append, {})
    assert killed == [] and not reused.exists() and not dead.exists()


def test_unrecorded_supervisor_of_a_box_mid_link_is_left_for_the_next_run(env):
    stamp = seed_stamp(env, 7496, "example-operator@machine-a.local")
    (env.sdir / "autolink-machine-a.lock").write_text("")
    killed = []
    env.al.reap_unrecorded(env.roster, env.sdir, lambda m: None, killed.append, {})
    assert killed == [] and stamp.is_file()


def test_reap_runs_no_ssh_when_every_live_stamp_is_recorded(env, monkeypatch):
    monkeypatch.setattr(env.al, "ssh_hostname", boom)
    seed_pair(env, "machine-a-lan", (2001, 2002), box="machine-a")
    seed_stamp(env, 2001, "machine-a-lan")
    env.al.reap_unrecorded(env.roster, env.sdir, lambda m: None, boom, {})


def test_link_peer_writes_the_record_after_each_spawn(env):
    add_key(env)
    env.sdir.mkdir(parents=True, exist_ok=True)
    seen = []
    base = Spawner()

    def spawn(cmd):
        seen.append(json.loads(env.al.record_path(env.sdir, "machine-a").read_text()) if
                    env.al.record_path(env.sdir, "machine-a").exists() else {})
        if len(base.cmds) == 1:
            raise OSError("second spawn fails")
        return base(cmd)

    with pytest.raises(OSError):
        env.al.link_peer(env.roster, "machine-a", str(env.key), lambda m: None, spawn=spawn, probe_fn=ok)
    rec = json.loads(env.al.record_path(env.sdir, "machine-a").read_text())
    assert seen == [{}, {"bridge": rec["bridge"], "box": "machine-a"}] and rec["bridge"]["pid"] == 1001


def test_link_peer_stamps_each_spawn_and_a_lost_record_pid_is_reaped(env):
    add_key(env)
    env.sdir.mkdir(parents=True, exist_ok=True)
    env.al.link_peer(env.roster, "machine-a", str(env.key), lambda m: None, spawn=Spawner(), probe_fn=ok)
    stamps = sorted(env.sdir.glob("supervisor-*.json"))
    assert [json.loads(s.read_text())["pid"] for s in stamps] == [1001, 1002]
    assert all(json.loads(s.read_text())["remote"] == "machine-a" for s in stamps)
    path = env.al.record_path(env.sdir, "machine-a")
    rec = json.loads(path.read_text())
    rec["bridge"] = {"pid": 9999, "procStart": "start-9999"}  # the record lost the live bridge's pid
    path.write_text(json.dumps(rec))
    env.live.update({1001, 1002, 9999})
    killed = []
    env.al.reap_unrecorded(env.roster, env.sdir, lambda m: None, killed.append, {})
    assert killed == [1001] and not (env.sdir / "supervisor-1001.json").exists()
    assert (env.sdir / "supervisor-1002.json").is_file()


def test_link_peer_keeps_its_lock_fresh_across_the_probe_chain(env):
    add_key(env)
    env.sdir.mkdir(parents=True, exist_ok=True)
    lock = env.sdir / "autolink-machine-a.lock"
    stale = time.time() - 3600
    ages = []

    def probe_fn(roster, alias, key, remote=None):
        if remote is None:  # first probe fails after a long step: the lock goes stale
            os.utime(lock, (stale, stale))
            return "timeout"
        ages.append(time.time() - lock.stat().st_mtime)
        return None

    env.al.link_peer(env.roster, "machine-a", str(env.key), lambda m: None, spawn=Spawner(), probe_fn=probe_fn,
                     stable_fn=lambda alias: "machine-a.local")
    assert ages and ages[0] < 60


def test_supervisor_started_outside_autolink_stamps_itself_and_is_reaped(env):
    # Producer: a supervisor's own main writes the stamp, so one started by install or by hand is reapable.
    pid = os.getpid()
    env.sdir.mkdir(parents=True, exist_ok=True)
    env.roster.stamp_supervisor("example-operator@machine-a.local")
    stamp = env.sdir / f"supervisor-{pid}.json"
    assert json.loads(stamp.read_text()) == {"pid": pid, "procStart": f"start-{pid}", "remote": "example-operator@machine-a.local"}
    env.live.add(pid)
    killed = []
    env.al.reap_unrecorded(env.roster, env.sdir, lambda m: None, killed.append, {})
    assert killed == [pid]
    assert not stamp.exists()


def _seed_box(e, pids=(500, 501), alias="machine-a", stamp_only=()):
    e.sdir.mkdir(parents=True, exist_ok=True)
    rec = {"box": alias}
    for role, pid in zip(("bridge", "inbox"), pids):
        rec[role] = {"pid": pid, "procStart": f"start-{pid}"}
        e.live.add(pid)
        (e.sdir / f"supervisor-{pid}.json").write_text(json.dumps(
            {"pid": pid, "procStart": f"start-{pid}", "remote": alias, "role": role}))
    (e.sdir / f"autolink-{alias}.json").write_text(json.dumps(rec))
    for pid, role in stamp_only:
        e.live.add(pid)
        (e.sdir / f"supervisor-{pid}.json").write_text(json.dumps(
            {"pid": pid, "procStart": f"start-{pid}", "remote": alias, "role": role}))


class _RestartRig:
    def __init__(self, e, honour=True):
        self.e, self.honour, self.spawned, self.stops, self.sleeps = e, honour, [], [], 0

    def spawn(self, cmd):
        self.spawned.append(cmd)
        pid = 900 + len(self.spawned)
        self.e.live.add(pid)
        return FakeProc(pid)

    def sleep(self, _):
        self.sleeps += 1
        for path in sorted(self.e.sdir.glob("stop-*.json")):
            req = json.loads(path.read_text())
            self.stops.append(req)
            if self.honour:
                self.e.live.discard(req["pid"])

    def link(self, roster, alias, key, say, spawn):
        return self.e.al.link_peer(roster, alias, key, say, spawn=spawn, probe_fn=ok)

    def run(self, alias="machine-a"):
        out = []
        rc = self.e.al.cmd_restart_box(alias, link=self.link, spawn=self.spawn, sleep=self.sleep, say=out.append,
                                       roster=self.e.roster)
        return rc, out


def test_restart_box_stops_by_request_then_starts_one_supervisor_per_role(env):
    add_key(env)
    _seed_box(env, stamp_only=[(502, "bridge")])
    rig = _RestartRig(env)
    rc, out = rig.run()
    assert rc == 0, out
    assert sorted(r["pid"] for r in rig.stops) == [500, 501, 502]
    assert {r["pid"]: r["procStart"] for r in rig.stops}[502] == "start-502"
    assert sorted(Path(c[1]).name for c in rig.spawned) == ["unimatrix-mirror-all.py", "unimatrix-roster.py"]
    rec = json.loads((env.sdir / "autolink-machine-a.json").read_text())
    assert {rec["bridge"]["pid"], rec["inbox"]["pid"]} == {901, 902}
    assert not list(env.sdir.glob("stop-*.json")) and not (env.sdir / "autolink-machine-a.lock").exists()
    roles = [json.loads(p.read_text())["role"] for p in env.sdir.glob("supervisor-9*.json")]
    assert sorted(roles) == ["bridge", "inbox"]


def test_restart_box_never_calls_kill(env, monkeypatch):
    add_key(env)
    _seed_box(env)
    calls = []
    monkeypatch.setattr(env.roster, "tree_kill", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(os, "kill", lambda *a, **k: calls.append(a))
    rc, _ = _RestartRig(env).run()
    assert rc == 0 and calls == []


def test_restart_box_survivor_fails_the_verb_and_starts_no_replacement(env):
    add_key(env)
    _seed_box(env)
    rig = _RestartRig(env, honour=False)
    rc, out = rig.run()
    assert rc == 1 and rig.spawned == []
    assert "supervisor bridge pid 500 did not honour the stop request" in out
    assert "supervisor inbox pid 501 did not honour the stop request" in out
    assert (env.sdir / "autolink-machine-a.json").exists() and not (env.sdir / "autolink-machine-a.lock").exists()
    assert not list(env.sdir.glob("stop-*.json"))
    assert len(list(env.sdir.glob("supervisor-*.json"))) == 2


def test_restart_box_refuses_while_the_box_link_lock_is_held(env):
    add_key(env)
    _seed_box(env)
    (env.sdir / "autolink-machine-a.lock").write_text("")
    rig = _RestartRig(env)
    rc, out = rig.run()
    assert rc == 1 and out == ["restart-box: link for machine-a mid-spawn"] and rig.stops == [] and rig.spawned == []


def test_restart_box_unreachable_box_exits_non_zero_naming_the_role_and_count(env):
    add_key(env)
    _seed_box(env)
    rig = _RestartRig(env)
    rig.link = lambda roster, alias, key, say, spawn: None
    rc, out = rig.run()
    assert rc == 1
    assert "restart-box: machine-a has 0 live bridge supervisors after handover" in out
    assert "restart-box: machine-a has 0 live inbox supervisors after handover" in out


def test_restart_box_leaves_another_boxs_supervisors_alone(env):
    add_key(env)
    _seed_box(env)
    _seed_box(env, pids=(600, 601), alias="fallbackbox")
    rig = _RestartRig(env)
    rc, _ = rig.run()
    assert rc == 0 and {r["pid"] for r in rig.stops} == {500, 501}
    assert {600, 601} <= env.live


def test_restart_box_hands_over_a_box_with_no_record_from_its_stamps_alone(env):
    # Bin-copy supervisors started by hand leave stamps and no autolink record; the handover still lands one per role.
    add_key(env)
    _seed_box(env, pids=(), stamp_only=[(700, "bridge"), (701, "inbox")])
    (env.sdir / "autolink-machine-a.json").unlink()
    rig = _RestartRig(env)
    rc, out = rig.run()
    assert rc == 0, out
    assert sorted(r["pid"] for r in rig.stops) == [700, 701]
    assert {700, 701}.isdisjoint(env.live)
    assert sorted(Path(c[1]).name for c in rig.spawned) == ["unimatrix-mirror-all.py", "unimatrix-roster.py"]


def test_restart_box_is_dispatched_from_main(env, monkeypatch):
    seen = []
    monkeypatch.setattr(env.al, "cmd_restart_box", lambda alias: seen.append(alias) or 0)
    assert env.al.main(["restart-box", "machine-a"]) == 0 and seen == ["machine-a"]


def _wire_run(env, monkeypatch, sp, probe_fn=ok, **fixed):
    """main() and restart reach run() with no arguments; route it onto the fake spawn, probe and boxes."""
    real = env.al.run
    calls = []

    def run(**kw):
        calls.append(kw)
        return real(**{"roster": env.roster, "spawn": sp, "probe_fn": probe_fn, **fixed, **kw})

    monkeypatch.setattr(env.al, "run", run)
    monkeypatch.setattr(env.al, "_roster", lambda: env.roster)
    return calls


def test_run_returns_a_summary_naming_the_roles_started_per_alias(env):
    add_key(env)
    summary = env.al.run(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), probe_fn=ok)
    assert summary == [{"alias": "machine-a", "roles_started": ["bridge", "inbox"], "reason": None}]
    assert summary.pending == []
    assert env.al.spawn_lines(summary) == ["spawned bridge+inbox pair for machine-a"]


def test_run_summary_names_an_unreachable_alias_as_pending_and_a_keyless_one_as_final(env, tmp_path):
    add_key(env)
    bad = lambda roster, alias, key, remote=None: "rc=255"  # noqa: E731
    down = env.al.run(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), probe_fn=bad)
    assert down == [{"alias": "machine-a", "roles_started": [], "reason": "unreachable: rc=255"}]
    assert down.pending == ["machine-a"] and env.al.spawn_lines(down) == []
    keyless = env.al.run(roster=env.roster, boxes=["nokey"], spawn=Spawner(), probe_fn=ok,
                         key_fn=lambda r, a: str(tmp_path / "absent"))
    assert keyless == [{"alias": "nokey", "roles_started": [], "reason": "no relay key"}] and keyless.pending == []


def test_healthy_cache_returns_an_empty_summary_with_nothing_pending(env):
    stamp_pull(env)
    seed_live_pair(env, "machine-a")
    (env.sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": ["machine-a"]}))
    summary = env.al.run(roster=env.roster, spawn=boom, probe_fn=boom, key_fn=boom, stable_fn=boom)
    assert summary == [] and summary.pending == []


def test_main_prints_one_spawn_line_per_started_pair_and_exits_zero(env, monkeypatch, capsys):
    add_key(env)
    _wire_run(env, monkeypatch, Spawner(), boxes=["machine-a", "other"])
    assert env.al.main([]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == ["spawned bridge+inbox pair for machine-a", "spawned bridge+inbox pair for other"]
    assert env.al.main([]) == 0  # both pairs are recorded as started but their fake pids are not live: restarted
    env.live.update(r["pid"] for p in env.sdir.glob("autolink-*.json") for r in json.loads(p.read_text()).values()
                    if isinstance(r, dict))
    capsys.readouterr()
    assert env.al.main([]) == 0
    assert capsys.readouterr().out == ""


def test_main_prints_one_stderr_line_when_the_pass_raises_and_still_exits_zero(env, monkeypatch, capsys):
    def explode(**kw):
        raise ValueError("no network yet")

    monkeypatch.setattr(env.al, "run", explode)
    assert env.al.main([]) == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err.strip() == "autolink failed: ValueError: no network yet"


def test_restart_relinks_after_the_router_line_and_bypasses_the_backoff(env, monkeypatch, capsys):
    add_key(env)
    monkeypatch.setattr(env.al, "restart_router", lambda *a, **k: {"stopped": "handshake", "old_pid": 7, "new_pid": 8})
    calls = _wire_run(env, monkeypatch, Spawner(), boxes=["machine-a"])
    assert env.al.main(["restart"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "router restarted: handshake old pid 7 new pid 8", "spawned bridge+inbox pair for machine-a"]
    assert calls[0]["ignore_backoff"] is True


def test_restart_that_refuses_links_nothing(env, monkeypatch):
    def refuse(*a, **k):
        raise env.al.RouterRestartError("lease held")

    monkeypatch.setattr(env.al, "restart_router", refuse)
    calls = _wire_run(env, monkeypatch, Spawner())
    assert env.al.main(["restart"]) == 1 and calls == []


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, n):
        self.sleeps.append(n)
        self.t += n


def _summary(env, *pending):
    s = env.al.LinkSummary()
    for alias in pending:
        s.add(alias, [], "unreachable: rc=255")
    return s


def test_boot_stops_at_the_first_pass_when_every_pair_is_live(env, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(env.al, "run", lambda **kw: _summary(env))
    assert env.al.cmd_boot(sleep=clock.sleep, clock=clock.monotonic) == 0
    assert clock.sleeps == []


def test_boot_polls_every_30s_until_the_pair_is_live_then_stops(env, monkeypatch, capsys):
    clock = _Clock()
    passes = []

    def run(**kw):
        passes.append(kw)
        if len(passes) < 3:
            return _summary(env, "machine-a")
        s = env.al.LinkSummary()
        s.add("machine-a", ["bridge", "inbox"], None)
        return s

    monkeypatch.setattr(env.al, "run", run)
    assert env.al.cmd_boot(sleep=clock.sleep, clock=clock.monotonic) == 0
    assert clock.sleeps == [env.al.BOOT_POLL_SECONDS] * 2 and len(passes) == 3
    assert all(p["ignore_backoff"] is True for p in passes)
    assert capsys.readouterr().out.splitlines() == ["spawned bridge+inbox pair for machine-a"]


def test_boot_gives_up_at_the_window_for_a_box_that_never_answers_and_exits_zero(env, monkeypatch):
    clock = _Clock()
    passes = []
    monkeypatch.setattr(env.al, "run", lambda **kw: passes.append(1) or _summary(env, "sleeping"))
    assert env.al.cmd_boot(sleep=clock.sleep, clock=clock.monotonic) == 0
    assert clock.t == env.al.BOOT_WINDOW_SECONDS == 600
    assert len(passes) == 600 // env.al.BOOT_POLL_SECONDS + 1


def test_boot_retries_a_pass_that_raises_until_the_network_is_up(env, monkeypatch, capsys):
    clock = _Clock()
    calls = []

    def run(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("network unreachable")
        return _summary(env)

    monkeypatch.setattr(env.al, "run", run)
    assert env.al.cmd_boot(sleep=clock.sleep, clock=clock.monotonic) == 0
    assert len(calls) == 2 and "autolink failed: OSError: network unreachable" in capsys.readouterr().err


def test_boot_pass_two_links_a_box_whose_pass_one_probe_charged_a_backoff(env, monkeypatch, capsys):
    add_key(env)
    clock = _Clock()
    state = {"up": False}

    def probe_fn(roster, alias, key, remote=None):
        return None if state["up"] else "rc=255"

    real_sleep = clock.sleep

    def sleep(n):
        state["up"] = True
        real_sleep(n)

    _wire_run(env, monkeypatch, Spawner(), probe_fn=probe_fn, boxes=["machine-a"])
    assert env.al.cmd_boot(sleep=sleep, clock=clock.monotonic) == 0
    assert capsys.readouterr().out.splitlines() == ["spawned bridge+inbox pair for machine-a"]


def test_plain_run_skips_a_box_inside_its_backoff_and_ignore_backoff_probes_it(env):
    add_key(env)
    bad = lambda roster, alias, key, remote=None: "rc=255"  # noqa: E731
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), probe_fn=bad)
    skipped = env.al.run(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), probe_fn=ok)
    assert skipped == [{"alias": "machine-a", "roles_started": [], "reason": "probe backoff"}]
    linked = env.al.run(roster=env.roster, boxes=["machine-a"], spawn=Spawner(), probe_fn=ok, ignore_backoff=True)
    assert linked[0]["roles_started"] == ["bridge", "inbox"]


def test_resolve_key_tries_the_machine_local_key_of_each_matching_alias_before_ssh_and_default(env, tmp_path):
    other = write_key(env.home / "other-key")
    ssh_calls = []
    local = lambda r, a: other if a == "other" else None  # noqa: E731
    ssh = lambda r, a: ssh_calls.append(a)  # noqa: E731
    via = lambda: iter(["macmini-lan", "other"])  # noqa: E731
    key = env.al.resolve_key(env.roster, "macmini-lan", local, ssh, via=via)
    assert key == other and ssh_calls == []
    assert env.al.resolve_key(env.roster, "macmini-lan", lambda r, a: write_key(env.home / "own"), ssh,
                              via=via) == str(env.home / "own")
    assert env.al.resolve_key(env.roster, "macmini-lan", lambda r, a: None, lambda r, a: None,
                              via=via) == str(env.key)


def test_ssh_identity_file_skips_pub_key_and_picks_next_private_key(env, tmp_path):
    pub = tmp_path / "unimatrix-1p.pub"
    pub.write_text("ssh-ed25519 AAAA test\n", encoding="utf-8")
    priv = tmp_path / "unimatrix-priv"
    priv.write_text("PRIVATE", encoding="utf-8")

    def run(cmd, **_kw):
        out = f"identityfile {pub}\nidentityfile {priv}\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    assert env.real_ssh_identity_file(env.roster, "machine-a-1p", run=run) == str(priv)

    def run_pub_only(cmd, **_kw):
        return subprocess.CompletedProcess(cmd, 0, f"identityfile {pub}\n", "")

    assert env.real_ssh_identity_file(env.roster, "machine-a-1p", run=run_pub_only) is None


def test_resolve_key_falls_back_to_via_alias_ssh_identity_file(env, tmp_path):
    via_key = write_key(env.home / "via-macmini-key")
    ssh_calls = []

    def ssh(r, a):
        ssh_calls.append(a)
        return via_key if a == "macmini-lan" else None

    via = lambda: iter(["example-operator@Mac-mini.local", "macmini-lan"])  # noqa: E731
    got = env.al.resolve_key(env.roster, "example-operator@Mac-mini.local", lambda r, a: None, ssh, via=via)
    assert got == via_key
    assert ssh_calls == ["example-operator@Mac-mini.local", "macmini-lan"]


def test_matching_aliases_lists_name_matches_before_known_host_key_matches(env):
    entries = [(["Mac-mini.local"], "ssh-ed25519 AAAAmini"), (["100.64.0.9"], "ssh-ed25519 AAAAmini")]
    hosts = {"other": "100.64.0.9", "macmini-lan": "mac-mini"}
    got = list(env.al.matching_aliases(env.roster, "Mac-mini.local", ["other", "macmini-lan", "unrelated"], hosts,
                                       entries=entries))
    assert got == ["macmini-lan", "other"]
    assert env.al.member_alias(env.roster, "Mac-mini", "Mac-mini.local", ["other", "macmini-lan"], hosts,
                               entries=entries) == "macmini-lan"


@needs_keygen
def test_snapshot_member_links_with_the_key_set_only_under_a_host_key_matched_alias(env, snap, monkeypatch):
    add_key(env)
    snap.adopt("Mac-mini")
    mini_key = write_key(env.home / "keys" / "unimatrix-gipsy-lan")
    (env.home / "bin" / "_machine_local.py").write_text(
        "import sys\nk = sys.argv[-1]\n"
        f"vals = {{'unimatrix.far_boxes': 'macmini-lan other', 'unimatrix.keys.other': {mini_key!r}}}\n"
        "print(vals.get(k, ''))\nsys.exit(0 if k in vals else 1)\n")
    hosts = {"macmini-lan": "mac-mini.local", "other": "100.64.0.9"}
    monkeypatch.setattr(env.al, "ssh_hostname", lambda roster, alias, run=None: hosts.get(alias))
    monkeypatch.setattr(env.al, "_known_hosts_entries", lambda *a, **k: [
        (["Mac-mini.local"], "ssh-ed25519 AAAAmini"), (["100.64.0.9"], "ssh-ed25519 AAAAmini")])
    sp = Spawner()
    summary = env.al.run(roster=env.roster, spawn=sp, probe_fn=ok, self_names=set(), pull_fn=lambda *a: False)
    assert [key_of(c) for c in sp.cmds] == [mini_key, mini_key]
    assert env.al.spawn_lines(summary) == ["spawned bridge+inbox pair for kim@Mac-mini.local"]


def lan_route_for(env, endpoint="kim@192.0.2.7"):
    far = env.al._far()
    return far.Route("machine-a", "machine-a", endpoint, "lan", str(env.key), None, far.OK)


def remotes(sp):
    return {c[c.index("--remote") + 1] for c in sp.cmds}


def test_verified_lan_route_replaces_the_off_lan_probe_and_supervisors_keep_the_logical_peer(env):
    add_key(env)
    sp = Spawner()
    seen = []
    probe_fn = stale_ip_probe(set())
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn, stable_fn=lambda a: None,
               lan_fn=lambda roster, alias, key: seen.append((alias, key)) or lan_route_for(env))
    assert seen == [("machine-a", str(env.key))] and probe_fn.seen == []
    assert remotes(sp) == {"machine-a"} and len(sp.cmds) == 2
    assert {c[c.index("--key") + 1] for c in sp.cmds} == {str(env.key)}
    assert "verified LAN route kim@192.0.2.7" in (env.sdir / "autolink.log").read_text()


def test_no_lan_route_keeps_the_recorded_name_and_probe_chain(env):
    add_key(env)
    sp = Spawner()
    probe_fn = stale_ip_probe({"machine-a"})
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn,
               lan_fn=lambda *a: None)
    assert probe_fn.seen == ["machine-a"] and remotes(sp) == {"machine-a"}


def test_unverified_lan_falls_back_to_the_authenticated_stable_route(env):
    add_key(env)
    sp = Spawner()
    probe_fn = stale_ip_probe({"machine-a-lan"})
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=probe_fn,
               stable_fn=lambda a: "machine-a-lan", lan_fn=lambda *a: None)
    assert remotes(sp) == {"machine-a-lan"}


def test_working_pair_is_never_replaced_to_claim_lan(env):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=ok, lan_fn=lambda *a: None)
    env.live.update({1001, 1002})

    def boom(*a, **k):
        raise AssertionError("a live pair is not re-routed")
    env.al.run(roster=env.roster, boxes=["machine-a"], spawn=sp, probe_fn=boom, stable_fn=boom,
               lan_fn=lambda *a: lan_route_for(env) if boom() else None)
    assert len(sp.cmds) == 2


def far_args(far, addresses, status, hostkey="SHA256:pinned-machine-a"):
    values = {"unimatrix.addresses.machine-a": addresses, "unimatrix.hostkey.machine-a": hostkey}
    verified = []

    def verify(target):
        verified.append(target)
        return status(target)
    return dict(far=far, read_key=lambda name: values.get(name, ""), connect=lambda a: True, verify=verify), verified


def test_lan_route_returns_the_first_host_key_verified_address(env):
    far = env.al._far()
    kw, verified = far_args(far, "192.0.2.5, 192.0.2.7",
                            lambda t: (far.NOT_THIS_BOX, "") if t.endswith("5") else (far.OK, ""))
    route = env.al.lan_route(env.roster, "kim@machine-a", str(env.key), **kw)
    assert (route.transport, route.endpoint, route.peer) == ("lan", "kim@192.0.2.7", "machine-a")
    assert verified == ["kim@192.0.2.5", "kim@192.0.2.7"]


def test_lan_route_never_takes_lan_for_a_peer_with_no_pinned_host_key(env):
    far = env.al._far()
    kw, verified = far_args(far, "192.0.2.7", lambda t: (far.OK, ""), hostkey="")
    assert env.al.lan_route(env.roster, "kim@machine-a", str(env.key), **kw) is None and verified == []

def test_lan_route_refuses_a_wrong_peer_address_and_an_unreachable_one(env):
    far = env.al._far()
    kw, verified = far_args(far, "192.0.2.5", lambda t: (far.NOT_THIS_BOX, ""))
    assert env.al.lan_route(env.roster, "kim@machine-a", str(env.key), **kw) is None
    kw, _ = far_args(far, "192.0.2.5", lambda t: (far.OK, ""))
    kw["connect"] = lambda a: False
    assert env.al.lan_route(env.roster, "kim@machine-a", str(env.key), **kw) is None


def test_lan_route_with_no_address_list_spawns_no_verify_and_a_failure_reads_as_none(env):
    far = env.al._far()
    kw, verified = far_args(far, "", lambda t: (far.OK, ""))
    assert env.al.lan_route(env.roster, "kim@machine-a", str(env.key), **kw) is None and verified == []

    def boom(name):
        raise OSError("reader")
    assert env.al.lan_route(env.roster, "kim@machine-a", str(env.key), far=far, read_key=boom) is None


def test_restart_box_old_supervisor_back_alive_is_not_handed_over_and_is_named(env):
    add_key(env)
    _seed_box(env)
    rig = _RestartRig(env)

    def link_revives_old(roster, alias, key, say, spawn):
        env.live.update({500, 501})  # the old pair outlives the stop; nothing new is spawned
        return None

    rig.link = link_revives_old
    rc, out = rig.run()
    assert rc == 1
    assert "restart-box: machine-a old bridge supervisor pid 500 is still running" in out
    assert "restart-box: machine-a old inbox supervisor pid 501 is still running" in out
    assert not any("handed over" in line for line in out)
