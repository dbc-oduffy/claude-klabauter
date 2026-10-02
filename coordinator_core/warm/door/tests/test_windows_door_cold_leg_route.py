"""A door installed under a stdin-reading basename never serves warm.

Spec: docs/plans/2026-09-12-warm-door-drops-stdin-for-every-entrypoint.md § C3,
resting on C2's shared table (`door_core.c :: door_basename_declares_stdin_read`).

WHY THIS GATE EXISTS, and why it is a SEPARATE gate from C1's
`--params-file -` one (`test_params_file_stdin_route.py`): that gate reads
the caller's own argv, which only ever covers `coordinator-invoke`'s own
`--params-file` flag. Every OTHER entrypoint this door fronts
(`claims-emit`, `queue-triage`, ...) reads its payload from stdin with no
argv declaration at all -- its basename IS the declaration, and only the
INVOKED name (`door_entrypoint_basename()`, C0) can name it. A warm pool
worker's `sys.stdin` is `None`
(`warm/server.py :: _suppress_pool_worker_consoles`), so serving one of
these names warm reproduces the same indeterminate-`-32603` shape C1 fixed
for the argv case, for every basename C2's table names.

DISCRIMINATION IS THE POINT, same shape as `test_params_file_stdin_route.py`:
a gate that always falls through cold would pass a fixture that only checks
the declared-basename leg, and a gate that never fires would pass one that
only checks the undeclared leg. Both legs share one server and one fixture
family, in the same function, so a "gate" that always/never fires fails
whichever leg it did not mean to take.
"""

from __future__ import annotations

import json
import atexit
import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.tests.door_test_support import (
    DOOR_WINDOWS_C,
    FALLBACK_MARKER,
    ReplyingServer,
    WINDOWS_ONLY,
    candidate_source_roots,
    make_stub_engine_root,
    pipe_name_for,
    read,
    resolve_stamped_source_root,
)
from coordinator_core.win_portability import no_console_creationflags

import subprocess

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.warm_tier,
]

_DECLARED_BASENAME = "claims-emit"

_UNDECLARED_BASENAME = "coordinator-invoke"

_WARM_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":'
    '{"stdout":"served-warm\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


_BUILT_DOOR: list = []


def _locally_built_door() -> Path:
    if not _BUILT_DOOR:
        source_root = resolve_stamped_source_root()
        if source_root is None:
            pytest.skip(
                "no candidate engine root carries a valid build stamp among "
                f"{candidate_source_roots()!r} -- nothing stamped to build a "
                "gate-carrying door.exe from, and the committed prebuilt is "
                "pre-C6 and therefore pre-gate"
            )
        tmpdir = tempfile.TemporaryDirectory(prefix="cold-leg-route-door-")
        atexit.register(tmpdir.cleanup)
        out = Path(tmpdir.name) / "door.exe"
        _BUILT_DOOR.append(door_build.build(source_root, output=out))
    return _BUILT_DOOR[0]


def _install_door_as(engine_root: Path, basename: str) -> Path:
    """Installs a LOCALLY-BUILT `door.exe` into `engine_root` under
    `basename`, and gives it a matching `coordinator/bin/<basename>.py` cold
    entrypoint that announces itself -- mirroring `make_stub_engine_root`'s
    own `coordinator-invoke.py`, but keyed to whichever basename this test is
    installing the door under, since `fall_through`'s name-aware cold leg
    (C0) spawns `<engine_root>/coordinator/bin/<basename>.py`.

    See `_locally_built_door` for why this is not `shutil.copy2(DOOR_EXE)`."""
    installed = engine_root / (basename + ".exe")
    if not installed.exists():
        shutil.copy2(_locally_built_door(), installed)
    script = engine_root / "coordinator" / "bin" / (basename + ".py")
    script.write_text(
        "import sys\n"
        f"print({FALLBACK_MARKER!r})\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return installed


def _release_unconnected_server(root: Path) -> None:
    """Unblocks a `ReplyingServer` that no client ever dialled.

    `ReplyingServer._serve` sits in `ConnectNamedPipe` until a client
    connects, and its `close()` then calls `CloseHandle` on a handle that
    thread still owns -- which BLOCKS INDEFINITELY when the connection never
    came. Every other user of that fixture has a door that dials, so the
    case never arose; this file's declared-basename leg asserts precisely
    that the door does NOT dial, so it is the first caller that can hang.

    Dialling the pipe once and dropping it immediately lets `ConnectNamedPipe`
    return and the serve thread exit. Nothing is written, so `server.request`
    stays empty and the assertion it feeds keeps its meaning.

    Fixed HERE rather than in `ReplyingServer` itself: that fixture lives in
    `coordinator_core/warm/tests/`, outside this plan's declared scope and
    shared with suites this plan does not touch."""
    import _winapi

    try:
        handle = _winapi.CreateFile(
            pipe_name_for(root),
            _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
            0,
            0,
            _winapi.OPEN_EXISTING,
            0,
            0,
        )
    except OSError:
        return
    try:
        _winapi.CloseHandle(handle)
    except OSError as exc:
        print(f"_release_unconnected_server: CloseHandle failed: {exc!r}")


def _run(door: Path, root: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env.pop("COORDINATOR_DOOR_STDIN_MODE", None)
    return subprocess.run(
        [str(door), "ping"],
        input=b"",
        capture_output=True,
        env=env,
        timeout=60,
        cwd=str(root),
        **no_console_creationflags(),
    )


def test_the_wide_predicate_reuses_c2s_shared_table():
    source = read(DOOR_WINDOWS_C)
    assert "door_invocation_declares_stdin_read(" in source, (
        "door.c does not call door_invocation_declares_stdin_read -- it has "
        "hand-rolled a second table, or the gate was not added"
    )
    core = read(DOOR_WINDOWS_C.with_name("door_core.c"))
    assert re.search(
        r"door_invocation_declares_stdin_read\([^)]*\)\s*\{\s*"
        r"if\s*\(\s*!door_basename_declares_stdin_read\(",
        core,
    ), (
        "door_invocation_declares_stdin_read no longer narrows C2's shared "
        "door_basename_declares_stdin_read table"
    )
    assert "door_basename_declares_stdin_read_w(" in source, (
        "door.c has no wide adapter over the shared predicate -- "
        "door_entrypoint_basename() returns const wchar_t *, which the "
        "char*-typed C2 predicate cannot take directly"
    )


def test_the_basename_gate_follows_resolve_own_basename_and_precedes_the_transport():
    source = read(DOOR_WINDOWS_C)
    main_at = source.index("int main")
    resolve_at = source.index("resolve_own_basename();", main_at)
    gate_at = source.index("door_basename_declares_stdin_read_w(", main_at)
    connect_at = source.index("CreateFileW(pipe_name", main_at)
    assert resolve_at < gate_at, (
        "the basename gate is reached before resolve_own_basename() runs -- "
        "door_entrypoint_basename() is not yet valid there"
    )
    assert gate_at < connect_at, (
        "the basename gate is consulted only after dialling the pipe -- the "
        "request can still be delivered"
    )


def test_the_gate_is_excluded_from_hook_mode():
    source = read(DOOR_WINDOWS_C)
    match = re.search(
        r"door_basename_declares_stdin_read_w\(\s*door_entrypoint_basename\(\)\s*,"
        r"\s*argc\s*,\s*wargv\s*\)",
        source,
    )
    assert match, "the basename gate call site was not found verbatim"
    preceding = source[max(0, match.start() - 200):match.start()]
    assert "g_door_hook_mode" in preceding, (
        "the basename gate is not conditioned on !g_door_hook_mode -- it "
        "would fire on a hook-mode invocation whose fall-through must be a "
        "deny envelope, not a cold spawn"
    )


def test_an_unresolved_basename_takes_the_cold_leg_unconditionally():
    source = read(DOOR_WINDOWS_C)
    match = re.search(
        r"if\s*\(\s*!g_door_hook_mode\s*&&\s*\(\s*!g_own_basename_ok\s*\|\|"
        r"\s*door_basename_declares_stdin_read_w\(",
        source,
    )
    assert match, (
        "the basename gate does not force the cold leg when "
        "!g_own_basename_ok -- an unresolved image name would be asked "
        "about DOOR_DEFAULT_ENTRYPOINT_W instead of the invoked name"
    )


def test_module_docstring_names_the_new_route():
    header = read(DOOR_WINDOWS_C)[:6000]
    assert "door_basename_declares_stdin_read_w" in header, (
        "the module docstring was not extended to mention the new "
        "basename-declared stdin gate"
    )


@WINDOWS_ONLY
def test_a_declared_basename_takes_the_cold_leg_and_never_reaches_the_server(
    tmp_path: Path,
) -> None:
    root = make_stub_engine_root(tmp_path)
    door = _install_door_as(root, _DECLARED_BASENAME)

    server = ReplyingServer(pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(door, root)
    finally:
        _release_unconnected_server(root)
        server.close()

    assert not server.request, (
        f"a door installed as {_DECLARED_BASENAME!r} reached the server -- "
        "the basename gate did not fire"
    )
    assert FALLBACK_MARKER.encode() in proc.stdout
    assert b"-32603" not in proc.stdout


@WINDOWS_ONLY
def test_an_undeclared_basename_is_still_served_warm(tmp_path: Path) -> None:
    root = make_stub_engine_root(tmp_path)
    door = _install_door_as(root, _UNDECLARED_BASENAME)

    server = ReplyingServer(pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(door, root)
    finally:
        server.close()

    assert server.request, (
        f"a door installed as {_UNDECLARED_BASENAME!r} (undeclared in C2's "
        "table) did not reach the server -- the basename gate fires "
        "unconditionally"
    )
    request = json.loads(server.request.decode("utf-8").strip())
    assert request["params"]["argv"] == ["ping"]
    assert proc.stdout == b"served-warm\n"
    assert FALLBACK_MARKER.encode() not in proc.stdout


def test_the_fall_through_line_names_the_jsonrpc_code_the_server_rejected_with():
    source = read(DOOR_WINDOWS_C)
    assert "g_fall_code = error_code;" in source, (
        "the post-delivery fall-through no longer records the JSON-RPC code "
        "that proved the request undispatched"
    )
    assert re.search(
        r"server rejected the request, JSON-RPC code %ld\]\\n\",\s*"
        r"script_path_w,\s*g_fall_code",
        source,
    ), (
        "the fall-through line does not print the JSON-RPC code -- the cause "
        "of a cold start cannot be read off the symptom"
    )
    assert "g_fall_reason" in source, (
        "pre-delivery fall-throughs no longer name the gate that fired"
    )
