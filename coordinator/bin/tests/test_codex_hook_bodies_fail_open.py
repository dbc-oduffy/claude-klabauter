"""Codex hook bodies fail open: each body, run as codex_hook_events emits it, exits 0 on every injected failure.

The command line comes from unimatrix-install.py :: codex_hook_events (interpreter swapped for sys.executable),
so the test runs what Codex runs. Typed-advisory cases pin `<hook>: degraded: <ExcType>: <detail>` on stdout;
host-guard's handled degrades keep the neutral answer, `degraded` stderr state and a degrade-log row.
"""
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("unimatrix_install_codex_fail_open", BIN / "unimatrix-install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

SESSION_ID = "01a20366-0b48-70a3-a581-446f427a61a6"
TIMEOUT_SECS = 60

# body id -> (Codex event, index of the body's handler in that event's hooks list)
BODIES = {
    "sessionstart": ("SessionStart", 0),
    "external-start": ("SessionStart", 1),
    "external-end": ("SessionEnd", 0),
    "guard": ("PreToolUse", 0),
}
KINDS = ("no_root", "absent_stamp", "missing_module", "import_raises", "system_exit", "bad_stdin", "non_object_payload")
POISON = {"import_raises": "raise RuntimeError('boom')\n", "system_exit": "raise SystemExit(3)\n"}


def _write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _engine(tmp_path, kind):
    """An engine root under tmp_path shaped for `kind`; None for no_root."""
    if kind == "no_root":
        return None
    root = tmp_path / "engine"
    core = root / "coordinator_core"
    ok = POISON.get(kind, "")
    for pkg in (core, core / "session", core / "host_hooks"):
        _write(pkg / "__init__.py")
    if kind != "absent_stamp":
        _write(core / "_engine_stamp", "stamp-1\n")
    if kind != "missing_module":
        _write(core / "session" / "external_registry.py",
               ok + "def register_current(payload):\n    return None\n\ndef unregister(session_id, payload=None):\n    return None\n")
        _write(core / "host_hooks" / "shim.py", ok + "def run(host, text):\n    return {}\n")
    _write(core / "host_hooks" / "session_context.py", "def context(payload, env):\n    return {}\n")
    _write(core / "host_hooks" / "codex_lifecycle.py", "def handle(payload, env):\n    return {}\n")
    return root


def _stdin(kind, event):
    if kind == "bad_stdin":
        return "this is not json"
    if kind == "non_object_payload":
        return json.dumps([SESSION_ID])
    payload = {"session_id": SESSION_ID, "transcript_path": None, "cwd": str(BIN), "hook_event_name": event}
    if event == "PreToolUse":
        payload.update(tool_name="Bash", tool_input={"command": "echo hi"})
    return json.dumps(payload)


def _run(tmp_path, monkeypatch, body, kind):
    monkeypatch.setattr(install, "_console_python", lambda: sys.executable)
    engine = _engine(tmp_path, kind)
    event, index = BODIES[body]
    hooks = install.codex_hook_events(BIN, engine)[event]["hooks"]
    argv = shlex.split(hooks[index]["command"])
    script = Path(argv[1])
    assert script.is_file(), argv
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "COORDINATOR_SETTINGS_HOME": str(tmp_path / "settings"),
           "HOME": str(home), "USERPROFILE": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        argv, input=_stdin(kind, event), capture_output=True, text=True, env=env, timeout=TIMEOUT_SECS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc, script.stem


def _cases(bodies, kinds):
    return [pytest.param(body, kind, id=f"{body}-{kind}") for body in bodies for kind in kinds]


GUARD_KINDS = tuple(k for k in KINDS if k != "absent_stamp")
HOOK_BODIES = ("sessionstart", "external-start", "external-end")
RC_CASES = _cases(HOOK_BODIES, KINDS) + _cases(("guard",), GUARD_KINDS)
ADVISORY_CASES = _cases(HOOK_BODIES, KINDS)


@pytest.mark.parametrize("body,kind", RC_CASES)
def test_body_exits_zero_on_injected_failure(tmp_path, monkeypatch, body, kind):
    proc, _ = _run(tmp_path, monkeypatch, body, kind)
    assert proc.returncode == 0, proc.stderr[-800:]


@pytest.mark.parametrize("body,kind", ADVISORY_CASES)
def test_body_prints_typed_advisory_on_injected_failure(tmp_path, monkeypatch, body, kind):
    proc, stem = _run(tmp_path, monkeypatch, body, kind)
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 1, proc.stdout
    obj = json.loads(lines[0])
    assert isinstance(obj, dict), obj
    assert re.match(rf"^{re.escape(stem)}: degraded: \w+: ", obj.get("systemMessage", "")), obj


GUARD_CAUSE = {"no_root": "no_engine_root", "missing_module": "import_error", "import_raises": "import_error",
               "system_exit": "invalid_answer", "bad_stdin": "bad_stdin", "non_object_payload": "bad_stdin"}


@pytest.mark.parametrize("kind", GUARD_KINDS)
def test_guard_handled_degrade_keeps_neutral_answer_and_logs(tmp_path, monkeypatch, kind):
    proc, _ = _run(tmp_path, monkeypatch, "guard", kind)
    assert json.loads(proc.stdout.strip()) == {}
    assert proc.stderr.strip().splitlines()[-1] == "degraded"
    log = tmp_path / "settings" / "host-hooks" / "host-guard-degrades.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["cause"] == GUARD_CAUSE[kind], rows
