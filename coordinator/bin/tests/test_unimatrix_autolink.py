"""Behavior pins for the unimatrix far-box autolink launcher: ssh and process spawns are faked."""
import importlib.util
import json
import os
import re
import subprocess
import sys
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
    monkeypatch.setattr(al, "ssh_identity_file", lambda roster, alias, run=None: None)
    monkeypatch.setattr(al, "pin_key_value", lambda roster, run=None: None)
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
    assert set(rec) == {"bridge", "inbox"}
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
    assert cmd[:3] == ["ssh", "-i", "/k/relay"]
    assert cmd.count("-i") == 1
    assert ["-o", "IdentitiesOnly=yes"] == cmd[3:5]
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
    with pytest.raises(RuntimeError, match="test home refuses real key_path"):
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


def test_cli_entrypoint_unconfigured_is_silent_exit_zero_and_logs_reason(tmp_path):
    home = tmp_path / "s"
    home.mkdir()
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-autolink.py")], capture_output=True, text=True,
                       env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(home),
                            "UNIMATRIX_AUTOLINK_NO_ROUTER": "1"})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert "no supervisors started" in (home / "unimatrix" / "autolink.log").read_text()


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


def test_healthy_cache_makes_no_key_probe_or_spawn_call_and_logs_nothing(env):
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
    assert seen == ["macmini-lan"]


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
    assert (env.sdir / f"autolink-{env.al.slug('kim@machine-a.local')}.json").is_file()


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
               pull_fn=lambda roster, remote, key: env.al.pull_membership(roster, remote, key, run=fake_run))
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
    assert env.al.pull_membership(env.roster, "kim@x", "/k", run=same) is False
    assert env.al.pull_membership(env.roster, "kim@x", "/k", run=failed) is False
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
    env.al.pull_membership(env.roster, "kim@x", "/k/relay", run=fake_run)
    assert len(seen) == 2 and all("AddressFamily=inet" in c for c in seen)
