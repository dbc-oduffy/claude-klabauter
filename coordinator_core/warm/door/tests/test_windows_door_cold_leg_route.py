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

from coordinator_core.benchmarks.tests.test_warm_door_process_time_gate import (
    _resolve_stamped_source_root,
    _candidate_source_roots,
)
from coordinator_core.warm.door import build as door_build
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.warm.tests.test_door_read_deadline import (
    _make_stub_engine_root,
    _pipe_name_for,
    _FALLBACK_MARKER,
    _ReplyingServer,
)
from coordinator_core.warm.door.tests.test_door_stdin_mode import (
    _DOOR_WINDOWS_C,
    _read,
    _WINDOWS_ONLY,
)

import subprocess

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.warm_tier,
]

#: A name C2's table declares as stdin-reading -- picked arbitrarily from
#: `door_stdin_reading_basenames`, not the one this repo happens to test
#: elsewhere, so this file does not silently depend on that other test's
#: choice staying the same.
_DECLARED_BASENAME = "claims-emit"

#: The door's own pre-C0 default -- deliberately NOT a member of C2's
#: table (`door_core_selftest.c` pins this exact fact for
#: `"coordinator-invoke"`), so installing the door under this name is a
#: true undeclared-leg control, not an accident of table membership.
_UNDECLARED_BASENAME = "coordinator-invoke"

_WARM_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":'
    '{"stdout":"served-warm\\n","stderr":"","exit_code":0}}\n'
).encode("utf-8")


_BUILT_DOOR: list = []


def _locally_built_door() -> Path:
    """A door image built from THIS tree's `door.c`, not the committed
    prebuilt.

    The committed `door.exe` is refreshed by C6, which `depends_on` C3 --
    so during C3's own wave the prebuilt still carries the pre-gate build,
    and provisioning these fixtures from it measured the OLD binary. That
    is the stale-artifact polarity the plan's C6 body already names for the
    installed image (`install_door` prefers the committed prebuilt, so an
    un-rebuilt one silently ships the old gate): the same trap reaches the
    tests, where it reads as "the basename gate did not fire".

    Built once per module via `door.build.build`, against the stamped source
    root `test_warm_door_process_time_gate :: _resolve_stamped_source_root`
    resolves -- reused, not re-derived. `build()` refuses a root with no
    `coordinator_core/_engine_stamp` (DR-315 SS2); that refusal is
    inherited here as a NAMED skip, never relaxed."""
    if not _BUILT_DOOR:
        source_root = _resolve_stamped_source_root()
        if source_root is None:
            pytest.skip(
                "no candidate engine root carries a valid build stamp among "
                f"{_candidate_source_roots()!r} -- nothing stamped to build a "
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
    entrypoint that announces itself -- mirroring `_make_stub_engine_root`'s
    own `coordinator-invoke.py`, but keyed to whichever basename this test is
    installing the door under, since `fall_through`'s name-aware cold leg
    (C0) spawns `<engine_root>/coordinator/bin/<basename>.py`.

    See `_locally_built_door` for why this is not `shutil.copy2(_DOOR_EXE)`."""
    installed = engine_root / (basename + ".exe")
    if not installed.exists():
        shutil.copy2(_locally_built_door(), installed)
    script = engine_root / "coordinator" / "bin" / (basename + ".py")
    script.write_text(
        "import sys\n"
        f"print({_FALLBACK_MARKER!r})\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    return installed


def _release_unconnected_server(root: Path) -> None:
    """Unblocks a `_ReplyingServer` that no client ever dialled.

    `_ReplyingServer._serve` sits in `ConnectNamedPipe` until a client
    connects, and its `close()` then calls `CloseHandle` on a handle that
    thread still owns -- which BLOCKS INDEFINITELY when the connection never
    came. Every other user of that fixture has a door that dials, so the
    case never arose; this file's declared-basename leg asserts precisely
    that the door does NOT dial, so it is the first caller that can hang.

    Dialling the pipe once and dropping it immediately lets `ConnectNamedPipe`
    return and the serve thread exit. Nothing is written, so `server.request`
    stays empty and the assertion it feeds keeps its meaning.

    Fixed HERE rather than in `_ReplyingServer` itself: that fixture lives in
    `coordinator_core/warm/tests/`, outside this plan's declared scope and
    shared with suites this plan does not touch."""
    import _winapi

    try:
        handle = _winapi.CreateFile(
            _pipe_name_for(root),
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


# =============================================================================
# Source legs -- cheap, no binary needed.
# =============================================================================


def test_the_wide_predicate_reuses_c2s_shared_table():
    """The wide gate must call C2's `door_basename_declares_stdin_read`, not
    hand-roll a second wcscmp table that could silently diverge from it."""
    source = _read(_DOOR_WINDOWS_C)
    assert "door_basename_declares_stdin_read(" in source, (
        "door.c does not call door_basename_declares_stdin_read -- it has "
        "hand-rolled a second table, or the gate was not added"
    )
    assert "door_basename_declares_stdin_read_w(" in source, (
        "door.c has no wide adapter over the shared predicate -- "
        "door_entrypoint_basename() returns const wchar_t *, which the "
        "char*-typed C2 predicate cannot take directly"
    )


def test_the_basename_gate_follows_resolve_own_basename_and_precedes_the_transport():
    """`door_entrypoint_basename()` is only valid once `resolve_own_basename()`
    has run (door.c's own ordering comment) -- and, like C1's argv gate, this
    one must fire before the pipe is ever dialled, or a gate placed after the
    dial can still produce the indeterminate verdict it exists to prevent."""
    source = _read(_DOOR_WINDOWS_C)
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
    """Hook mode's own stdin is already drained above this gate, and its
    disposition on fall-through is a deny envelope, not a cold spawn --
    the same exclusion C1's argv gate makes, for the same reason."""
    source = _read(_DOOR_WINDOWS_C)
    match = re.search(
        r"door_basename_declares_stdin_read_w\(door_entrypoint_basename\(\)\)",
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
    """staff-eng finding 4: when `g_own_basename_ok == 0`,
    `door_entrypoint_basename()` hands back the pre-C0 default rather than
    the invoked name -- so the call site itself, not the predicate, must
    force the cold leg rather than asking the predicate about the wrong
    name."""
    source = _read(_DOOR_WINDOWS_C)
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
    """The plan body requires the module docstring's own account of routes
    that never go warm to be extended, not left describing only C1's."""
    header = _read(_DOOR_WINDOWS_C)[:6000]
    assert "door_basename_declares_stdin_read_w" in header, (
        "the module docstring was not extended to mention the new "
        "basename-declared stdin gate"
    )


# =============================================================================
# Behavioural legs -- require a compiled door.exe. Windows-only, matching
# this suite's own existing gating.
# =============================================================================


@_WINDOWS_ONLY
def test_a_declared_basename_takes_the_cold_leg_and_never_reaches_the_server(
    tmp_path: Path,
) -> None:
    root = _make_stub_engine_root(tmp_path)
    door = _install_door_as(root, _DECLARED_BASENAME)

    server = _ReplyingServer(_pipe_name_for(root), _WARM_REPLY)
    try:
        proc = _run(door, root)
    finally:
        # The gate firing means nothing dialled this pipe, which is what
        # `server.close()` cannot survive on its own -- see
        # `_release_unconnected_server`.
        _release_unconnected_server(root)
        server.close()

    assert not server.request, (
        f"a door installed as {_DECLARED_BASENAME!r} reached the server -- "
        "the basename gate did not fire"
    )
    assert _FALLBACK_MARKER.encode() in proc.stdout
    assert b"-32603" not in proc.stdout


@_WINDOWS_ONLY
def test_an_undeclared_basename_is_still_served_warm(tmp_path: Path) -> None:
    """The control leg: a gate that always falls through cold is not a
    gate. `coordinator-invoke` -- the door's own pre-C0 default and NOT a
    member of C2's table -- must still reach the server."""
    root = _make_stub_engine_root(tmp_path)
    door = _install_door_as(root, _UNDECLARED_BASENAME)

    server = _ReplyingServer(_pipe_name_for(root), _WARM_REPLY)
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
    assert _FALLBACK_MARKER.encode() not in proc.stdout
