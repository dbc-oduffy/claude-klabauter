"""Both native doors honour `COORDINATOR_WARM`, the per-invocation escape hatch.

Spec backlink:
state/bug-backlog/2026-09-07-the-compiled-door-neither-reads-nor-forwards-coordinator-warm.yaml

`warm/settings.py`'s own docstring names rung 1 of `is_warm_enabled`'s
precedence "the per-invocation escape hatch": a falsy `COORDINATOR_WARM`
(`0`/`false`/`no`/`off`) ALWAYS wins, so ONE caller can opt ONE invocation
out of warmth. `warm/client.py :: _cli_is_warm_enabled` honours it on the
Python fast-path CLI. Before this fix, neither compiled door
(`door.c`/`door_posix.c`) read the variable at all, so a caller reaching an
installed native door had no way to turn warmth off for a single call --
the documented control was silently inert on the primary installed path.

THIS FILE IS SOURCE-LEVEL, not compiled, matching this directory's own
established convention for both doors
(`test_posix_door_cold_leg_route.py`'s module docstring: "door_posix.c
cannot be compiled or run on this box"; `test_windows_door_cold_leg_route.py`'s
own source-legs section for `door.c`). Structural assertions here are the
same shape those files already use: the gate exists, reads the right name,
precedes the transport dial, excludes hook mode, and falls through rather
than dialling.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOOR_DIR = Path(__file__).resolve().parents[1]
_DOOR_WINDOWS_C = _DOOR_DIR / "door.c"
_DOOR_POSIX_C = _DOOR_DIR / "door_posix.c"

_ENV_NAME = "COORDINATOR_WARM"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# =============================================================================
# door_posix.c
# =============================================================================


def test_posix_door_reads_the_escape_hatch_env_var():
    """The exact literal `COORDINATOR_WARM` must appear in `door_posix.c`,
    read through the shared `door_env_warm_is_falsy` predicate -- otherwise
    the escape hatch is not read at all on this leg."""
    source = _read(_DOOR_POSIX_C)
    assert f'"{_ENV_NAME}"' in source, (
        "door_posix.c does not reference the literal COORDINATOR_WARM -- "
        "the escape hatch is not read at all"
    )
    assert "door_env_warm_is_falsy" in source


def test_posix_door_gate_precedes_engine_root_resolution_and_the_transport():
    """A falsy `COORDINATOR_WARM` must short-circuit before both
    engine-root resolution and the socket dial -- consulting it any later
    still lets the request reach a warm worker."""
    source = _read(_DOOR_POSIX_C)
    main_at = source.index("int main")
    gate_at = source.index("door_env_warm_is_falsy()", main_at)
    engine_root_at = source.index("resolve_engine_root(&engine_root", main_at)
    connect_at = source.index("connect_socket(sock_path)", main_at)
    assert gate_at < engine_root_at, (
        "door_posix.c consults the COORDINATOR_WARM escape hatch only after "
        "engine-root resolution -- a falsy value should short-circuit before "
        "that work happens, not merely before the socket dial"
    )
    assert gate_at < connect_at, (
        "door_posix.c consults the COORDINATOR_WARM escape hatch only after "
        "dialling the socket -- the request can still be delivered warm"
    )


def test_posix_door_gate_excludes_hook_mode():
    """The gate must be conditioned on `!g_door_hook_mode` -- a hook-mode
    invocation's fall-through is a deny envelope, not a cold spawn, so the
    escape hatch must not fire there."""
    source = _read(_DOOR_POSIX_C)
    match = re.search(r"if\s*\([^)]*door_env_warm_is_falsy\(\)\s*\)", source)
    assert match, "the escape-hatch gate call site was not found verbatim"
    assert "!g_door_hook_mode" in match.group(0), (
        "the escape-hatch gate is not conditioned on !g_door_hook_mode -- it "
        "would fire on a hook-mode invocation, whose fall-through must be a "
        "deny envelope, not a cold spawn"
    )


def test_posix_door_gate_falls_through_without_the_resolved_engine_root():
    """A falsy `COORDINATOR_WARM` must short-circuit BEFORE engine-root
    resolution runs -- passing `NULL`, matching every other pre-resolution
    fall-through in this file (e.g. `!resolve_engine_root`'s own gate), not
    the resolved `engine_root` a later gate would carry."""
    source = _read(_DOOR_POSIX_C)
    main_at = source.index("int main")
    gate_at = source.index("door_env_warm_is_falsy()", main_at)
    body_start = source.index("{", gate_at)
    body_end = source.index("}", body_start)
    body = source[body_start:body_end]
    assert "fall_through(argc, argv, NULL)" in body


# =============================================================================
# door.c
# =============================================================================


def test_windows_door_reads_the_escape_hatch_env_var():
    """The exact wide literal `L"COORDINATOR_WARM"` must appear in
    `door.c`, read through the shared `door_env_warm_is_falsy` predicate --
    otherwise the escape hatch is not read at all on this leg."""
    source = _read(_DOOR_WINDOWS_C)
    assert f'L"{_ENV_NAME}"' in source, (
        "door.c does not reference the wide literal COORDINATOR_WARM -- the "
        "escape hatch is not read at all"
    )
    assert "door_env_warm_is_falsy" in source


def test_windows_door_gate_precedes_engine_root_resolution_and_the_transport():
    """A falsy `COORDINATOR_WARM` must short-circuit before both
    engine-root resolution and the named-pipe dial -- consulting it any
    later still lets the request reach a warm worker."""
    source = _read(_DOOR_WINDOWS_C)
    main_at = source.index("int main")
    gate_at = source.index("door_env_warm_is_falsy()", main_at)
    engine_root_at = source.index("resolve_engine_root(&engine_root_w", main_at)
    connect_at = source.index("CreateFileW(pipe_name", main_at)
    assert gate_at < engine_root_at, (
        "door.c consults the COORDINATOR_WARM escape hatch only after "
        "engine-root resolution -- a falsy value should short-circuit before "
        "that work happens, not merely before the pipe dial"
    )
    assert gate_at < connect_at, (
        "door.c consults the COORDINATOR_WARM escape hatch only after "
        "dialling the pipe -- the request can still be delivered warm"
    )


def test_windows_door_gate_excludes_hook_mode():
    """The gate must be conditioned on `!g_door_hook_mode` -- a hook-mode
    invocation's fall-through is a deny envelope, not a cold spawn, so the
    escape hatch must not fire there."""
    source = _read(_DOOR_WINDOWS_C)
    match = re.search(r"if\s*\([^)]*door_env_warm_is_falsy\(\)\s*\)", source)
    assert match, "the escape-hatch gate call site was not found verbatim"
    assert "!g_door_hook_mode" in match.group(0), (
        "the escape-hatch gate is not conditioned on !g_door_hook_mode -- it "
        "would fire on a hook-mode invocation, whose fall-through must be a "
        "deny envelope, not a cold spawn"
    )


def test_windows_door_gate_falls_through_without_the_resolved_engine_root():
    """A falsy `COORDINATOR_WARM` must short-circuit BEFORE engine-root
    resolution runs -- passing `NULL`, matching every other pre-resolution
    fall-through in this file, not the resolved `engine_root_w` a later
    gate would carry."""
    source = _read(_DOOR_WINDOWS_C)
    main_at = source.index("int main")
    gate_at = source.index("door_env_warm_is_falsy()", main_at)
    body_start = source.index("{", gate_at)
    body_end = source.index("}", body_start)
    body = source[body_start:body_end]
    assert "fall_through_and_free(argc, wargv, NULL)" in body
