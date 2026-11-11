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
    assert updated == expected
    assert bridges[sock] is bridge
    monkeypatch.setattr(roster, "write_json_atomic", lambda *a: pytest.fail("rewrote unchanged mirror"))
    roster.sync(bridges, {sock: row}, cfg, lambda *a: None)


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
    cfg = mod.parse_args([str(tmp_path / "m.log"), "--remote", "farbox", "--key", "k"])
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
