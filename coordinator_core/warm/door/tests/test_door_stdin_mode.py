"""The door reads a bounded, mode-gated stdin payload, and hook mode fails closed.

Spec backlink:
docs/plans/2026-08-31-the-door-reads-stdin-and-the-payload-lands-flat.md § C1,
resting on docs/research/spike-verdicts/2026-08-31-door-bounded-stdin-read.md.

WHY THIS FILE EXISTS. Before this chunk `door.c` contained exactly one
`stdin`-matching token (`#include <stdint.h>`) -- the Bash guard's entire
input IS a stdin payload, so the door could not carry it at all and the
guard fell through to the cold entrypoint instead of denying. The spike
that gates this chunk found the naive fix (an unconditional read) HANGS
FOREVER against a writer that never closes -- measured, still blocked at
3.0s -- which on the Bash hot path is a hang of every Bash call on the
box, strictly worse than the defect being fixed. `mode-gated` is therefore
a correctness requirement, not an ergonomic one, and this file's leg B is
the pinned anti-regression for that hazard, not decoration.

THE TWO PROPERTIES, PULLING IN OPPOSITE DIRECTIONS -- a test that only
checked one would happily pass a "fix" that reintroduces either defect:
  (a)/(c)/(d) CAPABILITY. A caller that DECLARES the mode gets its payload
      through, bounded, intact across chunk boundaries, and refused rather
      than truncated if it is too large.
  (b) NON-HANG. A caller that declares NOTHING never touches stdin at all,
      even against an inherited stdin whose writer never closes.
  (f) FAIL-CLOSED. Hook mode's disposition on an unreachable engine is an
      affirmative `permissionDecision: deny`, never the ordinary
      fall-through -- the plan's second half, and equally load-bearing:
      landing the read without this ships a guard that receives its
      payload and still fails open on a dead endpoint.

NO LIVE WARM SERVER IS INVOLVED, deliberately, following
`test_door_read_deadline.py`'s own precedent: the stub server (borrowed
from that module) is a real named pipe created through `election.elect()`
against a throwaway engine root, keying a pipe hash no other process on
this box uses. The resident fleet server is never consulted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.warm.tests.test_door_read_deadline import (
    _door_default_entrypoint,
    _make_stub_engine_root,
    _pipe_name_for,
    _FALLBACK_MARKER,
    _ReplyingServer,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.warm_tier,
]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_DOOR_CORE_H = _DOOR_DIR / "door_core.h"
_DOOR_CORE_C = _DOOR_DIR / "door_core.c"
_DOOR_WINDOWS_C = _DOOR_DIR / "door.c"
_DOOR_POSIX_C = _DOOR_DIR / "door_posix.c"
_DOOR_EXE = _DOOR_DIR / "door.exe"

_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="door.exe is a Windows binary"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _header_define(macro: str) -> str:
    """Reads a `#define <macro> "<value>"` string literal out of
    `door_core.h`, so this file's assertions track the source they are
    about rather than a literal that can silently drift away from it."""
    source = _read(_DOOR_CORE_H)
    match = re.search(rf'^#define\s+{macro}\s+"([^"]*)"\s*$', source, re.MULTILINE)
    assert match, f"{macro} not found in {_DOOR_CORE_H} -- renamed or removed"
    return match.group(1)


def _header_int_define(macro: str) -> int:
    source = _read(_DOOR_CORE_H)
    match = re.search(rf"^#define\s+{macro}\s+\(?([0-9u* ]+)\)?\s*$", source, re.MULTILINE)
    assert match, f"{macro} not found in {_DOOR_CORE_H} -- renamed or removed"
    expr = match.group(1).replace("u", "").replace(" ", "")
    value = 1
    for part in expr.split("*"):
        value *= int(part)
    return value


_STDIN_MODE_ENV = _header_define("DOOR_STDIN_MODE_ENV_NAME")
_STDIN_MODE_HOOK_VALUE = _header_define("DOOR_STDIN_MODE_HOOK_VALUE")
_STDIN_MAX_BYTES = _header_int_define("DOOR_STDIN_MAX_BYTES")
_STDIN_READ_CHUNK_BYTES = _header_int_define("DOOR_STDIN_READ_CHUNK_BYTES")


def _door_under_default_name(engine_root: Path) -> Path:
    """Same reasoning as `test_door_read_deadline.py`'s own helper: the
    build artifact is `door.exe`, but `fall_through`'s name-aware cold leg
    and the stub fixture's `coordinator-invoke.py` are both keyed on the
    door's DEFAULT entrypoint name, so the binary must be installed under
    that name to exercise either."""
    installed = engine_root / (_door_default_entrypoint() + ".exe")
    if not installed.exists():
        shutil.copy2(_DOOR_EXE, installed)
    return installed


def _run_door(
    engine_root: Path,
    timeout: float,
    *,
    hook_mode: bool = False,
    stdin_payload: bytes | None = None,
) -> subprocess.CompletedProcess:
    """Runs `door.exe ping` against `engine_root`, optionally declaring
    stdin mode and piping a payload. `hook_mode=False` with `stdin_payload`
    set would be a caller error in production (nothing reads it) and is
    deliberately not supported here -- this fixture only speaks the one
    declared mode this chunk defines."""
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(engine_root)
    if hook_mode:
        env[_STDIN_MODE_ENV] = _STDIN_MODE_HOOK_VALUE
    else:
        env.pop(_STDIN_MODE_ENV, None)
    return subprocess.run(
        [str(_door_under_default_name(engine_root)), "ping"],
        input=stdin_payload,
        capture_output=True,
        env=env,
        timeout=timeout,
        cwd=str(engine_root),
        **no_console_creationflags(),
    )


# =============================================================================
# (e) THE MODE GATE AND THE BOUND LIVE IN SHARED CORE, NOT PER-PLATFORM.
#
# Cheap, no-binary-needed source checks -- the same shape
# `test_door_undispatched_classification.py` uses for the same reason: a
# test that only builds and runs `door.exe` has checked Windows's own
# behaviour, not the CLAIM that the two transports agree on what "hook
# mode" means or how large a payload it accepts.
# =============================================================================


def test_stdin_mode_env_name_and_hook_value_are_defined_once_in_shared_core():
    header_source = _read(_DOOR_CORE_H)
    assert header_source.count("#define DOOR_STDIN_MODE_ENV_NAME") == 1
    assert header_source.count("#define DOOR_STDIN_MODE_HOOK_VALUE") == 1
    assert header_source.count("#define DOOR_STDIN_MAX_BYTES") == 1
    assert header_source.count("#define DOOR_STDIN_READ_CHUNK_BYTES") == 1

    for source_path in (_DOOR_CORE_C, _DOOR_WINDOWS_C, _DOOR_POSIX_C):
        source = _read(source_path)
        assert "#define DOOR_STDIN_MODE_ENV_NAME" not in source, (
            f"{source_path.name} redefines DOOR_STDIN_MODE_ENV_NAME -- it must "
            "come from door_core.h alone, or the two doors can recognise "
            "different declarations as hook mode"
        )
        assert "#define DOOR_STDIN_MAX_BYTES" not in source, (
            f"{source_path.name} redefines DOOR_STDIN_MAX_BYTES -- a bound "
            "that can drift per-platform is a mode gate that will diverge"
        )


def test_both_doors_include_shared_core_and_neither_defines_its_own_drain_loop():
    """`door_drain_stdin_bounded` and `build_hook_deny_envelope` are shared
    verbatim -- a door that defined its own body for either could refuse at
    a different bound or emit a different deny shape than its sibling."""
    for source_path in (_DOOR_WINDOWS_C, _DOOR_POSIX_C):
        source = _read(source_path)
        assert '#include "door_core.h"' in source
        assert not re.search(
            r"door_stdin_status_t\s+door_drain_stdin_bounded\s*\([^)]*\)\s*\{",
            source,
        ), f"{source_path.name} defines its own door_drain_stdin_bounded -- the doors have drifted"
        assert not re.search(
            r"int\s+build_hook_deny_envelope\s*\([^)]*\)\s*\{", source
        ), f"{source_path.name} defines its own build_hook_deny_envelope -- the doors have drifted"


def test_both_doors_gate_fall_through_on_the_same_flag_name():
    """Both platform `fall_through` functions must check a hook-mode flag as
    their own first statement -- the single choke point every existing
    fall-through call site (and any added later) funnels through. This does
    not prove EVERY call site is covered (the binary tests below do that
    behaviourally); it pins that the inversion lives at the choke point
    rather than being reachable only by threading a parameter through every
    call site by hand, which is how a future call site could be added
    without it."""
    for source_path in (_DOOR_WINDOWS_C, _DOOR_POSIX_C):
        source = _read(source_path)
        match = re.search(
            r"static int fall_through\([^)]*\)\s*\{(.{0,1500})", source, re.DOTALL
        )
        assert match, f"fall_through not found in {source_path.name}"
        assert "g_door_hook_mode" in match.group(1), (
            f"{source_path.name}'s fall_through does not check the hook-mode "
            "flag as an early statement"
        )


def test_stdin_max_bytes_is_not_a_this_box_measurement():
    """Anti-scope: the spike's timings gate NOTHING about this bound (cost
    is flat to 256KB and the spike says so explicitly). Scoped to the new
    stdin block's own comment, not the whole file -- `door.c` legitimately
    discusses the real (and separately withdrawn) 15.6ms scheduler-tick
    figure elsewhere, for the unrelated read-deadline mechanism; only the
    NEW bound must not cite it."""
    header_source = _read(_DOOR_CORE_H)
    match = re.search(
        r"#define DOOR_STDIN_MAX_BYTES.*?(?=#define DOOR_STDIN_READ_CHUNK_BYTES)",
        header_source,
        re.DOTALL,
    )
    assert match, "DOOR_STDIN_MAX_BYTES block not found in door_core.h"
    assert "15.6" not in match.group(0)


def test_try_warm_dispatch_inner_forwards_params_verbatim():
    """PRODUCER INVARIANT (docs/plans/2026-08-30-the-warm-envelope-s-two-
    producers-cannot.md): the door is not the only producer of a warm
    request. `warm.client._try_warm_dispatch_inner` is the other, and this
    chunk's new `params.stdin` field must survive it unchanged for a Python
    caller that builds its own `params` dict the way the door does in C.

    Asserted at the source level rather than by driving a real dispatch:
    the function's own request-building line merges the caller's `msg` with
    `**msg` and stamps only sibling keys (`_engine_token`, `_caller`, ...)
    at the TOP level, never reaching into or replacing `msg["params"]`. A
    change that started copying/filtering `params` would break this
    pattern match and this test would need re-examination rather than
    silently certifying a producer that now drops the field."""
    from coordinator_core.warm import client as warm_client

    source = Path(warm_client.__file__).read_text(encoding="utf-8")
    match = re.search(
        r"def _try_warm_dispatch_inner\(msg: dict\).*?request = \{\*\*msg, ",
        source,
        re.DOTALL,
    )
    assert match, (
        "_try_warm_dispatch_inner no longer builds its request as "
        "`{**msg, ...}` -- re-verify params.stdin still survives this "
        "producer before trusting this test's PASS"
    )
    body = source[match.end():match.end() + 4000]
    # None of the additive envelope-level stamps this function performs may
    # touch `msg["params"]` or `request["params"]` -- they are all siblings
    # of it (`_caller`, `_publish_lane`, `_settings_home_claim`), never a
    # rebuild of `params` itself.
    assert 'request["params"]' not in body
    assert 'msg["params"]' not in body
    assert 'msg.pop("params"' not in body


# =============================================================================
# Behavioural legs -- require a compiled door.exe. Windows-only, matching
# `test_door_read_deadline.py`'s own gating (this repo's box).
# =============================================================================


@_WINDOWS_ONLY
def test_declared_mode_with_a_piped_payload_surfaces_it_as_params_stdin(
    tmp_path: Path,
) -> None:
    """(a) THE CAPABILITY. A caller that declares hook mode and pipes a
    payload gets a request whose `params.stdin` carries that payload
    byte-for-byte, and the door relays a decided verdict from the server's
    reply rather than any fall-through shape."""
    root = _make_stub_engine_root(tmp_path)
    payload = b'{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'
    reply = (
        '{"jsonrpc":"2.0","id":1,"result":'
        '{"stdout":"decided\\n","stderr":"","exit_code":0}}\n'
    ).encode("utf-8")

    server = _ReplyingServer(_pipe_name_for(root), reply)
    try:
        proc = _run_door(root, timeout=60, hook_mode=True, stdin_payload=payload)
    finally:
        server.close()

    request = json.loads(server.request.decode("utf-8").strip())
    assert request["params"]["stdin"] == payload.decode("utf-8")
    assert request["params"]["argv"] == ["ping"]

    assert proc.returncode == 0
    assert proc.stdout == b"decided\n"
    assert _FALLBACK_MARKER.encode() not in proc.stdout


@_WINDOWS_ONLY
def test_no_declared_mode_never_reads_stdin_and_returns_promptly(
    tmp_path: Path,
) -> None:
    """(b) THE PINNED ANTI-REGRESSION LEG. No mode declared, and this
    invocation's stdin is a pipe whose writer NEVER closes -- the exact
    hazard the spike measured (still blocked at 3.0s against an
    unconditional read). The door must not touch stdin at all: it falls
    through immediately, well under the falsifier's 2s bar, having read
    nothing."""
    root = _make_stub_engine_root(tmp_path)
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env.pop(_STDIN_MODE_ENV, None)

    proc = subprocess.Popen(
        [str(_door_under_default_name(root)), "ping"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(root),
        **no_console_creationflags(),
    )
    started = time.monotonic()
    try:
        out, err = proc.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(
            "door.exe did not return within the falsifier's 2s bar against an "
            "inherited stdin whose writer never closes -- it touched stdin "
            "despite no declared mode"
        )
    elapsed = time.monotonic() - started
    # No mode declared, so this reached the ordinary pre-delivery
    # fall-through (no server is listening) -- the fixture's marker proves
    # it ran, and NOT the -32004 shape.
    assert _FALLBACK_MARKER.encode() in out
    assert b"-32004" not in out
    assert elapsed < 2.0, f"door.exe took {elapsed:.2f}s -- see falsifier leg B"


@_WINDOWS_ONLY
def test_a_payload_exceeding_the_bound_is_refused_not_truncated(
    tmp_path: Path,
) -> None:
    """(c) A payload one byte over `DOOR_STDIN_MAX_BYTES` is refused --
    hook mode's fail-closed deny, naming the bound -- never silently
    truncated to the bound and forwarded as if it were the whole payload."""
    root = _make_stub_engine_root(tmp_path)
    payload = b"x" * (_STDIN_MAX_BYTES + 1)

    proc = _run_door(root, timeout=30, hook_mode=True, stdin_payload=payload)

    assert proc.returncode == 0
    body = json.loads(proc.stdout.decode("utf-8").strip())
    reason = body["hookSpecificOutput"]["permissionDecisionReason"]
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "exceeded the bound" in reason


@_WINDOWS_ONLY
def test_a_payload_spanning_multiple_reads_arrives_intact(tmp_path: Path) -> None:
    """(d) A payload comfortably larger than one incremental read chunk
    (`DOOR_STDIN_READ_CHUNK_BYTES`) but under the bound must arrive at the
    server whole and byte-identical -- the drain loop reassembles it
    correctly rather than delivering only its first chunk."""
    root = _make_stub_engine_root(tmp_path)
    payload_len = _STDIN_READ_CHUNK_BYTES * 6 + 37  # deliberately not a multiple
    # Printable ASCII, deliberately -- the wire is a JSON string, and this
    # leg's job is proving the drain loop reassembles multiple chunks
    # correctly, not re-testing `buf_append_json_escaped`'s byte handling
    # (already covered by door_core_selftest.c's unicode-escape check).
    alphabet = b"abcdefghijklmnopqrstuvwxyz0123456789"
    payload = bytes(alphabet[i % len(alphabet)] for i in range(payload_len))
    reply = (
        '{"jsonrpc":"2.0","id":1,"result":'
        '{"stdout":"ok\\n","stderr":"","exit_code":0}}\n'
    ).encode("utf-8")

    server = _ReplyingServer(_pipe_name_for(root), reply)
    try:
        proc = _run_door(root, timeout=60, hook_mode=True, stdin_payload=payload)
    finally:
        server.close()

    request = json.loads(server.request.decode("utf-8").strip())
    received = request["params"]["stdin"].encode("ascii")
    assert len(received) == payload_len
    assert received == payload
    assert proc.returncode == 0


@_WINDOWS_ONLY
def test_hook_mode_fails_closed_on_a_dead_endpoint(tmp_path: Path) -> None:
    """(f) No server is listening (a dead endpoint) and hook mode is
    declared. The door must deny -- affirmatively, in the
    `hookSpecificOutput` shape a PreToolUse hook already knows how to read
    -- rather than falling through to the cold Python entrypoint, which
    would defeat the entire reason a guard uses the door.

    DR-367 is not reversed by this: its own non-license clause already
    excludes a warm server that is reachable and answers no, and a dead
    endpoint is unreachable, not answering-no."""
    root = _make_stub_engine_root(tmp_path)
    payload = b'{"tool_name":"Bash","tool_input":{"command":"echo hi"}}'

    proc = _run_door(root, timeout=30, hook_mode=True, stdin_payload=payload)

    assert proc.returncode == 0
    assert _FALLBACK_MARKER.encode() not in proc.stdout
    body = json.loads(proc.stdout.decode("utf-8").strip())
    assert body == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": body["hookSpecificOutput"][
                "permissionDecisionReason"
            ],
        }
    }
    assert "hook mode" in body["hookSpecificOutput"]["permissionDecisionReason"]
