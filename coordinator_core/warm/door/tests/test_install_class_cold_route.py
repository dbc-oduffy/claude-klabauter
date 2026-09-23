"""Both native doors gate install-class basenames to the cold leg, never the engine.

Spec backlink: dispatch brief, item 2 (cold-only gate for install-class
names), 2026-09-23 -- PM ruling: "install shouldn't route via the warm
engine, because it installs."

THIS FILE IS SOURCE-LEVEL, not compiled, matching this directory's own
established convention for a pre-resolution fall-through gate
(`test_warm_escape_hatch_gate.py`'s own module docstring: the gate is
asserted structurally -- exists, reads the right predicate, precedes engine-
root resolution and the transport dial, falls through with no resolved
engine root -- exactly like `COORDINATOR_WARM`'s own twin gate, which this
file mirrors call-for-call). A compiled behavioural leg is deliberately not
attempted here: a pre-resolution fall-through passes `NULL` for the engine
root, so `fall_through` falls back to this binary's BAKED `BUILD_ENGINE_ROOT_W`
default rather than any stub root a test could point it at (see
`build_fallback_cmdline`'s own comment) -- against a real published engine on
this box, spawning that cold leg would run the REAL install-class CLI, not a
stub. `door_maybe_spawn_server`/`connect_socket`/`CreateFileW(pipe_name` never
appearing before the gate's `fall_through` call is the behaviour these tests
pin instead: proof the engine is never dialled for these names, without ever
risking a real install side effect.
"""

from __future__ import annotations

from pathlib import Path

_DOOR_DIR = Path(__file__).resolve().parents[1]
_DOOR_WINDOWS_C = _DOOR_DIR / "door.c"
_DOOR_POSIX_C = _DOOR_DIR / "door_posix.c"

_PREDICATE_W = "door_basename_is_install_class_w"
_PREDICATE_POSIX = "door_basename_is_install_class"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# =============================================================================
# door_posix.c
# =============================================================================


def test_posix_door_consults_the_shared_install_class_predicate():
    source = _read(_DOOR_POSIX_C)
    assert _PREDICATE_POSIX in source, (
        "door_posix.c does not consult door_basename_is_install_class -- an "
        "install-class CLI would still be dialled to the warm engine it is "
        "meant to replace"
    )


def test_posix_door_gate_precedes_engine_root_resolution_and_the_transport():
    source = _read(_DOOR_POSIX_C)
    main_at = source.index("int main")
    gate_at = source.index(_PREDICATE_POSIX + "(", main_at)
    engine_root_at = source.index("resolve_engine_root(&engine_root", main_at)
    connect_at = source.index("connect_socket(sock_path)", main_at)
    spawn_at = source.index("door_maybe_spawn_server(engine_root, chosen_dir", main_at)
    assert gate_at < engine_root_at, (
        "door_posix.c consults the install-class gate only after engine-root "
        "resolution -- it should short-circuit before that work happens"
    )
    assert gate_at < connect_at, (
        "door_posix.c consults the install-class gate only after dialling "
        "the socket -- the request can still be delivered warm"
    )
    assert gate_at < spawn_at, (
        "door_posix.c consults the install-class gate only after "
        "door_maybe_spawn_server -- an install-class CLI could still spawn "
        "the warm server it is meant to replace"
    )


def test_posix_door_gate_falls_through_without_the_resolved_engine_root():
    source = _read(_DOOR_POSIX_C)
    main_at = source.index("int main")
    gate_at = source.index(_PREDICATE_POSIX + "(", main_at)
    if_at = source.rindex("if (", 0, gate_at)
    body_start = source.index("{", gate_at)
    body_end = source.index("}", body_start)
    header = source[if_at:body_start]
    body = source[body_start:body_end]
    assert "!g_own_basename_ok" in header
    assert "||" in header
    assert "fall_through(argc, argv, NULL)" in body


# =============================================================================
# door.c
# =============================================================================


def test_windows_door_consults_the_shared_install_class_predicate():
    source = _read(_DOOR_WINDOWS_C)
    assert f"static int {_PREDICATE_W}(" in source, (
        "door.c does not define the wide install-class adapter over "
        "door_basename_is_install_class"
    )
    assert _PREDICATE_POSIX in source


def test_windows_door_gate_precedes_engine_root_resolution_and_the_transport():
    source = _read(_DOOR_WINDOWS_C)
    main_at = source.index("int main")
    gate_at = source.index(_PREDICATE_W + "(", main_at)
    engine_root_at = source.index("resolve_engine_root(&engine_root_w", main_at)
    connect_at = source.index("CreateFileW(pipe_name", main_at)
    spawn_at = source.index("door_maybe_spawn_server(engine_root_w, clone_hash", main_at)
    assert gate_at < engine_root_at, (
        "door.c consults the install-class gate only after engine-root "
        "resolution -- it should short-circuit before that work happens"
    )
    assert gate_at < connect_at, (
        "door.c consults the install-class gate only after dialling the "
        "pipe -- the request can still be delivered warm"
    )
    assert gate_at < spawn_at, (
        "door.c consults the install-class gate only after "
        "door_maybe_spawn_server -- an install-class CLI could still spawn "
        "the warm server it is meant to replace"
    )


def test_windows_door_gate_falls_through_without_the_resolved_engine_root():
    source = _read(_DOOR_WINDOWS_C)
    main_at = source.index("int main")
    gate_at = source.index(_PREDICATE_W + "(", main_at)
    if_at = source.rindex("if (", 0, gate_at)
    body_start = source.index("{", gate_at)
    body_end = source.index("}", body_start)
    header = source[if_at:body_start]
    body = source[body_start:body_end]
    assert "!g_own_basename_ok" in header
    assert "||" in header
    assert "fall_through_and_free(argc, wargv, NULL)" in body


def test_gate_runs_before_hook_mode_logic_on_both_doors():
    """The gate itself is not conditioned on `g_door_hook_mode` -- it fires
    unconditionally, immediately after `resolve_own_basename()`, which is
    what keeps an install-class name out of hook mode's own logic too (the
    dispatch brief's third disjunct), not merely out of the warm dial."""
    for source_path in (_DOOR_WINDOWS_C, _DOOR_POSIX_C):
        source = _read(source_path)
        own_basename_at = source.index("resolve_own_basename();")
        predicate = _PREDICATE_W if source_path is _DOOR_WINDOWS_C else _PREDICATE_POSIX
        gate_at = source.index(predicate + "(", own_basename_at)
        between = source[own_basename_at:gate_at]
        assert "g_door_hook_mode" not in between, (
            f"{source_path.name} inspects g_door_hook_mode before the "
            "install-class gate runs -- the gate must fire unconditionally, "
            "right after the basename resolves"
        )
