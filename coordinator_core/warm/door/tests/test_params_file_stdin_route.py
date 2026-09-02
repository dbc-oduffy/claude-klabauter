"""`--params-file -` is decided pre-delivery and never crosses the door's wire.

WHY THIS FILE EXISTS. The door forwards argv and cwd; outside hook mode it
forwards no stdin. `coordinator-invoke <op> --params-file -` reads its params
payload from THE CALLING PROCESS'S stdin, so a warm-served invocation ran that
read inside a warm pool worker -- `pythonw.exe`-spawned, `sys.stdin is None`
(`warm/server.py :: _bind_null_std_streams` rebinds stdout and stderr only).
The `AttributeError` escaped the handler's `(OSError, UnicodeDecodeError)`
catch as a `-32603`, and a `-32603` reaches the door AFTER delivery, where the
only move left is `emit_indeterminate`. Every `--params-file -` invocation on
this box therefore answered `-32004` -- "the op may have COMPLETED" -- for a
request whose params were never read, while the same op through every other
param route succeeded against the same server in the same second. Trail:
`state/bug-backlog/2026-09-02-warm-engine-door-returns-indeterminate-for-every-op.yaml`.

DISCRIMINATION IS THE POINT. A live server answers throughout the behavioural
leg, so "the door fell through" is a decision this test forced, not the absence
of an endpoint -- the same fixture serves the control invocation warm in the
same function. A gate that fired for every route would fail the control; a gate
that fired for none would fail the subject.

The source legs cover the half no Windows binary can answer: that `door_posix.c`
gates on the SAME shared predicate rather than a second spelling of the flag,
since a POSIX door that misses this shape reproduces the whole defect on a
platform this box cannot run.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.warm.tests.test_door_read_deadline import (
    _make_stub_engine_root,
    _pipe_name_for,
    _FALLBACK_MARKER,
    _ReplyingServer,
)
from coordinator_core.warm.door.tests.test_door_stdin_mode import (
    _DOOR_CORE_C,
    _DOOR_CORE_H,
    _DOOR_POSIX_C,
    _DOOR_WINDOWS_C,
    _door_under_default_name,
    _read,
    _WINDOWS_ONLY,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.warm_tier,
]

_PREDICATE = "door_argv_declares_params_stdin"


# =============================================================================
# The gate is one predicate in shared core, and both transports read it.
# =============================================================================


def test_the_flag_spelling_lives_only_in_shared_core():
    """Neither platform door may carry its own `--params-file` literal: two
    spellings is how the two transports come to disagree about which argv
    shapes name this route, and the POSIX half cannot be compiled or run on
    the box that authors it."""
    header = _read(_DOOR_CORE_H)
    for macro, value in (
        ("DOOR_PARAMS_FILE_FLAG", "--params-file"),
        ("DOOR_PARAMS_FILE_STDIN_VALUE", "-"),
        ("DOOR_PARAMS_FILE_STDIN_JOINED", "--params-file=-"),
    ):
        match = re.search(rf'^#define\s+{macro}\s+"([^"]*)"\s*$', header, re.MULTILINE)
        assert match, f"{macro} is not defined in door_core.h -- renamed or removed"
        assert match.group(1) == value

    for door in (_DOOR_WINDOWS_C, _DOOR_POSIX_C):
        source = _read(door)
        assert "--params-file" not in source, (
            f"{door.name} spells the flag itself instead of reading "
            f"DOOR_PARAMS_FILE_FLAG from door_core.h"
        )


def test_both_doors_gate_on_the_shared_predicate():
    """The predicate is defined once, in `door_core.c`, and called by both
    doors -- so a change to what counts as a declaration lands on both
    transports or on neither."""
    assert f"int {_PREDICATE}(" in _read(_DOOR_CORE_C)
    assert f"int {_PREDICATE}(" in _read(_DOOR_CORE_H)
    for door in (_DOOR_WINDOWS_C, _DOOR_POSIX_C):
        assert _PREDICATE in _read(door), (
            f"{door.name} does not consult {_PREDICATE} -- it will deliver "
            f"a stdin-bound params request warm and answer -32004"
        )


def test_the_gate_precedes_the_transport_in_both_doors():
    """Pre-delivery is the whole property: a gate placed after the dial can
    still produce the indeterminate verdict it exists to prevent. Asserted
    positionally against each door's own pipe/socket call."""
    # door.c reaches the shared predicate through a wide-argv adapter; the
    # POSIX door calls it directly. Each door's own call site is named here.
    for door, call, connect in (
        (_DOOR_WINDOWS_C, _PREDICATE + "_w(", "CreateFileW(pipe_name"),
        (_DOOR_POSIX_C, _PREDICATE + "(", "connect_socket(sock_path)"),
    ):
        source = _read(door)
        main_at = source.index("int main")
        gate_at = source.index(call, main_at)
        connect_at = source.index(connect, main_at)
        assert gate_at < connect_at, (
            f"{door.name} consults {_PREDICATE} only after dialling the "
            f"transport -- the request can still be delivered"
        )


# =============================================================================
# Behavioural leg -- a live server answers throughout, so the fall-through is
# a decision, not a missing endpoint. Windows-only, matching the door tests'
# own gating (this repo's box).
# =============================================================================


def _make_echoing_engine_root(tmp_path: Path) -> Path:
    """`_make_stub_engine_root`'s throwaway root, with a cold entrypoint that
    reports what it read from stdin -- so the test can assert the payload
    survived the hand-off, not merely that a fall-through happened."""
    root = _make_stub_engine_root(tmp_path)
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text(
        "import sys\n"
        f"print({_FALLBACK_MARKER!r})\n"
        "print('ARGV=' + repr(sys.argv[1:]))\n"
        "print('STDIN=' + repr(sys.stdin.buffer.read().decode('utf-8')))\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return root


_WARM_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":'
    '{"stdout":"served-warm\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


def _door_env(root: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env.pop("COORDINATOR_DOOR_STDIN_MODE", None)
    return env


def _run(root: Path, args: list, payload: bytes) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(_door_under_default_name(root)), *args],
        input=payload,
        capture_output=True,
        env=_door_env(root),
        timeout=60,
        cwd=str(root),
        **no_console_creationflags(),
    )


@_WINDOWS_ONLY
def test_the_stdin_route_never_reaches_a_server_the_control_route_reaches(
    tmp_path: Path,
) -> None:
    """SUBJECT AND CONTROL SHARE ONE SERVER, in that order -- which is what
    makes the subject's non-delivery a decision rather than a missing
    endpoint. `_ReplyingServer` serves exactly one connection, so the request
    it ends up holding names which of the two invocations dialled it.

    Ordering is load-bearing in the other direction too: the server is still
    listening, unclaimed, when the subject runs."""
    root = _make_echoing_engine_root(tmp_path)
    payload = b'{"note":"C1\'s half"}'

    server = _ReplyingServer(_pipe_name_for(root), _WARM_REPLY)
    try:
        subject = _run(root, ["ping", "--params-file", "-"], payload)
        control = _run(root, ["ping", "{}"], b"")
    finally:
        server.close()

    # The control claimed the one connection -- so the subject did not.
    request = json.loads(server.request.decode("utf-8").strip())
    assert request["params"]["argv"] == ["ping", "{}"]
    assert control.stdout == b"served-warm\n"
    assert _FALLBACK_MARKER.encode() not in control.stdout

    assert _FALLBACK_MARKER.encode() in subject.stdout
    assert b"-32004" not in subject.stdout
    # The cold child read the caller's own stdin, unconsumed by the door.
    assert repr(payload.decode("utf-8")).encode() in subject.stdout
    assert b"'--params-file', '-'" in subject.stdout


@_WINDOWS_ONLY
def test_a_real_params_file_path_is_served_warm(tmp_path: Path) -> None:
    """`--params-file <path>` names a file the SERVER can open relative to the
    caller's cwd -- it binds no stream, so it is not this gate's business. A
    gate that matched the flag alone would send this route cold too."""
    root = _make_echoing_engine_root(tmp_path)
    params_path = root / "params.json"
    params_path.write_text("{}", encoding="utf-8")

    server = _ReplyingServer(_pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(root, ["ping", "--params-file", str(params_path)], b"")
    finally:
        server.close()

    assert server.request, "a file-form params route did not reach the server"
    assert proc.stdout == b"served-warm\n"
    assert _FALLBACK_MARKER.encode() not in proc.stdout
