"""Behavior pins for the Klabauter host-guard client: open answer per host, bounded wall, no child, deny pass-through."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]
REPO = BIN.parents[1]
SCRIPT = BIN / "unimatrix-host-guard.py"
BUDGET = 0.5
CODEX_PAYLOAD = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                            "tool_input": {"command": "ls"}, "session_id": "s1", "cwd": "/x"})
AGY_PAYLOAD = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "run_command",
                          "tool_input": {"CommandLine": "ls"}, "session_id": "s1", "cwd": "/x"})


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_host_guard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hg = load()
NEUTRAL = {"agy": {"decision": "allow"}, "codex": {}, "grok": {}}
GROK_PAYLOAD = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "edit_file",
                           "tool_input": {"path": "a.py"}, "cwd": "/x"})
GROK_DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                    "permissionDecisionReason": "no"}}


@pytest.fixture(autouse=True)
def settings_home(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm-runtime"))
    return home


@pytest.fixture(autouse=True)
def private_temp_root(tmp_path, monkeypatch):
    """The real shim and guards write managed state under gettempdir(); keep it inside tmp_path so the session Temp stays empty."""
    private = tmp_path / "private-temp"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))


@pytest.fixture(autouse=True)
def classifier_never_abandoned(monkeypatch):
    """A real shim or classifier thread outliving its wall keeps running after teardown restores the session Temp and writes into it."""
    monkeypatch.setattr(hg, "CLASSIFY_WALL_SECS", 20.0)


def degrade_records(home):
    log = home / "host-hooks" / hg.DEGRADE_LOG_NAME
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def assert_open(host, answer, state="degraded"):
    """The host's open answer; an agy degrade is an allow, never the bare ask, and carries a non-empty reason."""
    expected = {"decision": "allow"} if host == "agy" and state == "degraded" else NEUTRAL[host]
    assert {k: v for k, v in answer.items() if k != "reason"} == expected
    if host == "agy" and state == "degraded":
        assert isinstance(answer.get("reason"), str) and answer["reason"]
    elif host != "agy":
        assert "reason" not in answer


def make_engine(tmp_path, shim_src):
    """A stub engine tree on disk, for the CLI tests that run the guard script in its own interpreter."""
    root = tmp_path / "engine"
    pkg = root / "coordinator_core" / "host_hooks"
    pkg.mkdir(parents=True)
    (root / "coordinator_core" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "shim.py").write_text(shim_src)
    return str(root)


def shim_src(body):
    return "import sys\n\ndef run(host, stdin_text):\n    " + body + "\n"


def agy_command(command):
    return json.dumps({"toolCall": {"name": "run_command", "args": {"CommandLine": command}},
                       "workspacePaths": ["/x"], "conversationId": "c1"})


IRREVERSIBLE = ["git clean -fd", "git reset --hard", "git push --force", "git push --force origin topic",
                "git -C sub clean -fdx", "git add . && git reset --hard HEAD~2"]


def test_cli_agy_degrade_denies_irreversible_and_allows_status(tmp_path):
    for command, decision in (("git reset --hard", "deny"), ("git status", "allow")):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--host", "agy", "--engine-root", str(REPO / "no-such-dir")],
                              input=agy_command(command), capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(REPO)})
        assert proc.returncode == 0 and proc.stderr.strip() == "degraded"
        (line,) = proc.stdout.splitlines()
        assert json.loads(line)["decision"] == decision and json.loads(line)["reason"]


GOOD_INNER = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "no"}


CODEX_ADVISORY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "XS budget spent"}}
CODEX_ADVISORY_ALLOW = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                "additionalContext": "XS budget spent"}}


def test_stray_stderr_never_reaches_stdout(tmp_path):
    root = make_engine(tmp_path, shim_src("sys.stderr.write('noise\\n'); return {'decision': 'ask'}"))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--host", "agy", "--engine-root", root],
                          input=AGY_PAYLOAD, capture_output=True, text=True)
    assert proc.stdout.strip() == '{"decision": "allow"}'
    assert "noise" not in proc.stdout


@pytest.mark.parametrize("host,payload", [("agy", AGY_PAYLOAD), ("codex", CODEX_PAYLOAD), ("codex", "garbage"), ("agy", "")])
def test_cli_one_json_line_exit_zero(tmp_path, host, payload):
    root = make_engine(tmp_path, "raise SystemExit(7)\n")
    proc = subprocess.run([sys.executable, str(SCRIPT), "--host", host, "--engine-root", root],
                          input=payload, capture_output=True, text=True)
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert len(lines) == 1
    assert_open(host, json.loads(lines[0]))
    assert proc.stderr.strip() == "degraded"


def test_cli_without_args_is_open_exit_zero():
    proc = subprocess.run([sys.executable, str(SCRIPT)], input="", capture_output=True, text=True)
    assert proc.returncode == 0
    assert len(proc.stdout.splitlines()) == 1 and json.loads(proc.stdout) == {}


def test_cli_timeout_at_default_budget_records_and_stays_exit_zero(tmp_path, settings_home):
    root = make_engine(tmp_path, "import time\n\ndef run(host, stdin_text):\n    time.sleep(60)\n")
    r = subprocess.run([sys.executable, str(SCRIPT), "--host", "codex", "--engine-root", root],
                       input=CODEX_PAYLOAD, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout) == {} and r.stderr.strip() == "degraded"
    (rec,) = degrade_records(settings_home)
    assert rec["cause"] == "timeout" and rec["budget_secs"] == 0.5


GROK_ALLOW_ADVISORY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                               "additionalContext": "advisory text"}}


# --- multi-file apply_patch through the real shim, adapter and guard validation ---------------------


def _add_hunk(name, n):
    return f"*** Add File: {name}\n" + "".join(f"+row {i}\n" for i in range(n))


def _update_hunk(name, removed, added):
    return (f"*** Update File: {name}\n@@\n" + "".join(f"-l{i}\n" for i in range(1, removed + 1))
            + "".join(f"+new{i}\n" for i in range(added)))


GUARD_PATCHES = [
    ("add-add", [_add_hunk("bin/wordcount.py", 42), _add_hunk("tests/test_wordcount.py", 74)], "2/2 files, 116/30 lines"),
    ("update-update", [_update_hunk("a.py", 5, 20), _update_hunk("b.py", 5, 20)], "2/2 files, 50/30 lines"),
    ("add-update", [_add_hunk("bin/new.py", 20), _update_hunk("a.py", 5, 20)], "2/2 files, 45/30 lines"),
]


GROK_NAG_ANSWER = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                          "additionalContext": "NAG-BODY"}}


COLD_CHILD = (
    "import importlib.util, json, sys, time\n"
    "spec = importlib.util.spec_from_file_location('hg_cold', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "answer, state = mod.guard('grok', sys.argv[3], sys.argv[2])\n"
    "print(json.dumps({'answer': answer, 'state': state, 'process_secs': time.process_time()}))\n"
)


def _nag_repo(root, session_id):
    """A git repo whose lane record attributes three over-bound uncommitted edits to `session_id`."""
    root.mkdir()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def git(*args):
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True, creationflags=flags)

    git("init", "-q")
    names = [f"f{i}.txt" for i in range(3)]
    for name in names:
        (root / name).write_text("a\n" * 5)
    git("add", ".")
    git("commit", "-q", "-m", "base")
    for name in names:
        (root / name).write_text("b\n" * 40)
    sys.path.insert(0, str(REPO))
    from coordinator_core.session import xs_lane_budget

    lane = xs_lane_budget.lane_path(root, session_id)
    lane.parent.mkdir(parents=True, exist_ok=True)
    lane.write_text(json.dumps({"kind": "xs", "state": "open", "base_sha": "x", "paths": names}), encoding="utf-8")


def test_cold_process_grok_edit_delivers_the_nag_at_the_default_budget_within_it(tmp_path):
    """A fresh interpreter, default budget, no warm engine: the DIY nag reaches additionalContext and the
    whole process spends less CPU than the budget. Process time, never wall clock."""
    sid = str(uuid.uuid4())
    repo = tmp_path / "grok-repo"
    _nag_repo(repo, sid)
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "search_replace", "session_id": sid,
                          "cwd": str(repo), "tool_input": {"file_path": str(repo / "delta.py"), "old_string": "",
                                                           "new_string": "x = 1\n" * 20}})
    env = {**os.environ, "COORDINATOR_AGENT_PROVIDER": "grok", "TMP": tempfile.gettempdir(),
           "TEMP": tempfile.gettempdir(), "TMPDIR": tempfile.gettempdir()}
    proc = subprocess.run([sys.executable, "-c", COLD_CHILD, str(SCRIPT), str(REPO), payload], capture_output=True,
                          text=True, timeout=120, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    row = json.loads(proc.stdout.strip().splitlines()[-1])
    assert BUDGET == hg.DEFAULT_BUDGET_SECS
    nested = row["answer"].get("hookSpecificOutput", {})
    assert nested.get("additionalContext") and "XS bound" in nested["additionalContext"], row
    assert row["state"] == "neutral-unknown", row
    assert row["process_secs"] < BUDGET, row
