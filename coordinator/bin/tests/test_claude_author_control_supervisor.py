"""Tests for claude_author_control/supervisor.py with fake Relay/Transport (no PTY, no socket)."""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

_LIB = str(Path(__file__).resolve().parents[1] / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from claude_author_control import protocol as P  # noqa: E402
from claude_author_control import supervisor as S  # noqa: E402

PEER = P.Peer("unix", uid=1, pid=2)


class FakeRelay:
    pid = 4242

    def __init__(self):
        self.writes: list[bytes] = []
        self.up = True

    def write(self, data):
        self.writes.append(data)

    def resize(self, cols, rows):
        pass

    def alive(self):
        return self.up

    def wait(self):
        return 7


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(tmp_path, **kw):
    clock = Clock()
    sup = S.Supervisor(str(tmp_path), clock=clock, wallclock=lambda: 1000.0 + clock.t,
                       idle_ms=2000, settle_ms=300, **kw)
    sup.relay = FakeRelay()
    return sup, clock


def req(op, args=None, id=1, obo=None):
    return P.Request(id=id, op=op, args=args or {}, on_behalf_of=obo)


def call(sup, r):
    out = []
    sup.submit(r, PEER, out.append)
    return out


def write_turn(tmp_path, active, ts, sid="sid-1"):
    (Path(tmp_path) / P.TURN_FILE).write_text(
        json.dumps(P.turn_record(sid, active, "UserPromptSubmit", ts)))


WRITING = [("prompt", {"text": "hi"}), ("compact", {}), ("model", {"name": "opus"}),
           ("effort", {"level": "high"}), ("interrupt", {})]


@pytest.mark.parametrize("op,args", WRITING)
def test_busy_user_within_idle_window(tmp_path, op, args):
    sup, clock = make(tmp_path)
    sup.on_input(b"\r")
    clock.t += 1.0
    (resp,) = call(sup, req(op, args))
    assert resp["error"]["code"] == "busy_user"
    assert sup.relay.writes == []
    clock.t += 1.5
    (resp,) = call(sup, req(op, args))
    assert resp["ok"] is True


@pytest.mark.parametrize("op,args", WRITING)
def test_busy_user_while_draft_dirty(tmp_path, op, args):
    sup, clock = make(tmp_path)
    sup.on_input(b"abc")
    clock.t += 60
    assert sup.draft_dirty
    (resp,) = call(sup, req(op, args))
    assert resp["error"]["code"] == "busy_user"
    assert sup.relay.writes == []


@pytest.mark.parametrize("clear", [b"\r", b"\x1b", b"\x03", b"\x15"])
def test_draft_clears(tmp_path, clear):
    sup, _ = make(tmp_path)
    sup.on_input(b"abc")
    sup.on_input(clear)
    assert not sup.draft_dirty


def test_arrow_key_does_not_dirty_draft(tmp_path):
    sup, _ = make(tmp_path)
    sup.on_input(b"\x1b[A")
    assert not sup.draft_dirty


def test_status_takes_no_write_and_ignores_gate(tmp_path):
    sup, clock = make(tmp_path)
    sup.on_input(b"abc")
    (resp,) = call(sup, req("status", obo="PM"))
    assert resp["ok"] and "state" not in resp
    assert set(resp["status"]) == set(P.STATUS_FIELDS)
    assert resp["status"]["child_pid"] == 4242 and resp["status"]["draft_dirty"] is True
    assert resp["identity"]["relayed"] is True
    assert sup.relay.writes == []


def test_pty_dead(tmp_path):
    sup, _ = make(tmp_path)
    sup.relay.up = False
    (resp,) = call(sup, req("prompt", {"text": "x"}))
    assert resp["error"]["code"] == "pty_dead"


def test_queued_vs_injected_follows_turn_json(tmp_path):
    sup, _ = make(tmp_path)
    (r,) = call(sup, req("prompt", {"text": "a"}))
    assert r["state"] == "injected"
    write_turn(tmp_path, True, 5.0)
    (r,) = call(sup, req("prompt", {"text": "b"}))
    assert r["state"] == "queued"
    (r,) = call(sup, req("compact"))
    assert r["state"] == "queued"
    (r,) = call(sup, req("model", {"name": "opus"}))
    assert r["state"] == "injected"
    write_turn(tmp_path, False, 6.0)
    (r,) = call(sup, req("prompt", {"text": "c"}))
    assert r["state"] == "injected"


def test_relayed_marker_on_prompt_not_on_model(tmp_path):
    sup, _ = make(tmp_path)
    call(sup, req("prompt", {"text": "hello"}, obo="PM"))
    call(sup, req("model", {"name": "opus"}, obo="PM"))
    assert sup.relay.writes[0].startswith(
        b"[relayed via control-socket on behalf of PM] hello")
    assert sup.relay.writes[1] == b"/model opus\r"


def test_interrupt_sets_supervisor_turn_state_until_hook_newer(tmp_path):
    sup, _ = make(tmp_path)
    write_turn(tmp_path, True, 5.0)
    (r,) = call(sup, req("interrupt"))
    assert r["state"] == "injected" and sup.relay.writes == [P.ESC]
    (st,) = call(sup, req("status"))
    assert st["status"]["turn_active"] is False
    assert st["status"]["turn_source"] == "supervisor"
    write_turn(tmp_path, True, 99999.0)
    (st,) = call(sup, req("status"))
    assert st["status"]["turn_active"] is True and st["status"]["turn_source"] == "hook"


def test_force_esc_then_deferred_delivery(tmp_path):
    sup, clock = make(tmp_path)
    write_turn(tmp_path, True, 5.0)
    out = call(sup, req("prompt", {"text": "go", "force": True}))
    assert out == []  # no reply yet, nothing blocked
    assert sup.relay.writes == [P.ESC]
    assert not sup.write_lock._is_owned()
    sup.service()
    assert out == []  # settle not elapsed
    clock.t += 0.3
    sup.service()
    assert out[0]["ok"] and out[0]["state"] == "injected"
    assert sup.relay.writes[1].endswith(b"go\r")
    assert sup.turn_state()[1] == "supervisor"


def test_loop_keeps_relaying_during_settle(tmp_path):
    sup, clock = make(tmp_path)
    call(sup, req("prompt", {"text": "go", "force": True}))
    got = []

    def other():
        sup.on_output(P.PASTE_MODE_ON)
        got.append(sup.paste_mode)

    t = threading.Thread(target=other)
    t.start()
    t.join(2)
    assert got == [True]
    assert 0 < sup.next_timeout() <= 0.3


def test_keystroke_in_settle_aborts_with_busy_user(tmp_path):
    sup, clock = make(tmp_path)
    out = call(sup, req("prompt", {"text": "go", "force": True}))
    clock.t += 0.1
    sup.on_input(b"x")
    clock.t += 0.2
    sup.service()
    assert out[0]["error"]["code"] == "busy_user"
    assert "Esc already sent" in out[0]["error"]["msg"]
    assert sup.relay.writes == [P.ESC]


def test_force_bad_args_sends_no_esc(tmp_path):
    sup, _ = make(tmp_path)
    (r,) = call(sup, req("prompt", {"text": "a\nb", "force": True}))
    assert r["error"]["code"] == "bad_args"
    assert sup.relay.writes == []


def test_force_blocking_handler_returns_after_timer(tmp_path):
    sup = S.Supervisor(str(tmp_path), idle_ms=0, settle_ms=20)
    sup.relay = FakeRelay()
    stop = threading.Event()

    def timer():
        while not stop.is_set():
            sup.deadlines.service()
            sup.deadlines.wake.wait(0.01)
            sup.deadlines.wake.clear()

    th = threading.Thread(target=timer, daemon=True)
    th.start()
    try:
        resp = sup(req("prompt", {"text": "go", "force": True}), PEER)
    finally:
        stop.set()
    assert resp["ok"] and resp["state"] == "injected"


def test_paste_mode_tracking_across_chunks(tmp_path):
    sup, _ = make(tmp_path)
    sup.on_output(b"abc\x1b[?20")
    sup.on_output(b"04hxyz")
    assert sup.paste_mode is True
    sup.on_output(b"\x1b[?2004l")
    assert sup.paste_mode is False


def test_multiline_prompt_uses_bracketed_paste(tmp_path):
    sup, _ = make(tmp_path)
    sup.on_output(P.PASTE_MODE_ON)
    call(sup, req("prompt", {"text": "a\nb"}))
    assert sup.relay.writes[0].startswith(P.PASTE_START)


# ------------------------------------------------------------ env / argv

def test_env_strip():
    env = S.build_child_env({"CLAUDE_CODE_CHILD_SESSION": "1", "PATH": "/x", "A": "b"})
    assert env == {"PATH": "/x", "A": "b"}


def test_argv_mints_session_id_and_settings():
    argv, minted = S.build_child_argv(["claude", "--plugin-dir", "/p"], "/inst", "/h/turn_hook.py")
    assert argv[:3] == ["claude", "--plugin-dir", "/p"]
    assert argv[argv.index("--session-id") + 1] == minted
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert set(settings["hooks"]) == set(P.TURN_EVENTS)
    hook = settings["hooks"]["Stop"][0]["hooks"][0]
    assert hook == {"type": "command", "command": sys.executable,
                    "args": ["/h/turn_hook.py", "/inst"]}


@pytest.mark.parametrize("extra", [["--resume", "abc"], ["--continue"], ["-c"],
                                   ["--session-id", "u"], ["--session-id=u"], ["-r", "x"]])
def test_argv_skips_session_id_when_caller_chose(extra):
    argv, minted = S.build_child_argv(["claude", *extra], "/inst", "/h.py")
    assert minted is None
    assert argv.count("--session-id") == (1 if "--session-id" in extra else 0)
    assert "--settings" in argv


# ------------------------------------------------------------ discovery

def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_discovery_modes_fields_refresh_and_removal(tmp_path):
    old = os.umask(0o000)
    try:
        d = S.Discovery(str(tmp_path / "base"), "abcdef012345")
        d.create_dirs()
        rec = P.discovery_record(instance="abcdef012345", session_id=None, transport="unix",
                                 address="/x/sock", supervisor_pid=1, child_pid=2, cwd="/c",
                                 started_at="2026-01-01T00:00:00Z")
        d.write(rec)
    finally:
        os.umask(old)
    assert mode(d.base) == mode(d.sessions_dir) == mode(d.instance_dir) == 0o700
    assert mode(d.path) == 0o600
    assert set(json.loads(d.path.read_text())) == set(P.DISCOVERY_FIELDS)
    d.refresh("new-sid", 2)
    assert json.loads(d.path.read_text())["session_id"] == "new-sid"
    assert mode(d.path) == 0o600
    assert [p.name for p in d.sessions_dir.iterdir()] == ["abcdef012345.json"]
    d.cleanup()
    assert not d.path.exists() and not d.instance_dir.exists()


# ------------------------------------------------------------ run()

class FakeTransport:
    def __init__(self, address):
        self.address = address
        self.closed = False
        self.handler = None

    def serve(self, handler):
        self.handler = handler

    def close(self):
        self.closed = True


def test_run_propagates_exit_code_and_cleans_up(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")

    def spawn(argv, env, observers):
        seen.update(argv=argv, env=env, observers=observers)
        r = FakeRelay()
        seen["relay"] = r
        base = Path(tmp_path)
        seen["files"] = [p.name for p in (base / "sessions").iterdir()]
        return r

    def make_transport(instance, instance_dir):
        seen["instance_dir"] = instance_dir
        return FakeTransport("/x/sock")

    monkeypatch.setattr(S, "_select_legs", lambda: (spawn, make_transport))
    code = S.run(["claude", "--plugin-dir", "/p"], plugin_dir="/p", control_base=str(tmp_path))
    assert code == 7
    assert "CLAUDE_CODE_CHILD_SESSION" not in seen["env"]
    assert isinstance(seen["observers"], P.Observers)
    assert not any((tmp_path / "sessions").iterdir())
    assert not Path(seen["instance_dir"]).exists()


def test_run_start_failure_exits_nonzero_and_cleans(tmp_path, monkeypatch, capsys):
    def spawn(argv, env, observers):
        raise OSError("no pty")

    monkeypatch.setattr(S, "_select_legs", lambda: (spawn, lambda i, d: FakeTransport("a")))
    code = S.run(["claude"], plugin_dir="/p", control_base=str(tmp_path))
    assert code == 1
    assert "failed to start" in capsys.readouterr().err
    assert not any((tmp_path / "sessions").iterdir())


def test_import_loads_by_path_without_os_legs():
    # Importing the supervisor must not pull in an OS leg; only _select_legs does. Judged on a
    # fresh import, since another test's run() legitimately leaves legs loaded in the package.
    import importlib

    def ours(key):
        return key == "claude_author_control" or key.startswith("claude_author_control.")

    saved = {k: v for k, v in sys.modules.items() if ours(k)}
    for key in saved:
        del sys.modules[key]
    try:
        importlib.import_module("claude_author_control.supervisor")
        legs = [k for k in sys.modules if ours(k) and k.rsplit(".", 1)[-1].startswith(("relay_", "transport_"))]
        assert legs == []
    finally:
        for key in [k for k in sys.modules if ours(k)]:
            del sys.modules[key]
        sys.modules.update(saved)
