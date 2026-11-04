"""Both host shims as real subprocesses: engine up, engine down, slow engine, and process time.

The shim runs from a stamped hardlink copy of the engine's python sources, because the live tree is
unstamped and the warm client declines an unstamped clone before it dials. A stub engine bound to the
copy's real socket path (`election.socket_path`) answers over the same JSON-RPC line framing the warm
server speaks. Process time is `single_invocation_tree_process_time` (job object / kqueue / ptrace
tree accounting), asserted only after the run's stdout proved the verdict.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(sys.platform == "win32" and not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX"),
]

_REPO = Path(__file__).resolve().parents[3]
_PROCESS_TIME_BUDGET_MS = 200.0
_RUNS_PER_FIGURE = 3

CODEX_LS = {"hook_event_name": "PreToolUse", "session_id": "codex-sess-1", "cwd": "/nonexistent-e2e-cwd",
            "tool_name": "exec_command", "tool_input": {"command": "ls"}}
CODEX_PUSH = {**CODEX_LS, "tool_input": {"command": "git push https://github.com/stranger/r.git main"}}
AGY_LS = {"conversationId": "agy-sess-1", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}
AGY_PUSH = {"conversationId": "agy-sess-1", "toolCall": {"name": "run_command", "args": {
    "CommandLine": "git push https://github.com/stranger/r.git main"}}}

_ENGINE_DENY = {"permissionDecision": "deny", "permissionDecisionReason": "engine says no"}


def _link_or_copy(src: str, dst: str) -> None:
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _build_engine_tree(root: Path) -> None:
    """Python sources of `coordinator_core` (no tests, no caches) hardlinked under `root`, plus an engine stamp."""
    source = _REPO / "coordinator_core"
    for current, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if d not in {"tests", "__pycache__", "fixtures", "node_modules"} and not d.startswith(".")]
        target = root / "coordinator_core" / Path(current).relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.endswith(".py"):
                _link_or_copy(os.path.join(current, name), str(target / name))
    (root / "coordinator_core" / "_engine_stamp").write_text("shim-e2e-%d-%d\n" % (os.getpid(), time.time_ns()), encoding="utf-8")
    # The client spawns warm/server.py on a refused dial; the copy's spawn target is inert.
    server = root / "coordinator_core" / "warm" / "server.py"
    if server.exists():
        server.unlink()
    server.write_text("raise SystemExit(0)\n", encoding="utf-8")


class _StubEngine:
    """A line-framed JSON-RPC server on the engine copy's socket path."""

    def __init__(self, path: Path, result, delay: float = 0.0):
        self.requests: list = []
        self._result = result
        self._delay = delay
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn) -> None:
        try:
            with conn, conn.makefile("rwb") as fh:
                line = fh.readline()
                if not line.strip():
                    return
                request = json.loads(line)
                self.requests.append(request)
                time.sleep(self._delay)
                fh.write(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": self._result}).encode() + b"\n")
                fh.flush()
        except OSError:
            return  # the client hung up on a slow answer

    def close(self) -> None:
        self._stop = True
        with contextlib.suppress(OSError):
            self._sock.close()


@pytest.fixture(scope="module")
def world():
    """(engine tree, shim env, socket path), with the interpreter warmed so timings exclude pyc compilation."""
    from coordinator_core.warm import breadcrumb, election, skew

    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="she-", dir=None if os.name == "nt" else "/tmp")))
    tree = base / "tree"
    tree.mkdir()
    _build_engine_tree(tree)
    settings = base / "settings"
    registry = base / "registry"
    settings.mkdir()
    registry.mkdir()
    (registry / "registry.local.toml").write_text('[policy]\nnon_owned_remotes = ["github.com/stranger/"]\n', encoding="utf-8")
    runtime = base / "rt"
    runtime.mkdir()

    env = {k: v for k, v in os.environ.items()
           if k != "PYTEST_CURRENT_TEST" and not k.startswith("_COORDINATOR_TEST_")
           and k not in {"COORDINATOR_CLONE_ROOT", "COORDINATOR_SESSION_ID", "PYTHONPATH"}}
    env.update({
        "COORDINATOR_WARM": "1",
        breadcrumb.RUNTIME_BASE_ENV: str(runtime),
        "COORDINATOR_SETTINGS_HOME": str(settings),
        "MACHINE_LOCAL_REGISTRY_DIR": str(registry),
        "PYTHONPATH": str(tree),
    })
    prior = os.environ.get(breadcrumb.RUNTIME_BASE_ENV)
    os.environ[breadcrumb.RUNTIME_BASE_ENV] = str(runtime)
    try:
        sock_path = election.socket_path(skew.compute_client_token(tree), engine_clone=tree)
    finally:
        if prior is None:
            os.environ.pop(breadcrumb.RUNTIME_BASE_ENV, None)
        else:
            os.environ[breadcrumb.RUNTIME_BASE_ENV] = prior
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    _run_shim(tree, env, "codex", {})
    try:
        yield tree, env, sock_path
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _argv(host: str) -> list:
    return [sys.executable, "-m", "coordinator_core.host_hooks.shim", "--host", host]


def _run_shim(tree: Path, env: dict, host: str, payload: dict, extra_env: dict = None) -> dict:
    proc = subprocess.run(
        _argv(host), input=json.dumps(payload), capture_output=True, text=True, cwd=str(tree),
        env={**env, **(extra_env or {})}, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@contextlib.contextmanager
def _stdin_from(path: str):
    """The measured child inherits fd 0; point it at `path` for the duration."""
    saved = os.dup(0)
    handle = open(path, "rb")
    try:
        os.dup2(handle.fileno(), 0)
        if os.name == "nt":
            import ctypes
            import msvcrt

            ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))
        yield
    finally:
        os.dup2(saved, 0)
        if os.name == "nt":
            import ctypes
            import msvcrt

            ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))
        os.close(saved)
        handle.close()


def _measure(tree: Path, env: dict, host: str, payload: dict, scratch: Path) -> tuple:
    """(answer, best process_time_ms over runs). The answer is read from the measured run's stdout."""
    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

    stdin_path = scratch / "stdin.json"
    stdin_path.write_text(json.dumps(payload), encoding="utf-8")
    best = None
    answer = None
    for i in range(_RUNS_PER_FIGURE):
        out = scratch / ("out-%d.txt" % i)
        err = scratch / ("err-%d.txt" % i)
        with _stdin_from(str(stdin_path)):
            result = single_invocation_tree_process_time(
                _argv(host), env=env, cwd=str(tree), stdout_path=str(out), stderr_path=str(err),
            )
        assert result["rc"] == 0, err.read_text(encoding="utf-8", errors="replace")
        answer = json.loads(out.read_text(encoding="utf-8"))
        best = result["process_time_ms"] if best is None else min(best, result["process_time_ms"])
    return answer, best


# ---- engine up ---------------------------------------------------------------------------------

@pytest.mark.parametrize("host,payload,expected", [
    ("codex", CODEX_PUSH, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": "engine says no"}}),
    ("agy", AGY_PUSH, {"decision": "deny", "reason": "engine says no"}),
])
def test_engine_up_deny_is_rendered_with_payload_session_identity(world, host, payload, expected):
    tree, env, sock_path = world
    engine = _StubEngine(sock_path, _ENGINE_DENY)
    try:
        assert _run_shim(tree, env, host, payload) == expected
    finally:
        engine.close()
        sock_path.unlink(missing_ok=True)
    assert len(engine.requests) == 1
    request = engine.requests[0]
    assert request["method"] == "warm_guard.evaluate"
    want = "codex-sess-1" if host == "codex" else "agy-sess-1"
    assert request["params"]["payload"]["session_id"] == want
    assert request["_caller"]["session_id"] == want


@pytest.mark.parametrize("host,payload,expected", [
    ("codex", CODEX_LS, {}),
    ("agy", AGY_LS, {"decision": "allow"}),
])
def test_engine_up_allow_is_not_overridden_by_backstop(world, host, payload, expected):
    tree, env, sock_path = world
    engine = _StubEngine(sock_path, {})
    try:
        assert _run_shim(tree, env, host, payload) == expected
        push = CODEX_PUSH if host == "codex" else AGY_PUSH
        assert _run_shim(tree, env, host, push) == expected
    finally:
        engine.close()
        sock_path.unlink(missing_ok=True)


# ---- engine down -------------------------------------------------------------------------------

@pytest.mark.parametrize("host,payload,expected", [
    ("codex", CODEX_LS, {}),
    ("agy", AGY_LS, {"decision": "ask", "reason": "coordinator: guard did not run"}),
])
def test_engine_down_ordinary_command_gets_the_neutral_fallback(world, host, payload, expected):
    tree, env, _ = world
    assert _run_shim(tree, env, host, payload) == expected


@pytest.mark.parametrize("host,payload", [("codex", CODEX_PUSH), ("agy", AGY_PUSH)])
def test_engine_down_irreversible_command_is_denied_by_the_backstop(world, host, payload):
    tree, env, _ = world
    answer = _run_shim(tree, env, host, payload)
    if host == "codex":
        assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert answer["decision"] == "deny"


def test_slow_engine_past_budget_gets_the_neutral_fallback_inside_the_wall(world):
    tree, env, sock_path = world
    engine = _StubEngine(sock_path, {}, delay=1.5)
    try:
        started = time.monotonic()
        assert _run_shim(tree, env, "codex", CODEX_LS) == {}
        assert time.monotonic() - started < 1.5
    finally:
        engine.close()
        sock_path.unlink(missing_ok=True)


def test_unserved_event_never_dials(world):
    tree, env, sock_path = world
    engine = _StubEngine(sock_path, _ENGINE_DENY)
    try:
        assert _run_shim(tree, env, "codex", {**CODEX_PUSH, "hook_event_name": "PostToolUse"}) == {}
    finally:
        engine.close()
        sock_path.unlink(missing_ok=True)
    assert engine.requests == []


# ---- process time ------------------------------------------------------------------------------

@pytest.mark.parametrize("host,payload,engine_result,check", [
    ("codex", CODEX_PUSH, _ENGINE_DENY, lambda a: a["hookSpecificOutput"]["permissionDecision"] == "deny"),
    ("agy", AGY_PUSH, _ENGINE_DENY, lambda a: a["decision"] == "deny"),
])
def test_process_time_engine_up_under_budget(world, tmp_path, host, payload, engine_result, check):
    tree, env, sock_path = world
    engine = _StubEngine(sock_path, engine_result)
    try:
        answer, process_ms = _measure(tree, env, host, payload, tmp_path)
    finally:
        engine.close()
        sock_path.unlink(missing_ok=True)
    assert check(answer), answer
    assert len(engine.requests) == _RUNS_PER_FIGURE
    assert process_ms < _PROCESS_TIME_BUDGET_MS, "engine-up %s shim took %.1fms process time" % (host, process_ms)


@pytest.mark.parametrize("host,payload,check", [
    ("codex", CODEX_PUSH, lambda a: a["hookSpecificOutput"]["permissionDecision"] == "deny"),
    ("agy", AGY_PUSH, lambda a: a["decision"] == "deny"),
    ("codex", CODEX_LS, lambda a: a == {}),
    ("agy", AGY_LS, lambda a: a["decision"] == "ask"),
])
def test_process_time_engine_down_under_budget(world, tmp_path, host, payload, check):
    tree, env, _ = world
    answer, process_ms = _measure(tree, env, host, payload, tmp_path)
    assert check(answer), answer
    assert process_ms < _PROCESS_TIME_BUDGET_MS, "engine-down %s shim took %.1fms process time" % (host, process_ms)
