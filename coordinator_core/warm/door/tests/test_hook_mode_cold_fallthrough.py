
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm.tests.test_door_read_deadline_posix import (  # noqa: F401 -- fixture
    _make_stub_engine_root,
    runtime_base,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.warm_tier,
    pytest.mark.skipif(os.name == "nt", reason="door_posix is a macOS/Linux binary"),
]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_PAYLOAD = b'{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"echo hi"}}'


@pytest.fixture(scope="module")
def door(tmp_path_factory) -> Path:
    cc = shutil.which("cc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [cc, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""',
         "-o", str(out), str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True,
    )
    return out


def _run(door: Path, root: Path, runtime_base: Path, cold_body: str) -> subprocess.CompletedProcess:
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(cold_body, encoding="utf-8")
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    return subprocess.run(
        [str(door), "hooks.preuse_bash_dispatch"],
        input=_PAYLOAD, capture_output=True, env=env, cwd=str(root), timeout=60,
    )


def _decision(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout.decode("utf-8").strip())["hookSpecificOutput"]


def test_an_unreachable_engine_gets_the_cold_guards_verdict(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    echo_verdict = (
        "import json, sys\n"
        "payload = sys.stdin.read()\n"
        "print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',\n"
        "    'permissionDecision': 'allow', 'permissionDecisionReason': payload}}))\n"
    )
    proc = _run(door, root, runtime_base, echo_verdict)

    assert proc.returncode == 0
    hso = _decision(proc)
    assert hso["permissionDecision"] == "allow"
    assert hso["permissionDecisionReason"] == _PAYLOAD.decode("ascii")


@pytest.mark.parametrize(
    "cold_body",
    [
        pytest.param("import sys\nprint('not a verdict')\nsys.exit(1)\n", id="nonzero-exit"),
        pytest.param("import sys\nsys.stdin.read()\n", id="silent-exit-0"),
    ],
)
def test_a_cold_guard_that_does_not_answer_passes_loudly(door, tmp_path, runtime_base, cold_body):
    root = _make_stub_engine_root(tmp_path)
    proc = _run(door, root, runtime_base, cold_body)

    assert proc.returncode == 0
    assert b"not a verdict" not in proc.stdout
    body = json.loads(proc.stdout.decode("utf-8").strip())
    assert "guard did not run" in body["systemMessage"]
    assert "cold guard" in body["systemMessage"]
    hso = body["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "did not run" in hso["additionalContext"]


def test_no_cold_entrypoint_passes_loudly(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").unlink(missing_ok=True)
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    proc = subprocess.run(
        [str(door), "hooks.preuse_bash_dispatch"],
        input=_PAYLOAD, capture_output=True, env=env, cwd=str(root), timeout=60,
    )

    assert proc.returncode == 0
    body = json.loads(proc.stdout.decode("utf-8").strip())
    assert "no cold entrypoint" in body["systemMessage"]
    assert "permissionDecision" not in body["hookSpecificOutput"]


@pytest.mark.parametrize(
    ("event", "has_hso"),
    [("PostToolUse", True), ("SessionEnd", False), (None, False)],
)
def test_the_loud_pass_names_the_payloads_own_event(door, tmp_path, runtime_base, event, has_hso):
    root = _make_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").unlink(missing_ok=True)
    payload = {"tool_name": "Bash"}
    if event:
        payload["hook_event_name"] = event
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    proc = subprocess.run(
        [str(door), "hooks.preuse_bash_dispatch"],
        input=json.dumps(payload).encode(), capture_output=True, env=env, cwd=str(root), timeout=60,
    )

    assert proc.returncode == 0
    body = json.loads(proc.stdout.decode("utf-8").strip())
    assert "guard did not run" in body["systemMessage"]
    assert ("hookSpecificOutput" in body) is has_hso
    if has_hso:
        assert body["hookSpecificOutput"]["hookEventName"] == event

