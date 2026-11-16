"""One supervisor pair per box: records keyed by link_key, a second spelling's pair stopped. Spawn and kill are faked."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
SHORT, LONG = "example-operator@machine-b", "example-operator@Machine-b.local"


class FakeProc:
    def __init__(self, pid):
        self.pid = pid


class Spawner:
    def __init__(self):
        self.cmds = []
        self.next_pid = 5000

    def __call__(self, cmd):
        self.cmds.append(cmd)
        self.next_pid += 1
        return FakeProc(self.next_pid)


class Env:
    pass


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
    monkeypatch.setenv("UNIMATRIX_TEST_ALLOWED_BOXES", "example-operator@machine-b example-operator@Machine-b.local other")
    spec = importlib.util.spec_from_file_location("unimatrix_autolink", BIN / "unimatrix-autolink.py")
    al = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(al)
    monkeypatch.setattr(al, "pull_membership", lambda *a, **k: False)
    monkeypatch.setattr(al, "ssh_identity_file", lambda roster, alias, run=None: None)
    monkeypatch.setattr(al, "pin_key_value", lambda roster, run=None: None)
    roster = al._roster()
    live = set()
    monkeypatch.setattr(roster, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(roster, "proc_start", lambda pid: f"start-{pid}")
    liveness = al._liveness()
    monkeypatch.setattr(liveness, "pid_alive", lambda pid: pid in live)
    monkeypatch.setattr(liveness, "proc_start", lambda pid, platform=None: roster.proc_start(pid))
    e = Env()
    e.home, e.al, e.roster, e.live = home, al, roster, live
    e.sdir = home / "unimatrix"
    e.sdir.mkdir(parents=True)
    e.key = Path(roster.default_key())
    e.key.parent.mkdir(parents=True)
    e.key.write_text("k")
    e.killed = []

    def kill(pid):
        e.killed.append(pid)
        live.discard(pid)

    e.kill = kill
    (home / "bin" / "_machine_local.py").write_text(f"print({SHORT!r})\n")
    return e


def seed(e, alias, pids, stamps=None, slug_of=None):
    e.live.update(pids)
    stamps = stamps or [f"start-{p}" for p in pids]
    rec = {"bridge": {"pid": pids[0], "procStart": stamps[0]}, "inbox": {"pid": pids[1], "procStart": stamps[1]}}
    path = e.sdir / f"autolink-{(slug_of or e.al.slug)(alias)}.json"
    path.write_text(json.dumps(rec))
    return path


def ok(roster, alias, key, remote=None):
    return None


def records(e):
    return sorted(p.name for _, p in e.al.record_files(e.sdir))


def run(e, sp, **kw):
    e.al.run(roster=e.roster, spawn=sp, probe_fn=ok, self_names=set(), key_fn=lambda r, a: str(e.key),
             pull_fn=lambda *a: False, kill=e.kill, **kw)


def test_two_live_pairs_for_one_box_end_as_one_pair_and_spawn_nothing(env):
    seed(env, SHORT, (2001, 2002))
    seed(env, LONG, (2003, 2004))
    sp = Spawner()
    run(env, sp)
    assert sp.cmds == []
    assert sorted(env.killed) == [2003, 2004]
    assert env.live == {2001, 2002}
    assert len(records(env)) == 1
    assert any("duplicate supervisor pair stopped" in ln for ln in (env.sdir / "autolink.log").read_text().splitlines())


def test_the_duplicate_is_stopped_on_a_healthy_cache_too(env):
    seed(env, SHORT, (2001, 2002))
    seed(env, LONG, (2003, 2004))
    (env.sdir / "autolink-boxes.json").write_text(json.dumps({"aliases": [SHORT]}))
    sp = Spawner()
    run(env, sp)
    assert sp.cmds == [] and sorted(env.killed) == [2003, 2004] and len(records(env)) == 1


def test_the_canonical_record_is_the_one_kept(env):
    canon = env.al.record_path(env.sdir, SHORT)
    seed(env, LONG, (2003, 2004))
    seed(env, SHORT, (2001, 2002), slug_of=lambda a: env.al.slug(env.al.link_key(a)))
    assert canon.is_file()
    run(env, Spawner())
    assert env.live == {2001, 2002} and canon.is_file() and len(records(env)) == 1


def test_a_record_that_copies_the_kept_pair_is_removed_without_a_kill(env):
    seed(env, SHORT, (2001, 2002))
    seed(env, LONG, (2001, 2002))
    run(env, Spawner())
    assert env.killed == [] and env.live == {2001, 2002} and len(records(env)) == 1


def test_a_pid_whose_process_start_differs_is_never_killed(env):
    seed(env, SHORT, (2001, 2002))
    seed(env, LONG, (2003, 2004), stamps=["start-other", "start-other"])
    run(env, Spawner())
    assert env.killed == [] and 2003 in env.live and 2004 in env.live


def test_a_different_box_keeps_its_own_pair(env):
    seed(env, SHORT, (2001, 2002))
    seed(env, "example-operator@other", (2003, 2004))
    run(env, Spawner())
    assert env.killed == [] and env.live == {2001, 2002, 2003, 2004} and len(records(env)) == 2


def test_two_spellings_with_no_live_pair_collapse_to_one_link_under_the_canonical_record(env):
    (env.home / "bin" / "_machine_local.py").write_text(f"print({SHORT!r} + ',' + {LONG!r})\n")
    sp = Spawner()
    run(env, sp)
    assert len(sp.cmds) == 2 and {c[c.index("--remote") + 1] for c in sp.cmds} == {SHORT}
    assert [n for n in records(env)] == ["autolink-example_operator_at_machine_b.json"]


def test_an_old_raw_slug_record_is_read_once_then_folded_into_the_canonical_one(env):
    legacy = seed(env, LONG, (2001, 2002))
    sp = Spawner()
    env.al.link_peer(env.roster, LONG, str(env.key), lambda m: None, spawn=sp, probe_fn=ok)
    canon = env.al.record_path(env.sdir, LONG)
    assert sp.cmds == [] and canon.is_file() and not legacy.exists()
    assert json.loads(canon.read_text())["bridge"]["pid"] == 2001
    assert canon.name == "autolink-example_operator_at_machine_b.json"


def test_a_link_in_flight_for_the_box_defers_the_duplicate_cleanup(env):
    seed(env, SHORT, (2001, 2002))
    seed(env, LONG, (2003, 2004))
    lock = env.sdir / f"autolink-{env.al.slug(env.al.link_key(SHORT))}.lock"
    lock.write_text("")
    run(env, Spawner())
    assert env.killed == [] and env.live == {2001, 2002, 2003, 2004} and len(records(env)) == 2 and lock.exists()
    lock.unlink()
    run(env, Spawner())
    assert sorted(env.killed) == [2003, 2004] and len(records(env)) == 1
