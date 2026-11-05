"""Behavior pins for the unimatrix far-box autolink launcher: ssh and process spawns are faked."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
    monkeypatch.delenv("UNIMATRIX_TUNNEL_REMOTE", raising=False)
    al = load("unimatrix-autolink")
    real_ssh = al.ssh_identity_file
    monkeypatch.setattr(al, "ssh_identity_file", lambda roster, alias, run=None: None)
    roster = al._roster()
    live = set()
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(roster, "proc_start", lambda pid: f"start-{pid}")

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


def test_unconfigured_exits_silently_without_touching_state(env, capsys):
    env.al.run(roster=env.roster, spawn=Spawner(), probe_fn=ok)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert not env.sdir.exists()


def test_no_far_box_with_key_present_is_silent(env, capsys):
    add_key(env)
    sp = Spawner()
    env.al.run(roster=env.roster, boxes=[], spawn=sp, probe_fn=ok)
    assert sp.cmds == [] and capsys.readouterr().out == ""


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
    local, ssh = write_key(tmp_path / "local"), write_key(tmp_path / "ssh")
    add_key(env)
    r = env.roster
    assert env.al.resolve_key(r, "a", lambda r, a: local, lambda r, a: ssh) == local
    assert env.al.resolve_key(r, "a", lambda r, a: None, lambda r, a: ssh) == ssh
    assert env.al.resolve_key(r, "a", lambda r, a: None, lambda r, a: None) == str(env.key)


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


def test_cli_entrypoint_unconfigured_is_silent_exit_zero(tmp_path):
    home = tmp_path / "s"
    home.mkdir()
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-autolink.py")], capture_output=True, text=True,
                       env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(home)})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
