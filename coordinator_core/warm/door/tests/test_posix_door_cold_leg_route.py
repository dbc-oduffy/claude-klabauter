"""The POSIX door's stdin-reading-entrypoint gate: source-level, not compiled.

Spec backlink: docs/plans/2026-09-12-warm-door-drops-stdin-for-every-entrypoint.md
§ C4 (mirrors § C3's Windows twin, `door.c`'s own
`door_basename_declares_stdin_read` gate).

WHY THIS FILE NEVER SPAWNS A PROCESS. `door_posix.c` cannot be compiled or
run on this box (Windows) -- see `test_params_file_stdin_route.py`'s own
module docstring: "The source legs cover the half no Windows binary can
answer." Both directions the plan's exit criterion asks for -- a declared
basename takes the cold leg, an undeclared one still goes warm -- are
asserted structurally against the source text instead: the gate's condition,
its placement relative to the transport dial, and the fact that the function
body continues past the gate into the warm path rather than falling through
unconditionally.

THE FAIL-DIRECTION ASYMMETRY THIS FILE PINS. The argv-shaped gate at 0a
degrades WARM when its own precondition (a renderable argv) fails; this gate
degrades COLD when ITS precondition (a resolved own-basename) fails -- the
opposite direction, for the reason `door_posix.c`'s own comment gives: an
unresolved basename means the table is being asked about the wrong name
entirely, not that the argv gate's failure mode of "this argv could not be
rendered" applies. A test asserting only the declared/undeclared split and
missing this asymmetry would pass a "fix" that quietly asks the table about
`DOOR_DEFAULT_ENTRYPOINT` on every `resolve_own_basename()` failure.
"""

from __future__ import annotations

import re
from pathlib import Path

_DOOR_DIR = Path(__file__).resolve().parents[1]
_DOOR_POSIX_C = _DOOR_DIR / "door_posix.c"
_DOOR_CORE_H = _DOOR_DIR / "door_core.h"
_DOOR_CORE_C = _DOOR_DIR / "door_core.c"

_PREDICATE = "door_basename_declares_stdin_read"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_predicate_is_shared_not_reimplemented():
    """The table lives once, in `door_core.c`/`door_core.h`; `door_posix.c`
    must call it rather than hand-rolling a second name list that could
    silently diverge from `door.c`'s."""
    assert f"int {_PREDICATE}(" in _read(_DOOR_CORE_H)
    assert f"int {_PREDICATE}(" in _read(_DOOR_CORE_C)
    source = _read(_DOOR_POSIX_C)
    assert _PREDICATE in source, (
        "door_posix.c does not consult door_basename_declares_stdin_read -- "
        "it will still dispatch a stdin-reading entrypoint warm into a pool "
        "worker whose stdin is None"
    )


def test_the_gate_precedes_the_transport():
    """Pre-delivery is the whole property: a gate placed after the socket
    dial can still hand the request to a live server before falling
    through."""
    source = _read(_DOOR_POSIX_C)
    main_at = source.index("int main")
    gate_at = source.index(_PREDICATE + "(", main_at)
    connect_at = source.index("connect_socket(sock_path)", main_at)
    assert gate_at < connect_at, (
        "door_posix.c consults door_basename_declares_stdin_read only after "
        "dialling the socket -- the request can still be delivered warm"
    )


def test_the_gate_excludes_hook_mode():
    """Hook mode's stdin is already drained above this gate, and its own
    fall-through disposition is a deny envelope, not a cold spawn -- the
    same exclusion the argv-shaped 0a gate applies."""
    source = _read(_DOOR_POSIX_C)
    gate_at = source.index(_PREDICATE + "(")
    # The nearest enclosing `if` above the call must condition on
    # `!g_door_hook_mode`, matching the 0a gate's own wiring.
    if_at = source.rindex("if (", 0, gate_at)
    condition = source[if_at:gate_at]
    assert "!g_door_hook_mode" in condition


def test_the_fail_direction_is_cold_on_unresolved_basename():
    """`g_own_basename_ok == 0` means this image's own name could not be
    resolved, so `door_entrypoint_basename()` would answer for the pre-C0
    default rather than the name actually invoked -- asking the table about
    the wrong name is unsafe, so this must take the COLD leg
    UNCONDITIONALLY, the opposite fail direction from the argv gate's own
    (which stays warm on its own precondition failure). Asserted as an
    `||` disjunct with the declared-name predicate, both inside the SAME
    `if` that reaches `fall_through`, so neither condition alone decides."""
    source = _read(_DOOR_POSIX_C)
    gate_at = source.index(_PREDICATE + "(")
    if_at = source.rindex("if (", 0, gate_at)
    # The `if` header runs until the line ending in `{`.
    header_end = source.index("{", gate_at)
    header = source[if_at:header_end]
    assert "!g_own_basename_ok" in header, (
        "the gate does not fall through cold when this image's own basename "
        "could not be resolved -- door_entrypoint_basename() would then "
        "answer for the wrong name and the table would be asked about it"
    )
    assert "||" in header, (
        "the unresolved-basename check and the declared-name predicate must "
        "be OR'd together in one gate, not two separate ifs"
    )

    body_start = header_end
    body_end = source.index("}", body_start)
    body = source[body_start:body_end]
    assert "fall_through(argc, argv, engine_root)" in body


def test_an_undeclared_name_falls_through_the_gate_and_stays_reachable():
    """A gate that always takes the cold leg is not a gate. Structurally:
    the function must continue past this `if` block into the warm-path
    steps (socket derivation, `connect_socket`) rather than returning
    unconditionally -- so a basename NOT in the table (and a resolved own
    basename) reaches the transport dial instead of a guaranteed
    fall-through."""
    source = _read(_DOOR_POSIX_C)
    gate_at = source.index(_PREDICATE + "(")
    body_start = source.index("{", gate_at)
    body_end = source.index("}", body_start)
    after_gate = source[body_end + 1 :]
    # Warm-path steps 1-5 (identity/token/clone-hash/socket/connect) still
    # follow the gate in source order -- the gate is not the last statement
    # in `main` before an unconditional return.
    assert "connect_socket(sock_path)" in after_gate
