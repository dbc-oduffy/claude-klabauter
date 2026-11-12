"""A restarted far peer keeps its socket; its relay pid still needs refreshing."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "restart_roster", Path(__file__).resolve().parents[1] / "unimatrix-roster.py")
roster = importlib.util.module_from_spec(spec)
spec.loader.exec_module(roster)


@pytest.mark.parametrize("name", ["codex-repo-12", None])
def test_existing_bridge_refreshes_far_pid_without_changing_local_identity(tmp_path, monkeypatch, name):
    sock = "/tmp/cc-socks/stable.sock"
    local = {"pid": 31337, "procStart": "local-start", "pidDomain": "win32:box",
             "name": "codex-repo-12", "farPid": 62725, "messagingSocketPath": "local-pipe",
             "macSocketPath": sock}
    path = tmp_path / "31337.json"
    path.write_text(json.dumps(local))
    monkeypatch.setattr(roster, "sessions_dir", lambda: str(tmp_path))
    monkeypatch.setattr(roster, "live_sessions", lambda: [])
    monkeypatch.setattr(roster, "mirror", lambda *a: pytest.fail("restarted live bridge"))
    bridge = SimpleNamespace(pid=31337, poll=lambda: None)
    bridges = {sock: bridge}
    entry = {"pid": 80954, "procStart": "far-start", "messagingSocketPath": sock}
    if name is not None:
        entry["name"] = name
    row = {"entry": entry, "key": "rotated-far-key"}
    cfg = SimpleNamespace(remote="macmini")
    roster.sync(bridges, {sock: row}, cfg, lambda *a: None)
    updated = json.loads(path.read_text())
    assert isinstance(updated.pop("unimatrixFarSeen"), int)
    expected = {**local, "farPid": 80954, "box": "macmini", "unimatrixBox": "macmini", "vendor": "unknown", "unimatrixRemote": "macmini",
                **roster.supervisor_fields()}
    if name is not None:
        expected["unimatrixFarName"] = name
        expected["name"] = roster.labelled(name, cfg.remote)
    assert updated == expected
    assert bridges[sock] is bridge
    monkeypatch.setattr(roster, "write_json_atomic", lambda *a: pytest.fail("rewrote unchanged mirror"))
    roster.sync(bridges, {sock: row}, cfg, lambda *a: None)


def test_far_names_never_collapse_onto_one_near_session():
    scan = [("alpha", "local-sock")]
    a = {"name": "alpha", "pid": 1}
    b = {"name": "beta", "pid": 2}
    na = roster.mirror_entry_name(a, "macmini", "/s/a", scan)
    nb = roster.mirror_entry_name(b, "macmini", "/s/b", scan)
    assert na != nb and "alpha" not in {na, nb}
    assert nb == "beta.macmini"


def _mirror_all_rig(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "unimatrix-mirror-all.py"
    s = importlib.util.spec_from_file_location("restart_mirror_all", path)
    mod = importlib.util.module_from_spec(s)
    s.loader.exec_module(mod)
    procs, clock = [], [1000.0]

    def fake_spawn(cmd):
        p = SimpleNamespace(pid=900 + len(procs), rc=None)
        p.poll = lambda p=p: p.rc
        procs.append(p)
        return p

    monkeypatch.setattr(mod.R, "spawn", fake_spawn)
    monkeypatch.setattr(mod.R, "tree_kill", lambda pid: None)
    monkeypatch.setattr(mod, "_now", lambda: clock[0])
    monkeypatch.setitem(mod._STATE, "fails", {})
    monkeypatch.setitem(mod._STATE, "retry_at", {})
    monkeypatch.setitem(mod._STATE, "absent", {})
    monkeypatch.setitem(mod._STATE, "up_since", {})
    monkeypatch.setitem(mod._STATE, "rc", {})
    monkeypatch.setitem(mod._STATE, "state_sig", None)
    monkeypatch.setitem(mod._STATE, "unsupported", False)
    monkeypatch.setattr(mod, "_PUBLISHED", {})
    mod.sent = []
    monkeypatch.setattr(mod, "_start_publish", lambda cfg, cmds, say: mod.sent.extend(cmds))
    cfg =mod.parse_args([str(tmp_path / "m.log"), "--remote", "farbox", "--key", "k"])
    return mod, procs, clock, cfg


def test_a_client_that_keeps_dying_backs_off_across_deaths_not_resetting_per_poll(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}}
    clients = {}
    mod.sync(clients, live, cfg, lambda m: None)
    waits = []
    for _ in range(3):
        procs[-1].rc = 255
        mod.sync(clients, live, cfg, lambda m: None)  # death seen
        waits.append(mod._STATE["retry_at"]["a"] - clock[0])
        clock[0] += waits[-1] + 1
        mod.sync(clients, live, cfg, lambda m: None)  # respawn
        clock[0] += 60  # alive a poll or two, far short of STABLE_SECONDS
        mod.sync(clients, live, cfg, lambda m: None)
    assert waits == [30, 60, 120] and len(procs) == 4


def test_a_client_up_for_the_stable_window_resets_the_backoff(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}}
    clients = {}
    mod.sync(clients, live, cfg, lambda m: None)
    procs[-1].rc = 255
    mod.sync(clients, live, cfg, lambda m: None)
    clock[0] += 31
    mod.sync(clients, live, cfg, lambda m: None)
    assert mod._STATE["fails"] == {"a": 1}
    clock[0] += mod.STABLE_SECONDS - 1
    mod.sync(clients, live, cfg, lambda m: None)
    assert mod._STATE["fails"] == {"a": 1}
    clock[0] += 1
    mod.sync(clients, live, cfg, lambda m: None)
    assert mod._STATE["fails"] == {}


def _state(tmp_path):
    return json.loads((tmp_path / "unimatrix-mirror-state.json").read_text())


def test_rc3_is_a_quiet_recorded_stop_with_no_backoff_or_redial(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}, "b": {"pid": 2, "messagingSocketPath": "e2"}}
    clients, said = {}, []
    mod.sync(clients, live, cfg, said.append)
    assert len(procs) == 2 and {v["state"] for v in _state(tmp_path).values()} == {"up"}
    procs[0].rc = mod.UNSUPPORTED_HOST_RC
    mod.sync(clients, live, cfg, said.append)
    for _ in range(5):
        clock[0] += 10_000
        mod.sync(clients, live, cfg, said.append)
    assert len(procs) == 2
    assert mod._STATE["retry_at"] == {} and mod._STATE["fails"] == {}
    assert len([m for m in said if "rc=3" in m]) == 1
    state = _state(tmp_path)
    assert state["a"]["state"] == "host-refused" and state["a"]["rc"] == 3
    assert state["b"]["state"] == "host-refused"


@pytest.mark.parametrize("rc", [1, 255])
def test_the_host_refusal_text_latches_at_any_rc(tmp_path, monkeypatch, rc):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}}
    clients, said = {}, []
    mod.sync(clients, live, cfg, said.append)
    (tmp_path / "unimatrix-inbox-a.log").write_text("inbox: mirror sessions are hosted on macOS/Linux boxes only\n")
    procs[0].rc = rc
    for _ in range(4):
        mod.sync(clients, live, cfg, said.append)
        clock[0] += 10_000
    assert len(procs) == 1 and mod._STATE["retry_at"] == {}
    assert _state(tmp_path)["a"]["state"] == "host-refused"


def test_an_unrelated_failure_log_still_backs_off(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}}
    clients = {}
    mod.sync(clients, live, cfg, lambda m: None)
    (tmp_path / "unimatrix-inbox-a.log").write_text("ssh: connection reset\n")
    procs[0].rc = 1
    mod.sync(clients, live, cfg, lambda m: None)
    assert _state(tmp_path)["a"]["state"] == "backoff"


def test_a_dying_client_is_recorded_as_backoff_with_its_retry_time(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}}
    clients = {}
    mod.sync(clients, live, cfg, lambda m: None)
    procs[-1].rc = 255
    mod.sync(clients, live, cfg, lambda m: None)
    rec = _state(tmp_path)["a"]
    assert rec["state"] == "backoff" and rec["rc"] == 255
    assert rec["retry_at_epoch"] > __import__("time").time()


def test_a_backed_off_mirror_is_published_to_the_far_box_once_and_an_up_one_never(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}, "b": {"pid": 2, "messagingSocketPath": "e2"}}
    clients = {}
    mod.sync(clients, live, cfg, lambda m: None)
    assert mod.sent == []
    procs[0].rc = 255
    mod.sync(clients, live, cfg, lambda m: None)
    mod.sync(clients, live, cfg, lambda m: None)
    assert len(mod.sent) == 1
    verb, name, state, until, box = mod.sent[0].split()
    assert (verb, name, state) == ("mirror-stale", "a", "backoff") and int(until) > __import__("time").time()
    assert box


def test_a_host_refused_mirror_is_not_published(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}}
    clients = {}
    mod.sync(clients, live, cfg, lambda m: None)
    procs[0].rc = 3
    mod.sync(clients, live, cfg, lambda m: None)
    assert _state(tmp_path)["a"]["state"] == "host-refused" and mod.sent == []


def test_the_relay_accepts_only_a_well_formed_stale_mark():
    relay = _load_relay()
    ok = relay.parse_mode("mirror-stale a backoff 1900000000 box-1")
    assert ok == ("mirror-stale", ("a", "backoff", 1900000000, "box-1"))
    for bad in ("mirror-stale a up 1900000000 b", "mirror-stale ../x dead 1 b", "mirror-stale a dead soon b",
                "mirror-stale a dead 1"):
        assert relay.parse_mode(bad) is None


def _load_relay():
    s = importlib.util.spec_from_file_location(
        "restart_relay", Path(__file__).resolve().parents[1] / "unimatrix-relay.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_a_stale_mark_written_by_the_relay_lists_the_name_stale_in_the_router(tmp_path, monkeypatch):
    relay = _load_relay()
    monkeypatch.setattr(relay, "SOCKS", str(tmp_path))
    relay.mark_stale("a", "backoff", int(__import__("time").time()) + 100, "winbox")
    relay.mark_stale("old", "dead", 1, "winbox")
    s = importlib.util.spec_from_file_location(
        "restart_router", Path(__file__).resolve().parents[1] / "unimatrix-peer-router.py")
    router = importlib.util.module_from_spec(s)
    s.loader.exec_module(router)
    fake = SimpleNamespace(mirror_stale_dir=str(tmp_path))
    markers = router.Router.stale_mirror_markers(fake)
    assert list(markers) == ["a"] and markers["a"]["box"] == "winbox"


def test_an_alive_unchanged_client_is_never_respawned_and_a_new_session_pid_is(tmp_path, monkeypatch):
    mod, procs, clock, cfg = _mirror_all_rig(tmp_path, monkeypatch)
    live = {"a": {"pid": 1, "messagingSocketPath": "e"}}
    clients = {}
    mod.sync(clients, live, cfg, lambda m: None)
    for _ in range(5):
        clock[0] += 60
        mod.sync(clients, live, cfg, lambda m: None)
    assert len(procs) == 1
    live["a"] = {"pid": 2, "messagingSocketPath": "e"}
    mod.sync(clients, live, cfg, lambda m: None)
    assert len(procs) == 2 and clients["a"][0] == 2
