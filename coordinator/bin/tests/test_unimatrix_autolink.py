"""Behavior pins for the unimatrix far-box autolink launcher: ssh and process spawns are faked."""
import importlib.util
import json
import os
import subprocess
import sys
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
    monkeypatch.delenv("UNIMATRIX_TUNNEL_REMOTE", raising=False)
    al = load("unimatrix-autolink")
    real_ssh = al.ssh_identity_file
    monkeypatch.setattr(al, "ssh_identity_file", lambda roster, alias, run=None: None)
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


def test_cli_entrypoint_unconfigured_is_silent_exit_zero_and_logs_reason(tmp_path):
    home = tmp_path / "s"
    home.mkdir()
    r = subprocess.run([sys.executable, str(BIN / "unimatrix-autolink.py")], capture_output=True, text=True,
                       env={**os.environ, "COORDINATOR_SETTINGS_HOME": str(home)})
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert "no supervisors started" in (home / "unimatrix" / "autolink.log").read_text()


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


def test_missing_membership_library_means_transition_fallback(env, monkeypatch):
    def gone():
        raise FileNotFoundError("unimatrix-membership.py")

    monkeypatch.setattr(env.al, "_membership", gone)
    assert env.al.member_targets(env.roster)[0] == "none"
