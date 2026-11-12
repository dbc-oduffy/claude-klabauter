"""Far relay process count is bounded by far boxes, not mirrored inboxes; dead faces stay unmirrored;
the forced command names the Homebrew interpreter on macOS.
"""
import importlib.util
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]
HOMEBREW = "/opt/homebrew/bin/python3"
DEAD_PID = 2_147_000_000  # far above any live pid on every host


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _session(sessions, pid, name, **extra):
    d = {"pid": pid, "name": name, "messagingSocketPath": f"/tmp/{name}.sock", **extra}
    (sessions / f"{pid}.json").write_text(json.dumps(d), encoding="utf-8")


def _rig(tmp_path, monkeypatch):
    """A fresh mirror-all module over a tmp sessions dir; every spawn is recorded, none is real."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    mod = _load("process_bound_mirror_all", "unimatrix-mirror-all.py")
    spawned = []

    def fake_spawn(cmd):
        p = SimpleNamespace(pid=9000 + len(spawned), rc=None)
        p.poll = lambda: None
        spawned.append(cmd)
        return p

    monkeypatch.setattr(mod.R, "sessions_dir", lambda: str(sessions))
    monkeypatch.setattr(mod.R, "pid_alive", lambda pid: pid != DEAD_PID)
    monkeypatch.setattr(mod.R, "spawn", fake_spawn)
    monkeypatch.setattr(mod.R, "tree_kill", lambda pid: None)
    monkeypatch.setattr(mod, "_start_publish", lambda cfg, cmds, say: None)
    # Any per-name or mux subscribe a client would make is a far relay process; none runs here.
    monkeypatch.setattr(mod, "_box_mux", lambda: pytest.fail("sync opened a box_mux stream itself"))
    return mod, sessions, spawned


def _far_relay_processes(spawned):
    """Far relay processes started: each spawned near client holds one subscribe, hence one far relay,
    for the far box named by its --remote."""
    return Counter(cmd[cmd.index("--remote") + 1] for cmd in spawned)


def _cfg(mod, tmp_path, remote):
    return mod.parse_args([str(tmp_path / f"{remote}.log"), "--remote", remote, "--key", "k"])


def test_six_mirrored_inboxes_to_one_far_box_start_one_far_relay_process(tmp_path, monkeypatch):
    mod, sessions, spawned = _rig(tmp_path, monkeypatch)
    for i in range(6):
        _session(sessions, 100 + i, f"sess-{i}")
    mod.sync({}, mod.live_sessions(set()), _cfg(mod, tmp_path, "farbox"), lambda m: None)
    counts = _far_relay_processes(spawned)
    assert counts == {"farbox": 1}, (
        f"far relay processes per far box with 6 mirrored inboxes: {dict(counts)}; expected 1 per far box")


def test_far_relay_process_count_does_not_grow_with_mirrored_inboxes(tmp_path, monkeypatch):
    mod, sessions, spawned = _rig(tmp_path, monkeypatch)
    cfg, clients = _cfg(mod, tmp_path, "farbox"), {}
    for i in range(2):
        _session(sessions, 100 + i, f"sess-{i}")
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    at_two = sum(_far_relay_processes(spawned).values())
    for i in range(2, 8):
        _session(sessions, 100 + i, f"sess-{i}")
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    at_eight = sum(_far_relay_processes(spawned).values())
    assert at_eight == at_two == 1, (
        f"far relay processes started: {at_two} at 2 mirrored inboxes, {at_eight} at 8; expected 1 at both")


def test_two_far_boxes_start_one_far_relay_process_each(tmp_path, monkeypatch):
    mod, sessions, spawned = _rig(tmp_path, monkeypatch)
    for i in range(6):
        _session(sessions, 100 + i, f"sess-{i}")
    for remote in ("farbox-a", "farbox-b"):
        mod.sync({}, mod.live_sessions(set()), _cfg(mod, tmp_path, remote), lambda m: None)
    counts = _far_relay_processes(spawned)
    assert counts == {"farbox-a": 1, "farbox-b": 1}, (
        f"far relay processes per far box with 6 mirrored inboxes and 2 far boxes: {dict(counts)}; "
        "expected 1 per far box")


class _Stop(Exception):
    pass


def test_dead_codex_face_is_not_mirrored_and_leaves_no_mirror_entry_after_the_boot_pass(tmp_path, monkeypatch):
    mod, sessions, spawned = _rig(tmp_path, monkeypatch)
    _session(sessions, 100, "live-session")
    _session(sessions, 101, "codex-dead-face", vendor="codex", target="thread-dead")
    peers = tmp_path / "settings" / "unimatrix" / "peers.json"
    peers.parent.mkdir(parents=True)
    peers.write_text(json.dumps({"peers": [
        {"name": "codex-dead-face", "vendor": "codex", "target": "thread-dead", "codexPid": DEAD_PID}]}),
        encoding="utf-8")
    monkeypatch.setattr(mod, "ensure_router", lambda say, wait=3.0: False)
    monkeypatch.setattr(mod.signal, "signal", lambda *a: None)

    def stop(_seconds):
        raise _Stop

    monkeypatch.setattr(mod, "time", SimpleNamespace(
        strftime=time.strftime, gmtime=time.gmtime, time=time.time, monotonic=time.monotonic, sleep=stop))
    with pytest.raises(_Stop):
        mod.main([str(tmp_path / "m.log"), "--remote", "farbox", "--key", "k"])
    assert not any("codex-dead-face" in part for cmd in spawned for part in cmd), (
        f"dead codex face got a client; spawned: {spawned}")
    state = json.loads((tmp_path / "unimatrix-mirror-state.json").read_text(encoding="utf-8"))
    assert "codex-dead-face" not in state, f"dead codex face kept a mirror entry: {state['codex-dead-face']}"
    assert "live-session" in state


def test_relay_line_on_darwin_names_homebrew_python_not_the_running_interpreter(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    relay = _load("process_bound_relay", "unimatrix-relay.py")
    real_isfile, real_access = os.path.isfile, os.access
    monkeypatch.setattr(os.path, "isfile", lambda p: True if str(p) == HOMEBREW else real_isfile(p))
    monkeypatch.setattr(os, "access", lambda p, mode, **kw: True if str(p) == HOMEBREW else real_access(p, mode, **kw))
    monkeypatch.setattr(sys, "platform", "darwin")
    line = relay.relay_line("AAAA", "peerbox")
    command = line.split('command="', 1)[1].split('"', 1)[0]
    assert command.startswith(HOMEBREW + " "), (
        f"forced command interpreter is {command.split()[0]!r}; expected {HOMEBREW!r} (base writes sys.executable)")
    assert "--peer peerbox" in command


def test_a_far_relay_answering_usage_falls_back_to_one_client_per_name(tmp_path, monkeypatch):
    mod, sessions, spawned = _rig(tmp_path, monkeypatch)
    for i in range(3):
        _session(sessions, 100 + i, f"sess-{i}")
    cfg, clients = _cfg(mod, tmp_path, "farbox"), {}
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    assert len(spawned) == 1 and spawned[0][2] == "mux"
    mux = clients["sess-0"][1]
    mux.poll = lambda: mod.MUX_UNSUPPORTED_RC
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    named = [cmd for cmd in spawned[1:] if cmd[2] != "mux"]
    assert mod._STATE["mux_off"] and [cmd[2] for cmd in named] == ["100", "101", "102"], spawned
    assert sorted(cmd[3] for cmd in named) == ["sess-0", "sess-1", "sess-2"]


def test_a_dead_mux_client_is_respawned_once_after_the_backoff_not_per_name(tmp_path, monkeypatch):
    mod, sessions, spawned = _rig(tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(mod, "_now", lambda: clock[0])
    for i in range(4):
        _session(sessions, 100 + i, f"sess-{i}")
    cfg, clients = _cfg(mod, tmp_path, "farbox"), {}
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    clients["sess-0"][1].poll = lambda: 255
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    assert len(spawned) == 1
    assert {v["state"] for v in json.loads((tmp_path / "unimatrix-mirror-state.json").read_text()).values()} == {"backoff"}
    clock[0] += mod.backoff_seconds(0) + 1
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    assert len(spawned) == 2 and all(c[1] is clients["sess-0"][1] for c in clients.values())


def test_the_mux_names_file_follows_live_sessions_and_keeps_an_absent_one_for_two_polls(tmp_path, monkeypatch):
    mod, sessions, spawned = _rig(tmp_path, monkeypatch)
    for i in range(2):
        _session(sessions, 100 + i, f"sess-{i}")
    cfg, clients = _cfg(mod, tmp_path, "farbox"), {}
    names = mod.mux_names_path(str(tmp_path), "farbox")
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    assert sorted(json.loads(open(names).read())) == ["sess-0", "sess-1"]
    (sessions / "101.json").unlink()
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    assert sorted(json.loads(open(names).read())) == ["sess-0", "sess-1"]
    mod.sync(clients, mod.live_sessions(set()), cfg, lambda m: None)
    assert sorted(json.loads(open(names).read())) == ["sess-0"] and len(spawned) == 1


def _inbox(tmp_path, monkeypatch):
    mod = _load("process_bound_inbox", "unimatrix-inbox.py")
    sessions = tmp_path / "ssessions"
    sessions.mkdir()
    monkeypatch.setattr(mod, "SESSIONS", str(sessions))
    mod.log = open(tmp_path / "inbox.log", "a", encoding="utf-8")
    return mod, sessions


def _near_session(sessions, pid, name, endpoint):
    (sessions / f"{pid}.json").write_text(json.dumps({"pid": pid, "name": name, "messagingSocketPath": endpoint}))
    (sessions / f"{pid}.abc.key").write_text("keybody\n")


def test_the_mux_feed_sends_add_and_drop_frames_as_the_names_file_changes(tmp_path, monkeypatch):
    import io
    mod, sessions = _inbox(tmp_path, monkeypatch)
    _near_session(sessions, 100, "a", "/tmp/a.sock")
    _near_session(sessions, 101, "b", "/tmp/b.sock")
    names, out = tmp_path / "names.json", io.BytesIO()
    out.flush = lambda: None
    feed = mod.MuxFeed(out, str(names))
    names.write_text(json.dumps({"a": {"pid": 100, "endpoint": "/tmp/a.sock"},
                                 "b": {"pid": 101, "endpoint": "/tmp/b.sock"}}))
    assert feed.reconcile() and feed.reconcile()
    names.write_text(json.dumps({"a": {"pid": 100, "endpoint": "/tmp/a.sock"}}))
    assert feed.reconcile()
    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [(f["op"], f["name"]) for f in frames] == [("add", "a"), ("add", "b"), ("drop", "b")]
    assert frames[0]["key"] == "keybody" and frames[0]["entry"]["name"] == "a"


def test_a_name_the_far_box_refuses_is_parked_not_redialed_every_poll(tmp_path, monkeypatch):
    import io
    mod, sessions = _inbox(tmp_path, monkeypatch)
    _near_session(sessions, 100, "a", "/tmp/a.sock")
    names, out, clock = tmp_path / "names.json", io.BytesIO(), [0.0]
    out.flush = lambda: None
    feed = mod.MuxFeed(out, str(names), clock=lambda: clock[0])
    names.write_text(json.dumps({"a": {"pid": 100, "endpoint": "/tmp/a.sock"}}))
    feed.reconcile()
    feed.refuse("a", "belongs to a live local session")
    feed.reconcile()
    assert len(out.getvalue().splitlines()) == 1
    clock[0] += mod.MUX_PARK_SECONDS + 1
    feed.reconcile()
    assert len(out.getvalue().splitlines()) == 2


def test_the_mux_client_exits_with_the_unsupported_code_when_the_far_relay_prints_usage(tmp_path, monkeypatch):
    import io
    mod, _ = _inbox(tmp_path, monkeypatch)
    p = SimpleNamespace(pid=1, stdin=io.BytesIO(), stdout=iter([]), wait=lambda: 1,
                        stderr=iter([b"usage: roster | specs | inbox <name>\n"]))
    p.stdin.flush = lambda: None
    chan = SimpleNamespace(subscribe=lambda command, data=b"": (command == "inbox-mux") and p)
    monkeypatch.setattr(mod, "_box_mux", lambda: SimpleNamespace(for_remote=lambda *a, **k: chan))
    monkeypatch.setattr(mod, "pick_remote", lambda lan, tunnel: lan)
    a = mod.parse_mux_args([str(tmp_path / "m.log"), "--names-file", str(tmp_path / "n.json"),
                            "--remote", "farbox", "--key", "k"])
    with pytest.raises(SystemExit) as e:
        mod._mux_run(a)
    assert e.value.code == mod.MUX_UNSUPPORTED_RC


def test_a_wrapped_far_frame_is_delivered_to_the_endpoint_of_its_name(tmp_path, monkeypatch):
    import io
    mod, sessions = _inbox(tmp_path, monkeypatch)
    _near_session(sessions, 100, "a", "/tmp/a.sock")
    names = tmp_path / "n.json"
    names.write_text(json.dumps({"a": {"pid": 100, "endpoint": "/tmp/a.sock"}}))
    wrapped = json.dumps({"name": "a", "frame": {"from": "uds:/far/x", "message": "hi"}}).encode() + b"\n"
    p = SimpleNamespace(pid=1, stdin=io.BytesIO(), wait=lambda: 0, stderr=iter([]), stdout=None)
    p.stdin.flush = lambda: None
    delivered = []

    def stdout():
        # the feed has run its first reconcile by the time the far relay answers
        deadline = time.monotonic() + 5
        while "a" not in feed_box[0].sent and time.monotonic() < deadline:
            time.sleep(0.01)
        yield wrapped

    p.stdout = stdout()
    chan = SimpleNamespace(subscribe=lambda command, data=b"": p)
    feed_box = []
    real_feed = mod.MuxFeed
    monkeypatch.setattr(mod, "MuxFeed", lambda *a, **k: feed_box.append(real_feed(*a, **k)) or feed_box[0])
    monkeypatch.setattr(mod, "_box_mux", lambda: SimpleNamespace(for_remote=lambda *a, **k: chan))
    monkeypatch.setattr(mod, "pick_remote", lambda lan, tunnel: lan)
    monkeypatch.setattr(mod, "write_local", lambda endpoint, data: delivered.append((endpoint, data)))
    a = mod.parse_mux_args([str(tmp_path / "m.log"), "--names-file", str(names), "--remote", "farbox", "--key", "k"])
    mod._mux_run(a)
    assert [e for e, _ in delivered] == ["/tmp/a.sock"] and b'"message": "hi"' in delivered[0][1]
