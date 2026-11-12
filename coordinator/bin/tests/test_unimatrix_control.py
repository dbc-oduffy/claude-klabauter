"""Behavior pins for the unimatrix control key's forced command: `launch` and `wake` only."""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

CONTROL = Path(__file__).resolve().parents[1] / "unimatrix-control.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_control_under_test", CONTROL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Spawner:
    def __init__(self, pid=4242):
        self.pid, self.calls = pid, []

    def __call__(self, cmd, log, **kw):
        self.calls.append((cmd, log, kw))
        return self.pid


class Runner:
    """Stands in for subprocess.run: records argv and kwargs, replies a canned launcher result."""

    def __init__(self, stdout='{"name": "n", "id": "sid-1", "session_id": "minted"}\n', returncode=0, exc=None):
        self.stdout, self.returncode, self.exc, self.calls = stdout, returncode, exc, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_parse_accepts_launch_and_wake():
    c = load()
    assert c.parse_mode("launch claude_klabauter") == ("launch", "claude_klabauter")
    assert c.parse_mode("wake claude_klabauter-a1b2c3") == ("wake", "claude_klabauter-a1b2c3")


@pytest.mark.parametrize("cmd", [
    "", "launch", "launch /abs/path", "launch ../x", "launch a b", "launch a;id", "launch $(id)", "launch  a",
    "wake", "wake a b", "wake ../x", "wake a;id", "wake a\nid", "roster", "send 1", "inbox x", "put x",
    "launch " + "a" * 57,
])
def test_parse_refuses_other_shapes(cmd):
    assert load().parse_mode(cmd) is None


def test_launch_runs_claude_launcher_in_repo_and_returns_printed_id(tmp_path):
    c, run = load(), Runner()
    reply = c.launch("claude_klabauter", resolve=lambda key: str(tmp_path), runner=run)
    assert re.fullmatch(r"claude_klabauter-[0-9a-f]{6}", reply["session_name"])
    assert reply == {"ok": True, "session_name": reply["session_name"], "session_id": "sid-1", "pid": None,
                     "family": "claude"}
    cmd, kw = run.calls[0]
    assert cmd[1].endswith("headless-em-launch.py")
    assert cmd[2:] == ["claude", "--name", reply["session_name"]]
    assert kw["cwd"] == str(tmp_path) and kw["capture_output"] is True and kw["timeout"] > 0


def test_launch_names_are_distinct():
    c = load()
    names = {c.launch("k", resolve=lambda key: str(Path.cwd()), runner=Runner())["session_name"]
             for _ in range(8)}
    assert len(names) == 8


def test_launch_unknown_repo_key_is_refused_without_running():
    c, run = load(), Runner()
    reply = c.launch("nope", resolve=lambda key: None, runner=run)
    assert reply["ok"] is False and not run.calls


@pytest.mark.parametrize("run", [
    Runner(returncode=1), Runner(stdout=""), Runner(stdout="not json"), Runner(stdout='{"id": 5}'),
    Runner(stdout='{"id": "a b"}'), Runner(stdout='{"session_id": "only-minted"}'), Runner(exc=subprocess.TimeoutExpired("x", 1)),
    Runner(exc=OSError("boom")),
])
def test_launch_refuses_failed_or_unparseable_launcher(run, tmp_path):
    reply = load().launch("k", resolve=lambda key: str(tmp_path), runner=run)
    assert reply["ok"] is False and "error" in reply


def test_wake_claude_resumes_recorded_id_in_recorded_cwd(tmp_path):
    c, run, spawn = load(), Runner(stdout='{"id": "abc-123", "session_id": "minted"}'), Spawner()
    entry = {"sessionId": "abc-123", "cwd": str(tmp_path)}
    reply = c.wake("n-1", sessions=lambda name: [(entry, False)], runner=run, spawner=spawn)
    assert reply == {"ok": True, "session_name": "n-1", "session_id": "abc-123", "pid": None, "family": "claude"}
    cmd, kw = run.calls[0]
    assert cmd[2:] == ["claude", "--resume", "abc-123"] and kw["cwd"] == str(tmp_path)
    assert not spawn.calls


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_wake_non_claude_vendor_still_detaches_under_recorded_id(platform, tmp_path):
    c, spawn, run = load(), Spawner(), Runner()
    entry = {"sessionId": "abc-123", "cwd": str(tmp_path), "vendor": "grok"}
    reply = c.wake("n-1", sessions=lambda name: [(entry, False)], spawner=spawn, platform=platform, runner=run)
    assert reply["ok"] is True and reply["session_name"] == "n-1" and reply["family"] == "grok"
    cmd, _, kw = spawn.calls[0]
    assert cmd[2:] == ["grok", "--session-id", "abc-123", "--name", "n-1"]
    assert kw["cwd"] == str(tmp_path) and kw["platform"] == platform
    assert not run.calls


def test_wake_refuses_unknown_live_and_ambiguous(tmp_path):
    c, spawn = load(), Spawner()
    a = {"sessionId": "a", "cwd": str(tmp_path)}
    b = {"sessionId": "b", "cwd": str(tmp_path)}
    assert c.wake("n", sessions=lambda name: [], spawner=spawn)["ok"] is False
    assert c.wake("n", sessions=lambda name: [(a, True)], spawner=spawn)["ok"] is False
    assert c.wake("n", sessions=lambda name: [(a, False), (b, False)], spawner=spawn)["ok"] is False
    assert c.wake("n", sessions=lambda name: [({"sessionId": "a", "cwd": "/no/such/dir"}, False)],
                  spawner=spawn)["ok"] is False
    assert not spawn.calls


def test_wake_registry_scan_matches_name_and_session_id(tmp_path, monkeypatch):
    c = load()
    monkeypatch.setattr(c, "SESS", str(tmp_path))
    (tmp_path / "99999.json").write_text(
        '{"pid": 99999, "name": "n", "sessionId": "s1", "cwd": "/x"}', encoding="utf-8")
    (tmp_path / "99998.json").write_text(
        '{"pid": 99998, "name": "other", "sessionId": "s2"}', encoding="utf-8")
    monkeypatch.setattr(c, "_load_sibling", lambda f, m: type("L", (), {"entry_live": staticmethod(lambda d: False)}))
    found = c.named_session_entries("n")
    assert [(d["sessionId"], live) for d, live in found] == [("s1", False)]


@pytest.mark.parametrize("cmd", [
    '"/p/python3" "/h/unimatrix/unimatrix-control.py" launch myrepo',
    "/p/python3 /h/unimatrix/unimatrix-control.py launch myrepo",
    "'/p/my dir/python3' '/h/my dir/unimatrix-control.py' launch myrepo",
    '& "C:\\Python\\python.exe" "C:\\Users\\me\\.coordinator-claude-settings\\unimatrix\\unimatrix-control.py" launch myrepo',
    '"C:\\Python\\python.exe" "C:\\Users\\me\\unimatrix\\unimatrix-control.py" launch myrepo',
])
def test_parse_accepts_explicit_prefixed_launch(cmd):
    assert load().parse_mode(cmd) == ("launch", "myrepo")


def test_parse_accepts_explicit_prefixed_wake():
    c = load()
    assert c.parse_mode('"/p/python3" "/h/unimatrix/unimatrix-control.py" wake n-1') == ("wake", "n-1")
    assert c.parse_mode('& "C:\\p\\python.exe" "C:\\h\\unimatrix-control.py" wake n-1') == ("wake", "n-1")


@pytest.mark.parametrize("cmd", [
    '"/p/python3" "/h/unimatrix/other.py" launch myrepo',
    '"/p/python3" "/h/unimatrix/unimatrix-control.py.bak" launch myrepo',
    '"/h/unimatrix/unimatrix-control.py" launch myrepo',
    '& "/h/unimatrix/unimatrix-control.py" launch myrepo',
    'rm -rf x launch myrepo',
    '"/p/python3" "/h/unimatrix/unimatrix-control.py" launch /abs',
    '"/p/python3" "/h/unimatrix/unimatrix-control.py" send 1',
    '"/p/python3" "/h/unimatrix/unimatrix-control.py" launch a;id',
    '"/p/python3" "/h/unimatrix/unimatrix-control.py"  launch myrepo',
    '"/p/python3" "/h/unimatrix/unimatrix-control.py" launch myrepo\nid',
    '"/p/python3 "/h/unimatrix/unimatrix-control.py" launch myrepo',
])
def test_parse_refuses_explicit_prefix_not_naming_the_control_script(cmd):
    assert load().parse_mode(cmd) is None


def test_relay_refuses_the_explicit_form_and_the_bare_form():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_for_control_pin", CONTROL.parent / "unimatrix-relay.py")
    relay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(relay)
    assert relay.parse_mode("launch myrepo") is None
    assert relay.parse_mode('"/p/python3" "/h/unimatrix/unimatrix-control.py" launch myrepo') is None
    assert relay.parse_mode('& "C:\\p\\python.exe" "C:\\h\\unimatrix-control.py" wake n') is None


def run_main(cmd, tmp_path, argv=()):
    env = {"COORDINATOR_SETTINGS_HOME": str(tmp_path), "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    if cmd is not None:
        env["SSH_ORIGINAL_COMMAND"] = cmd
    return subprocess.run([sys.executable, str(CONTROL), *argv], capture_output=True, text=True, timeout=30,
                          env=env, stdin=subprocess.DEVNULL)


def test_main_argv_form_runs_without_ssh_original_command(tmp_path):
    r = run_main(None, tmp_path, argv=["launch", "nosuchrepo"])
    assert r.returncode == 1
    assert json.loads(r.stdout) == {"ok": False, "error": "unknown repo key nosuchrepo"}


def test_main_argv_wins_over_ssh_original_command(tmp_path):
    r = run_main("roster", tmp_path, argv=["launch", "nosuchrepo"])
    assert json.loads(r.stdout)["error"] == "unknown repo key nosuchrepo"


@pytest.mark.parametrize("argv", [["roster", "x"], ["launch", "/abs"], ["launch", "a b"], ["wake", "a;id"]])
def test_main_argv_refuses_other_modes_with_usage(argv, tmp_path):
    r = run_main(None, tmp_path, argv=argv)
    assert r.returncode != 0 and r.stdout == "" and "usage:" in r.stderr


def test_main_explicit_prefixed_ssh_original_command_reaches_the_launch_path(tmp_path):
    r = run_main('"/p/python3" "/h/unimatrix/unimatrix-control.py" launch nosuchrepo', tmp_path)
    assert json.loads(r.stdout) == {"ok": False, "error": "unknown repo key nosuchrepo"}


def test_main_no_argv_and_no_original_command_is_usage(tmp_path):
    r = run_main(None, tmp_path)
    assert r.returncode != 0 and "usage:" in r.stderr


@pytest.mark.parametrize("cmd", ["roster", "send 1", "launch /x", "put drop", "launch a b"])
def test_main_refuses_other_modes_with_usage(cmd, tmp_path):
    r = run_main(cmd, tmp_path)
    assert r.returncode != 0 and r.stdout == "" and "usage:" in r.stderr


def test_main_unknown_repo_key_replies_json_error_and_exits_nonzero(tmp_path):
    r = run_main("launch nosuchrepo", tmp_path)
    assert r.returncode == 1
    assert json.loads(r.stdout) == {"ok": False, "error": "unknown repo key nosuchrepo"}
