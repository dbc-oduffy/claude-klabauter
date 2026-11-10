"""Behavior pins for the agy PreToolUse hook and the leg launcher it prefixes."""
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]
PRETOOL = BIN / "unimatrix-agy-hook-pretool.py"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pre = load("unimatrix-agy-hook-pretool")
launch = load("unimatrix-agy-hook-launch")
CONV = "0cf351a3-6538-4a9b-814c-52b1b75a596c"
posix_rewrite = pytest.mark.skipif(os.name == "nt", reason="the leg-start rewrite is POSIX only; nt passes through")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def ls_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    yield f"127.0.0.1:{srv.getsockname()[1]}"
    srv.close()


def run_payload(*, conv=CONV, name="run_command", cmd="ls -la"):
    return {"conversationId": conv, "toolCall": {"name": name, "args": {"CommandLine": cmd}}}


def write_leg(home, *, pid, agy_pid, ls, conv=CONV):
    d = home / "unimatrix" / "agy-legs"
    d.mkdir(parents=True)
    (d / f"{conv}.json").write_text(json.dumps(
        {"pid": pid, "conversation": conv, "endpoint": "x.sock", "ls_address": ls, "agy_pid": agy_pid}))


@posix_rewrite
def test_no_leg_rewrites_with_detached_launch_and_asks(home):
    out = pre.decide(run_payload(), 4242)
    assert out["decision"] == "ask"
    new = out["overwrite"]["CommandLine"]
    assert new.endswith("ls -la")
    assert str(pre.LAUNCH) in new and CONV in new and "4242" in new
    assert "nohup" in new and "/Users/" not in new.replace(str(pre.LAUNCH), "").replace(sys.executable, "")


def test_current_leg_leaves_command_alone(home, ls_server):
    write_leg(home, pid=os.getpid(), agy_pid=4242, ls=ls_server)
    assert pre.decide(run_payload(), 4242) == {"decision": "ask"}


@posix_rewrite
def test_stale_leg_by_agy_pid_is_rewritten(home, ls_server):
    write_leg(home, pid=os.getpid(), agy_pid=1111, ls=ls_server)
    assert "overwrite" in pre.decide(run_payload(), 4242)


@posix_rewrite
def test_stale_leg_with_unreachable_ls_is_rewritten(home):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    addr = f"127.0.0.1:{s.getsockname()[1]}"
    s.close()
    write_leg(home, pid=os.getpid(), agy_pid=4242, ls=addr)
    assert "overwrite" in pre.decide(run_payload(), 4242)


@posix_rewrite
def test_dead_leg_pid_is_rewritten(home, ls_server, monkeypatch):
    write_leg(home, pid=os.getpid(), agy_pid=4242, ls=ls_server)
    monkeypatch.setattr(pre, "pid_alive", lambda pid: False)
    assert "overwrite" in pre.decide(run_payload(), 4242)


@pytest.mark.parametrize("payload", [
    run_payload(name="read_file"),
    run_payload(cmd=""),
    run_payload(conv="../escape"),
    {"conversationId": CONV},
    {"toolCall": None},
    [1, 2],
    "text",
])
def test_non_rewrite_inputs_pass_without_overwrite(home, payload):
    assert pre.decide(payload, 4242) == {"decision": "ask"}


def stub_guard(monkeypatch, answer):
    seen = []
    monkeypatch.setattr(pre, "run_guard", lambda raw, root: seen.append((raw, root)) or dict(answer))
    return seen


def test_guard_deny_returned_unchanged_without_overwrite(home, monkeypatch):
    deny = {"decision": "deny", "reason": "blocked"}
    seen = stub_guard(monkeypatch, deny)
    assert pre.decide(run_payload(), 4242, "/engine") == deny
    assert seen[0][1] == "/engine" and CONV in seen[0][0]


@posix_rewrite
def test_guard_ask_with_reason_and_stale_leg_is_one_ask_with_reason_and_overwrite(home, monkeypatch):
    stub_guard(monkeypatch, {"decision": "ask", "reason": "guard says look"})
    out = pre.decide(run_payload(), 4242)
    assert out["decision"] == "ask" and "guard says look" in out["reason"]
    assert "unimatrix" in out["reason"] and out["overwrite"]["CommandLine"].endswith("ls -la")


def test_guard_ask_with_current_leg_keeps_guard_reason_without_overwrite(home, ls_server, monkeypatch):
    write_leg(home, pid=os.getpid(), agy_pid=4242, ls=ls_server)
    stub_guard(monkeypatch, {"decision": "ask", "reason": "guard says look"})
    assert pre.decide(run_payload(), 4242) == {"decision": "ask", "reason": "guard says look"}


def test_guard_down_current_leg_is_bare_ask(home, ls_server):
    write_leg(home, pid=os.getpid(), agy_pid=4242, ls=ls_server)
    assert pre.decide(run_payload(), 4242, str(home / "no-engine")) == {"decision": "ask"}


@posix_rewrite
def test_guard_down_stale_leg_asks_with_overwrite(home):
    out = pre.decide(run_payload(), 4242, str(home / "no-engine"))
    assert out["decision"] == "ask" and "overwrite" in out


def test_guard_runs_for_non_run_command_tools(home, monkeypatch):
    deny = {"decision": "deny", "reason": "no"}
    stub_guard(monkeypatch, deny)
    assert pre.decide(run_payload(name="write_file"), 4242) == deny


def test_unloadable_guard_degrades_to_ask(home, monkeypatch):
    monkeypatch.setattr(pre, "load_sibling", lambda name: 1 / 0)
    assert pre.run_guard("{}", None) == {"decision": "ask"}


def test_no_path_emits_allow_or_force_ask(home, monkeypatch):
    for guard_answer in ({"decision": "ask"}, {"decision": "ask", "reason": "r"}):
        stub_guard(monkeypatch, guard_answer)
        for payload in (run_payload(), run_payload(name="read_file"), {}, []):
            text = json.dumps(pre.decide(payload, 4242))
            assert '"allow"' not in text and "force_ask" not in text


SEND = str(pre.SEND_SCRIPT)
PY = sys.executable


def test_exact_peer_reply_is_allowed_without_rewrite(home, monkeypatch):
    stub_guard(monkeypatch, {"decision": "ask"})
    for cmd in (f"{PY} {SEND} --to coordinator-content-repo-ea 'Reviewed: two issues (a), (b)'", f"{PY} {SEND} --list"):
        out = pre.decide(run_payload(cmd=cmd), 4242)
        assert out["decision"] == "allow" and "overwrite" not in out


@pytest.mark.parametrize("tmpl", [
    "{PY} {SEND}x --to a hi",
    "{PY} /elsewhere/unimatrix-send.py --to a hi",
    "other-python {SEND} --to a hi",
    "{SEND} --to a hi",
    "echo hi; {PY} {SEND} --to a hi",
    "{PY} {SEND} --to a hi; rm -rf x",
    "{PY} {SEND} --to a hi && id",
    "{PY} {SEND} --to a hi | cat",
    "{PY} {SEND} --to a hi > /tmp/f",
    "{PY} {SEND} --to a $(id)",
    "{PY} {SEND} --to a `id`",
    "FOO=1 {PY} {SEND} --to a hi",
    "x {PY} {SEND} --to a hi",
    "{PY}",
    "{PY} {SEND} --to a 'unterminated",
])
def test_non_exact_send_commands_keep_ask(home, monkeypatch, tmpl):
    stub_guard(monkeypatch, {"decision": "ask"})
    out = pre.decide(run_payload(cmd=tmpl.format(PY=PY, SEND=SEND)), 4242)
    assert out["decision"] == "ask"


def test_peer_reply_allow_needs_run_command_and_yields_to_guard_deny(home, monkeypatch):
    cmd = f"{PY} {SEND} --to a hi"
    stub_guard(monkeypatch, {"decision": "ask"})
    assert pre.decide(run_payload(name="write_file", cmd=cmd), 4242)["decision"] == "ask"
    deny = {"decision": "deny", "reason": "blocked"}
    stub_guard(monkeypatch, deny)
    assert pre.decide(run_payload(cmd=cmd), 4242) == deny


def test_engine_root_argument_reaches_the_guard(home, monkeypatch, capsys):
    seen = stub_guard(monkeypatch, {"decision": "ask"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(run_payload(name="read_file"))))
    assert pre.main(["--engine-root", "/eng"]) == 0
    assert seen[0][1] == "/eng" and json.loads(capsys.readouterr().out)["decision"] == "ask"


@posix_rewrite
def test_entry_point_with_missing_engine_root_still_asks(home):
    r = subprocess.run([sys.executable, str(PRETOOL), "--engine-root", str(home / "none")],
                       input=json.dumps(run_payload()), capture_output=True, text=True,
                       env=dict(os.environ, COORDINATOR_SETTINGS_HOME=str(home)))
    assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "ask" and "overwrite" in json.loads(r.stdout)


def hook(stdin, home):
    return subprocess.run([sys.executable, str(PRETOOL)], input=stdin, capture_output=True, text=True,
                          env=dict(os.environ, COORDINATOR_SETTINGS_HOME=str(home)))


@pytest.mark.parametrize("stdin", ["", "not json", "{}", "[]", json.dumps(run_payload(name="read_file"))])
def test_entry_point_always_emits_one_decision_and_exits_zero(home, stdin):
    r = hook(stdin, home)
    assert r.returncode == 0
    assert json.loads(r.stdout)["decision"] == "ask"


@posix_rewrite
def test_entry_point_rewrites_a_real_run_command(home):
    r = hook(json.dumps(run_payload()), home)
    assert r.returncode == 0
    assert "overwrite" in json.loads(r.stdout)


def test_internal_exception_still_emits_decision(home, monkeypatch, capsys):
    monkeypatch.setattr(pre, "decide", lambda *a: 1 / 0)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(run_payload())))
    assert pre.main() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "ask"


def test_windows_adds_no_cmd_prefix_and_passes_command_through(home, monkeypatch):
    monkeypatch.setattr(pre.os, "name", "nt")
    monkeypatch.setattr(pre, "pid_alive", lambda pid: False)
    monkeypatch.setattr(pre.subprocess, "Popen", lambda cmd, **kw: None)
    assert pre.launch_prefix(CONV, 7) is None
    stub_guard(monkeypatch, {"decision": "ask"})
    assert pre.decide(run_payload(cmd="Get-ChildItem | Select-Object -First 1"), 4242) == {"decision": "ask"}


def test_windows_spawns_launcher_detached_when_leg_not_current(home, monkeypatch):
    monkeypatch.setattr(pre.os, "name", "nt")
    monkeypatch.setattr(pre, "pid_alive", lambda pid: False)
    calls = []
    monkeypatch.setattr(pre.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    stub_guard(monkeypatch, {"decision": "ask"})
    payload = run_payload(cmd="Get-ChildItem | Select-Object -First 1")
    assert pre.decide(payload, 4242) == {"decision": "ask"}
    assert payload["toolCall"]["args"]["CommandLine"] == "Get-ChildItem | Select-Object -First 1"
    (cmd, kw), = calls
    assert cmd == [sys.executable, str(pre.LAUNCH), "--conversation", CONV, "--agy-pid", "4242"]
    assert kw["stdout"] is subprocess.DEVNULL and kw["stdin"] is subprocess.DEVNULL
    want = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert kw["creationflags"] & want == want
    assert not kw["creationflags"] & 0x8  # DETACHED_PROCESS: every descendant opens a visible console


def test_windows_second_run_command_within_debounce_window_spawns_nothing(home, monkeypatch):
    monkeypatch.setattr(pre.os, "name", "nt")
    monkeypatch.setattr(pre, "pid_alive", lambda pid: False)
    calls = []
    monkeypatch.setattr(pre.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    stub_guard(monkeypatch, {"decision": "ask"})
    pre.decide(run_payload(), 4242)
    pre.decide(run_payload(cmd="dir"), 4242)
    assert len(calls) == 1
    stamp = home / "unimatrix" / "agy-legs" / f"{CONV}.spawned"
    old = stamp.stat().st_mtime - pre.SPAWN_DEBOUNCE_S - 1
    os.utime(stamp, (old, old))
    pre.decide(run_payload(cmd="dir"), 4242)
    assert len(calls) == 2


def test_windows_does_not_spawn_when_leg_current(home, ls_server, monkeypatch):
    monkeypatch.setattr(pre.os, "name", "nt")
    write_leg(home, pid=os.getpid(), agy_pid=4242, ls=ls_server)
    monkeypatch.setattr(pre, "pid_alive", lambda pid: True)
    calls = []
    monkeypatch.setattr(pre.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    stub_guard(monkeypatch, {"decision": "ask"})
    assert pre.decide(run_payload(), 4242) == {"decision": "ask"}
    assert calls == []


def test_posix_prefix_still_rewrites(monkeypatch):
    monkeypatch.setattr(pre.os, "name", "posix")
    start, sep = pre.launch_prefix(CONV, 7)
    assert start.startswith("(nohup ") and sep == "; "


@pytest.mark.parametrize("tmpl,expected", [
    ('"{PY}" "{SEND}" --to a "Reviewed: two issues (a), (b); 100% done"', False),
    ('"{PY}" "{SEND}" --to a "Reviewed: two issues (a), (b); done"', True),
    ("{PY} {SEND} --to a 'x (y) ; z'", True),
    ("{PY} {SEND} --to a hi; id", False),
    ("{PY} {SEND} --to a (id)", False),
    ("{PY} {SEND} --to a hi | cat", False),
    ("{PY} {SEND} --to a 'unterminated", False),
    ("{PY} /elsewhere/unimatrix-send.py --to a hi", False),
    ("{PY} {SEND}", True),
])
def test_windows_peer_reply_is_quote_aware(monkeypatch, tmpl, expected):
    monkeypatch.setattr(pre.os, "name", "nt")
    cmd = tmpl.format(PY=PY, SEND=SEND)
    assert pre.is_peer_reply(run_payload(cmd=cmd)) is expected


def test_scripts_hardcode_no_home_path():
    for name in ("unimatrix-agy-hook-pretool.py", "unimatrix-agy-hook-launch.py"):
        assert "/Users/" not in (BIN / name).read_text()


# --- launcher -------------------------------------------------------------------------------

class FakeProc:
    pid = 31337


@pytest.fixture
def agy_env(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "localhost:5555")
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "tok")


def record_path(home):
    return home / "unimatrix" / "agy-legs" / f"{CONV}.json"


def test_launcher_starts_leg_detached_and_writes_record(home, agy_env, monkeypatch):
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc())
    assert launch.main(["--conversation", CONV, "--agy-pid", "99"]) == 0
    cmd, kw = calls[0]
    assert cmd[1] == str(BIN / "unimatrix-agy-leg.py")
    if os.name == "nt":
        assert kw["creationflags"] & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        assert kw["start_new_session"] is True
    assert kw["env"]["ANTIGRAVITY_CONVERSATION_ID"] == CONV
    rec = json.loads(record_path(home).read_text())
    assert (rec["pid"], rec["agy_pid"], rec["ls_address"], rec["conversation"]) == (31337, 99, "localhost:5555", CONV)
    assert rec["endpoint"]
    assert "tok" not in record_path(home).read_text()


def test_launcher_reads_json_stdin_when_no_args(home, agy_env, monkeypatch):
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"conversationId": CONV, "agyPid": 5})))
    launch.main([])
    assert json.loads(record_path(home).read_text())["agy_pid"] == 5


def test_launcher_windows_detaches_by_creationflags(monkeypatch):
    monkeypatch.setattr(launch.os, "name", "nt")
    monkeypatch.setattr(launch.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(launch.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(launch.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    kw = launch.detach_kwargs()
    assert kw == {"creationflags": 0x200 | 0x08000000}


def test_launcher_without_token_starts_pending_leg_with_discovered_ls(home, monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    monkeypatch.setenv("ANTIGRAVITY_CSRF_TOKEN", "")
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc())
    monkeypatch.setattr(launch, "discover_ls_address", lambda pid: f"127.0.0.1:{pid}")
    assert launch.main(["--conversation", CONV, "--agy-pid", "4444"]) == 0
    rec = json.loads(record_path(home).read_text())
    assert (rec["mode"], rec["ls_address"], rec["pid"]) == ("pending", "127.0.0.1:4444", 31337)
    assert "ANTIGRAVITY_CSRF_TOKEN" not in calls[0][1]["env"]


def test_launcher_with_token_and_ls_records_live_mode(home, agy_env, monkeypatch):
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    launch.main(["--conversation", CONV, "--agy-pid", "1"])
    assert json.loads(record_path(home).read_text())["mode"] == "live"


def test_pending_start_keeps_a_pending_leg_of_the_same_agy(home, monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    monkeypatch.setattr(launch, "discover_ls_address", lambda pid: None)
    record_path(home).parent.mkdir(parents=True)
    record_path(home).write_text(json.dumps(
        {"pid": os.getpid(), "conversation": CONV, "endpoint": "e", "ls_address": None, "agy_pid": 9, "mode": "pending"}))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("restarted a pending leg"))
    launch.main(["--conversation", CONV, "--agy-pid", "9"])


def test_live_start_replaces_pending_leg_with_the_same_ls(home, agy_env, monkeypatch):
    record_path(home).parent.mkdir(parents=True)
    record_path(home).write_text(json.dumps(
        {"pid": 777, "conversation": CONV, "endpoint": "e", "ls_address": "localhost:5555", "agy_pid": 1,
         "mode": "pending"}))
    killed = []
    monkeypatch.setattr(launch.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    roster = launch.load_sibling("unimatrix-roster")
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    monkeypatch.setattr(launch, "load_sibling", lambda name: roster)
    launch.main(["--conversation", CONV, "--agy-pid", "1"])
    assert killed == [777]
    rec = json.loads(record_path(home).read_text())
    assert (rec["pid"], rec["mode"]) == (31337, "live")


def test_pending_start_never_downgrades_a_live_leg_of_the_same_agy(home, monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.setattr(launch, "discover_ls_address", lambda pid: None)
    record_path(home).parent.mkdir(parents=True)
    record_path(home).write_text(json.dumps(
        {"pid": os.getpid(), "conversation": CONV, "endpoint": "e", "ls_address": "localhost:5555", "agy_pid": 9}))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("downgraded a live leg"))
    launch.main(["--conversation", CONV, "--agy-pid", "9"])
    assert "mode" not in json.loads(record_path(home).read_text())


def _lsof(monkeypatch, stdout=None, exc=None):
    def run(cmd, **kw):
        assert cmd[0] == "lsof" and "-p" in cmd
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    monkeypatch.setattr(launch.subprocess, "run", run)


def test_discover_ls_address_parses_loopback_listeners(monkeypatch):
    if os.name == "nt":
        monkeypatch.setattr(launch, "nt_loopback_listener_ports", lambda pid: [50321, 50100])
    else:
        _lsof(monkeypatch, "p42\nf10\nn127.0.0.1:50321\nf11\nn127.0.0.1:50100\nf12\nn*:8080\nf13\nn[::1]:9\n")
    assert launch.discover_ls_address(42) == "127.0.0.1:50100"


def test_discover_ls_address_is_none_without_lsof_or_listeners(monkeypatch):
    _lsof(monkeypatch, exc=FileNotFoundError("lsof"))
    assert launch.discover_ls_address(42) is None
    _lsof(monkeypatch, "p42\nn*:8080\n")
    assert launch.discover_ls_address(42) is None
    _lsof(monkeypatch, "")
    assert launch.discover_ls_address(None) is None


def test_discover_ls_address_is_none_on_windows(monkeypatch):
    monkeypatch.setattr(launch.os, "name", "nt")
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: pytest.fail("ran lsof on Windows"))
    assert launch.discover_ls_address(42) is None


def test_launcher_rejects_unsafe_conversation_id(home, agy_env, monkeypatch):
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("started a leg"))
    launch.main(["--conversation", "../evil"])
    assert not (home / "unimatrix" / "evil.json").exists()


def test_launcher_current_leg_only_restamps_agy_pid(home, agy_env, monkeypatch):
    d = record_path(home).parent
    d.mkdir(parents=True)
    record_path(home).write_text(json.dumps(
        {"pid": os.getpid(), "conversation": CONV, "endpoint": "e", "ls_address": "localhost:5555", "agy_pid": 1}))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: pytest.fail("restarted a current leg"))
    launch.main(["--conversation", CONV, "--agy-pid", "2"])
    assert json.loads(record_path(home).read_text())["agy_pid"] == 2


def test_launcher_stale_leg_is_terminated_and_replaced(home, agy_env, monkeypatch):
    d = record_path(home).parent
    d.mkdir(parents=True)
    record_path(home).write_text(json.dumps(
        {"pid": 777, "conversation": CONV, "endpoint": "e", "ls_address": "localhost:1111", "agy_pid": 1}))
    killed = []
    monkeypatch.setattr(launch.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    roster = launch.load_sibling("unimatrix-roster")
    monkeypatch.setattr(roster, "pid_alive", lambda pid: True)
    monkeypatch.setattr(launch, "load_sibling", lambda name: roster)
    launch.main(["--conversation", CONV, "--agy-pid", "3"])
    assert killed and killed[0][0] == 777
    rec = json.loads(record_path(home).read_text())
    assert (rec["pid"], rec["ls_address"], rec["agy_pid"]) == (31337, "localhost:5555", 3)


def test_launcher_swallows_internal_errors(home, agy_env, monkeypatch):
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: 1 / 0)
    assert launch.main(["--conversation", CONV]) == 0


# --- SessionStart hook ----------------------------------------------------------------------

sess = load("unimatrix-agy-hook-sessionstart")


def test_sessionstart_spawns_leg_with_conversation_id_and_notes_visibly(home, agy_env, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)) or FakeProc())
    out = sess.reply(sess.register({"conversationId": CONV}, 77))
    assert calls and calls[0][1]["env"]["ANTIGRAVITY_CONVERSATION_ID"] == CONV
    rec = json.loads(record_path(home).read_text())
    assert (rec["conversation"], rec["agy_pid"], rec["pid"]) == (CONV, 77, 31337)
    msg = out["injectSteps"][0]["systemMessage"]
    assert msg == {"systemMessage": sess.NOTE}  # no metadata: agy panics on it at SessionStart


def test_sessionstart_with_only_conversation_id_registers_a_pending_leg(home, monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_CSRF_TOKEN", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_LS_ADDRESS", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: FakeProc())
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout="p1\nn127.0.0.1:6001\n", stderr=""))
    listener = None
    agy_pid, port = 1, 6001
    if os.name == "nt":  # the Windows leg reads the pid's real listener table
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        agy_pid, port = os.getpid(), listener.getsockname()[1]
    try:
        out = sess.reply(sess.register({"conversationId": CONV}, agy_pid))
    finally:
        if listener:
            listener.close()
    rec = json.loads(record_path(home).read_text())
    assert (rec["mode"], rec["pid"], rec["ls_address"]) == ("pending", 31337, f"127.0.0.1:{port}")
    assert out["injectSteps"][0]["systemMessage"] == {"systemMessage": sess.NOTE}


def test_pending_leg_is_never_current_for_the_pretool_rewrite(home, ls_server):
    write_leg(home, pid=os.getpid(), agy_pid=1234, ls=ls_server)
    path = home / "unimatrix" / "agy-legs" / f"{CONV}.json"
    assert pre.leg_current(CONV, 1234) is True
    path.write_text(json.dumps(dict(json.loads(path.read_text()), mode="pending")))
    assert pre.leg_current(CONV, 1234) is False


@pytest.mark.parametrize("payload", [None, [], {}, {"conversationId": "../evil"}, {"conversationId": 5}])
def test_sessionstart_bad_payload_starts_nothing(home, agy_env, monkeypatch, payload):
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("started a leg"))
    assert sess.register(payload, 1) is False


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", '{"conversationId": "../x"}'])
def test_sessionstart_entry_point_exits_zero_with_one_json_object(home, stdin):
    env = dict(os.environ, COORDINATOR_SETTINGS_HOME=str(home))
    for k in ("ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CONVERSATION_ID"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, str(BIN / "unimatrix-agy-hook-sessionstart.py")], input=stdin,
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and json.loads(p.stdout) == {}


def test_sessionstart_internal_exception_still_emits_json(monkeypatch, capsys):
    monkeypatch.setattr(sess, "register", lambda *a: 1 / 0)
    monkeypatch.setattr(sess.json, "load", lambda f: {})
    assert sess.main() == 0
    assert json.loads(capsys.readouterr().out) == {}


def _worker_env():
    return load("unimatrix-codex-hook-sessionstart").worker_env_name()


def test_sessionstart_worker_marker_adds_pointer_step_even_unregistered(home, monkeypatch, capsys):
    monkeypatch.setenv(_worker_env(), "1")
    monkeypatch.setattr(sess, "register", lambda *a: False)
    monkeypatch.setattr(sess.json, "load", lambda f: {})
    assert sess.main() == 0
    steps = json.loads(capsys.readouterr().out)["injectSteps"]
    assert len(steps) == 1 and "worker-mode.md" in steps[0]["systemMessage"]["systemMessage"]


def test_sessionstart_worker_marker_keeps_registration_note(home, monkeypatch, capsys):
    monkeypatch.setenv(_worker_env(), "1")
    monkeypatch.setattr(sess, "register", lambda *a: True)
    monkeypatch.setattr(sess.json, "load", lambda f: {})
    sess.main()
    texts = [s["systemMessage"]["systemMessage"] for s in json.loads(capsys.readouterr().out)["injectSteps"]]
    assert texts[0] == sess.NOTE and len(texts) == 2


def test_sessionstart_without_worker_marker_adds_no_pointer(home, monkeypatch, capsys):
    monkeypatch.delenv(_worker_env(), raising=False)
    monkeypatch.setattr(sess, "register", lambda *a: True)
    monkeypatch.setattr(sess.json, "load", lambda f: {})
    sess.main()
    steps = json.loads(capsys.readouterr().out)["injectSteps"]
    assert [s["systemMessage"]["systemMessage"] for s in steps] == [sess.NOTE]


def test_sessionstart_script_hardcodes_no_home_path():
    text = (BIN / "unimatrix-agy-hook-sessionstart.py").read_text()
    assert "/Users/" not in text


def test_agy_leg_log_path_resolves_at_call_time(tmp_path, monkeypatch):
    monkeypatch.delenv("AGY_LEG_LOG", raising=False)
    leg = load("unimatrix-agy-leg")
    assert leg.LOG is None
    assert leg.log_path() == str(BIN / "unimatrix-agy-leg.log")
    monkeypatch.setenv("AGY_LEG_LOG", str(tmp_path / "late.log"))
    assert leg.log_path() == str(tmp_path / "late.log")
    leg.log("hello-late")
    assert "hello-late" in (tmp_path / "late.log").read_text()
