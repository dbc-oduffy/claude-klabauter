"""Behavior pins for the agy PreToolUse hook and the leg launcher it prefixes."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

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


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


def run_payload(*, conv=CONV, name="run_command", cmd="ls -la"):
    return {"conversationId": conv, "toolCall": {"name": name, "args": {"CommandLine": cmd}}}


def assert_fail_open(out):
    """The no-verdict answer: an allow whose reason keeps the fail-open benchmarks' marker."""
    assert out["decision"] == "allow" and "guard did not run" in out["reason"] and "overwrite" not in out


def test_no_verdict_answer_is_allow_through_the_entry_point(home):
    r = hook("{}", home)
    assert r.returncode == 0 and json.loads(r.stdout)["decision"] == "allow"


SEND = str(pre.SEND_SCRIPT)
PY = sys.executable


def test_entry_point_with_missing_engine_root_allows_without_rewrite(home):
    r = subprocess.run([sys.executable, str(PRETOOL), "--engine-root", str(home / "none")],
                       input=json.dumps(run_payload()), capture_output=True, text=True,
                       env=dict(os.environ, COORDINATOR_SETTINGS_HOME=str(home)))
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["decision"] == "allow"
    assert "overwrite" not in out


def hook(stdin, home):
    return subprocess.run([sys.executable, str(PRETOOL)], input=stdin, capture_output=True, text=True,
                          env=dict(os.environ, COORDINATOR_SETTINGS_HOME=str(home)))


@pytest.mark.parametrize("stdin", ["", "not json", "{}", "[]", json.dumps(run_payload(name="read_file"))])
def test_entry_point_always_emits_one_decision_and_exits_zero(home, stdin):
    r = hook(stdin, home)
    assert r.returncode == 0
    assert json.loads(r.stdout)["decision"] == "allow"


@pytest.mark.parametrize("stdin", ["", "not json", "[]"])
def test_entry_point_with_no_verdict_input_names_the_guard_did_not_run(home, stdin):
    r = hook(stdin, home)
    assert r.returncode == 0
    assert_fail_open(json.loads(r.stdout))


def test_entry_point_passes_a_real_run_command_through_unrewritten_and_starts_no_leg(home):
    r = hook(json.dumps(run_payload()), home)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["decision"] == "allow" and "overwrite" not in out
    assert not (home / "unimatrix" / "agy-legs" / f"{CONV}.json").exists()


# --- launcher -------------------------------------------------------------------------------


# --- SessionStart hook ----------------------------------------------------------------------

sess = load("unimatrix-agy-hook-sessionstart")


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", '{"conversationId": "../x"}'])
def test_sessionstart_entry_point_exits_zero_with_one_json_object(home, stdin):
    env = dict(os.environ, COORDINATOR_SETTINGS_HOME=str(home))
    for k in ("ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_LS_ADDRESS", "ANTIGRAVITY_CONVERSATION_ID"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, str(BIN / "unimatrix-agy-hook-sessionstart.py")], input=stdin,
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and json.loads(p.stdout) == {}


# --- agy ancestor walk and workspace cwd ------------------------------------------------------


HOOK_PID, LAUNCHER_PID, AGY_PID = 100, 200, 300
AGY_TREE = {HOOK_PID: (LAUNCHER_PID, "python.exe"), LAUNCHER_PID: (AGY_PID, "cmd.exe"),
            AGY_PID: (400, "agy.exe"), 400: (1, "explorer.exe")}


PRINT_LINES = ['agy -p="Run exactly: git grep x" --output-format json', "agy.exe --print hi", "agy --print=hi", "agy -p hi"]


DAEMON_PID, CLI_PID = 2895, 3001


PASS_ASK = {"decision": "ask"}
