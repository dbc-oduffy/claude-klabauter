"""A hook-mode door allows a non-irreversible payload on every engine failure.

Pins both sides: a `git commit` payload whose engine answers -32004 worker_died
or never answers gets one advisory line and no deny; a `git push` payload on
the same failures is still denied. Drives the built POSIX door at default
hook-mode arguments; the Windows twin is checked textually.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import DOOR_POSIX_C, DOOR_WINDOWS_C
from coordinator_core.warm.tests.test_door_read_deadline_posix import (  # noqa: F401 -- fixture
    _make_socket_dir,
    _make_stub_engine_root,
    _socket_path_for,
    _WedgedServer,
    runtime_base,
)
from coordinator_core.win_portability import no_console_creationflags

_DOOR_DIR = Path(__file__).resolve().parents[1]
_PRECHECK_OP = "hooks.preuse_bash_dispatch"
_OTHER_OP = "invoke.from_argv"
_WORKER_DIED = (
    '{"jsonrpc":"2.0","id":1,"error":{"code":-32004,"message":"warm dispatch indeterminate '
    "[worker_died: op='invoke.from_argv' BrokenProcessPool]\"}}\n"
)
_posix_only = pytest.mark.skipif(os.name == "nt", reason="door_posix is a macOS/Linux binary")


@pytest.fixture(scope="module")
def door(tmp_path_factory) -> Path:
    cc = shutil.which("cc") or shutil.which("clang")
    if cc is None:
        pytest.skip("no C compiler")
    out = tmp_path_factory.mktemp("door") / "coordinator-invoke"
    subprocess.run(
        [cc, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""',
         "-o", str(out), str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


class _ReplyServer:
    """Accepts one connection, reads the request line, answers `reply`."""

    def __init__(self, sock_path: Path, reply: bytes) -> None:
        self._path = sock_path
        self._reply = reply
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(sock_path))
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
            seen = b""
            while b"\n" not in seen:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                seen += chunk
            conn.sendall(self._reply)
            conn.close()
        except OSError:
            pass

    def close(self) -> None:
        self._thread.join(timeout=10)
        try:
            self._sock.close()
        except OSError:
            pass
        self._path.unlink(missing_ok=True)


def _payload(command: str) -> bytes:
    return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                       "tool_input": {"command": command}}).encode()


def _run(door: Path, root: Path, base: Path, op: str, command: str):
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(base),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    return subprocess.run(
        [str(door), op], input=_payload(command), capture_output=True, env=env, timeout=60,
        **no_console_creationflags(),
    )


def _assert_one_line_allow(proc) -> dict:
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.decode("utf-8")
    assert out.count("\n") == 1 and out.endswith("\n"), out
    body = json.loads(out)
    assert "permissionDecision" not in out
    assert body["systemMessage"].startswith("coordinator: engine failed (")
    assert body["systemMessage"].endswith("guard not evaluated, command allowed.")
    assert body["hookSpecificOutput"]["additionalContext"] == body["systemMessage"]
    return body


def _assert_irreversible_deny(proc) -> None:
    body = json.loads(proc.stdout.decode("utf-8"))
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "irreversible-external command is not run unguarded" in body["hookSpecificOutput"]["permissionDecisionReason"]


@_posix_only
@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.cadence
@pytest.mark.parametrize("op", [_PRECHECK_OP, _OTHER_OP])
def test_worker_died_response_allows_a_commit_and_still_denies_a_push(door, tmp_path, runtime_base, op):
    root = _make_stub_engine_root(tmp_path)
    sock = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock)

    server = _ReplyServer(sock, _WORKER_DIED.encode())
    try:
        committed = _run(door, root, runtime_base, op, "git commit -m x")
    finally:
        server.close()
    body = _assert_one_line_allow(committed)
    assert "server returned an error" in body["systemMessage"]

    server = _ReplyServer(sock, _WORKER_DIED.encode())
    try:
        pushed = _run(door, root, runtime_base, op, "git push origin main")
    finally:
        server.close()
    _assert_irreversible_deny(pushed)


@_posix_only
@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.cadence
def test_a_never_answering_engine_allows_a_commit_and_still_denies_a_push(door, tmp_path, runtime_base):
    root = _make_stub_engine_root(tmp_path)
    sock = _socket_path_for(runtime_base, root)
    _make_socket_dir(sock)

    wedge = _WedgedServer(sock)
    try:
        committed = _run(door, root, runtime_base, _PRECHECK_OP, "git commit -m x")
    finally:
        wedge.close()
    body = _assert_one_line_allow(committed)
    assert "no response within" in body["systemMessage"]

    _make_socket_dir(sock)
    wedge = _WedgedServer(sock)
    try:
        pushed = _run(door, root, runtime_base, _PRECHECK_OP, "git push origin main")
    finally:
        wedge.close()
    _assert_irreversible_deny(pushed)


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_both_doors_route_every_non_irreversible_hook_failure_to_the_one_line_allow(source):
    text = source.read_text(encoding="utf-8")
    failure = text[text.index("static int emit_post_delivery_failure("):]
    failure = failure[: failure.index("\n}\n")]
    assert "emit_hook_pass_loudly" not in failure
    assert "if (g_precheck_armed && emit_precheck_if_matched()) return 0;" in failure
    assert "if (g_door_hook_mode) return emit_hook_post_delivery(detail);" in failure
    assert failure.index("emit_hook_deny(reason)") < failure.index("emit_hook_post_delivery(detail)")
    assert "door_engine_failure_allow_envelope(&out, detail, event_name)" in text
    assert "build_hook_post_delivery_envelope" not in text
    assert "!g_precheck_armed && !g_door_hook_mode" in text
