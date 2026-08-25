"""coordinator_core.install.door_uninstall -- reverses `door_install.py`'s
writes: the door binary itself, its build-provenance sidecar, and the
engine-root sidecar `write_sidecar()` drops beside it.

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C9.md

WHY THIS EXISTS. makima ships no uninstall leg for anything it installs.
DoE-claude's `coordinator:uninstall` reverses only coordinator-claude's own
writes from a hand-listed disposition table it owns -- it has no awareness
of makima's installs, and getting this module's removal leg named there is
a `coordinator/bin/cross-repo-memo` (raised separately, not from inside
this module). This module is net-new surface, not a hookup to something
that already existed.

SCOPE, HELD DELIBERATELY NARROW. Exactly what `install_door()` writes at
`bin_dst`, per that module's own docstring: the platform-resolved
`DOOR_INSTALLED_NAME` binary, its `<name>.provenance.json` sidecar (copied
alongside the prebuilt, when one was used), and the single shared
`door_build.SIDECAR_FILENAME` (`door.engine-root.txt`) file. It does NOT
touch `engine.warm.enabled` -- that key belongs to `offer_warm_opt_in`'s
opt-in decision, and removing the door binary is not a decision to turn
warmth off for the operator. A caller that wants both runs this module
and the opt-in reversal as two separate, deliberate steps.

One narrow addition past pure removal: when a removal actually fires,
`uninstall_door()` re-emits the plain-Python `coordinator-invoke` forwarder
(and its `.ps1` sibling) that `install_door()` claimed/shadowed-out at the
same bare name -- see `_reemit_fallback_forwarder`. This is still reversal,
not new scope: `install_door()`'s own docstring documents this as its
uninstall counterpart, and it fires only on a genuine removal (never on an
already-clean or never-installed `bin_dst`).

`bin_dst` IS ALWAYS AN EXPLICIT PARAMETER, mirroring `door_install.py`'s
own convention and for the same reason: this module never resolves the
live, ~40-concurrent-session `settings_home()` itself, so it stays
trivially testable against a scratch directory and never becomes a second,
divergent path to the real one.

RUNNABLE, NOT MERELY IMPORTABLE. eng-director review F9 (dispatch brief
C9): AC10 asks that this removal leg be "named where an operator will
find it," and a module discoverable only by grep does not satisfy that.
`python3 -m coordinator_core.install.door_uninstall --bin-dst <path>` is
the real entry point C2/C4/C6's own ADVISORY strings should name where a
removal remediation applies -- not merely an importable function some
future caller has to already know exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.install.door_install import BARE_FORWARDER_NAME, DOOR_INSTALLED_NAME, is_door_installed
from coordinator_core.install.substrate import _write_agent_forwarder, _write_agent_ps1_forwarder
from coordinator_core.warm.door import build as door_build

__all__ = ["uninstall_door"]

#: The on-disk `coordinator/bin/` filename `install_bin_forwarders`
#: derives `BARE_FORWARDER_NAME` from (the `.py` stripped to form the
#: installed name -- `_derive_agent_helper_target_map`'s own convention).
#: `_write_agent_forwarder` needs this as the `target` it execs; it is not
#: re-derived from `BARE_FORWARDER_NAME` because that would silently
#: produce a nonexistent extensionless target if a future rename ever
#: decoupled the two spellings (the exact class of bug that constant's own
#: docstring cites, `.py`-suffix stripping asymmetry).
_BARE_FORWARDER_TARGET = f"{BARE_FORWARDER_NAME}.py"


def _provenance_path(bin_dst: Path) -> Path:
    """Same naming convention `install_door()` writes under: the installed
    exe's own name plus a `.provenance.json` suffix, e.g.
    `coordinator-invoke.exe.provenance.json` on Windows or
    `coordinator-invoke.provenance.json` on POSIX."""
    return bin_dst / (DOOR_INSTALLED_NAME + ".provenance.json")


def _reemit_fallback_forwarder(bin_dst: Path) -> "list[Path]":
    """AC10 safety net -- door_install.py's Hard Invariant 1: a box must
    never be left with no `coordinator-invoke` on PATH.

    `install_door()` claims `BARE_FORWARDER_NAME` for the door binary
    itself (on POSIX this IS `DOOR_INSTALLED_NAME`, the same bare path)
    and deletes the `.ps1` sibling that would otherwise shadow it (see
    `door_install._remove_shadowing_forwarder_siblings`). Removing the door
    without replacing what it claimed would leave that name answering to
    nothing at all -- a regression from the pre-door state, not a neutral
    uninstall. This writes back the same plain-Python forwarder body
    (`.py` half) and `.ps1` sibling `install_bin_forwarders`
    (`coordinator_core.install.substrate`) would have written for
    `coordinator-invoke.py`, using that module's own writers
    (`_write_agent_forwarder`/`_write_agent_ps1_forwarder`) so this is not
    a second, drift-prone template of the same body.

    Only fills gaps -- a name already present is left untouched, both
    because overwriting a file this function did not just delete is out of
    this module's stated scope (its own module docstring: "removes exactly
    what install_door() writes ... does NOT touch" anything else) and
    because re-running uninstall against an already-clean `bin_dst` must
    stay a true no-op (`test_uninstall_is_idempotent`).

    `python3_cmd_resolved_bin=""` for the `.ps1` write: that template's own
    contract (see `_write_agent_ps1_forwarder`'s docstring) treats an empty
    bake as a legal, non-failing input -- it falls through to the
    `where python.exe`/`py -3` runtime ladder rather than a baked fast
    path. Re-resolving the box's actual interpreter here would need the
    same machinery `_install_bin_resolvers` runs at full-install time,
    which this narrow, best-effort safety net deliberately does not carry.

    Best-effort and non-raising, matching every other step in this module:
    a write failure here is reported to stderr, never propagated -- a
    forwarder this function could not restore is a defect to fix, not a
    reason to make `uninstall_door()` itself start raising.
    """
    bin_dst = Path(bin_dst)
    written: "list[Path]" = []

    py_dst = bin_dst / BARE_FORWARDER_NAME
    if not py_dst.exists():
        try:
            _write_agent_forwarder(BARE_FORWARDER_NAME, py_dst, False, target=_BARE_FORWARDER_TARGET)
            written.append(py_dst)
        except OSError as exc:
            print(f"[door-uninstall] could not re-emit fallback forwarder at {py_dst}: {exc}", file=sys.stderr)

    ps1_dst = bin_dst / f"{BARE_FORWARDER_NAME}.ps1"
    if not ps1_dst.exists():
        try:
            _write_agent_ps1_forwarder(BARE_FORWARDER_NAME, ps1_dst, False, python3_cmd_resolved_bin="")
            written.append(ps1_dst)
        except OSError as exc:
            print(f"[door-uninstall] could not re-emit fallback forwarder at {ps1_dst}: {exc}", file=sys.stderr)

    return written


def uninstall_door(bin_dst: Path) -> list[Path]:
    """Removes exactly what `install_door()` writes at `bin_dst`: the
    installed door binary, its provenance sidecar, and the shared
    engine-root sidecar (`door_build.SIDECAR_FILENAME`). Non-raising and
    idempotent -- a path that is already absent is simply skipped, not an
    error, so this is safe to run against a never-installed or
    already-uninstalled `bin_dst`.

    When this call actually removes a live door (gated on
    `is_door_installed(bin_dst)`, checked BEFORE any unlink -- see this
    function's own docstring below for why), it also re-emits the
    plain-Python `coordinator-invoke` forwarder at the same bare name the
    door just vacated -- see `_reemit_fallback_forwarder`. A `bin_dst` that
    never held a door never reaches that step, so it never gets a
    forwarder fabricated into it; that would be this module inventing
    state outside its own stated scope, not reversing a door install.

    Returns the list of paths actually removed (possibly empty) --
    re-emitted paths are NOT included here (a different action from
    "removed"); see `_reemit_fallback_forwarder`'s own return value if a
    caller needs that.

    Gated on `is_door_installed(bin_dst)` UP FRONT, not on a per-candidate
    existence check, and this is load-bearing, not a style choice: on
    POSIX `BARE_FORWARDER_NAME` (the fallback forwarder's own path) and
    `DOOR_INSTALLED_NAME` (the door's path) are the identical bare string.
    A per-candidate check would see the fallback forwarder this same
    function just re-emitted as "a door binary present," unlink it on the
    very next call, and re-emit it again -- non-idempotent, and eventually
    reporting a bogus non-empty `removed` on a `bin_dst` with no door left
    at all. `is_door_installed` requires the engine-root SIDECAR too, which
    only `install_door()` ever writes and this function never re-creates,
    so it stays a reliable "a door is genuinely here" oracle across
    repeated calls.
    """
    bin_dst = Path(bin_dst)
    if not is_door_installed(bin_dst):
        return []

    candidates = [
        bin_dst / DOOR_INSTALLED_NAME,
        _provenance_path(bin_dst),
        bin_dst / door_build.SIDECAR_FILENAME,
    ]
    removed: list[Path] = []
    for path in candidates:
        if path.exists():
            path.unlink()
            removed.append(path)

    for reemitted in _reemit_fallback_forwarder(bin_dst):
        print(f"[door-uninstall] re-emitted fallback forwarder {reemitted}")

    return removed


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove the native warm-engine door installed by "
            "coordinator_core.install.door_install: the door binary, its "
            "build-provenance sidecar, and the engine-root sidecar. NEVER "
            "resolves the real settings-home itself -- pass --bin-dst "
            "explicitly. Does not touch engine.warm.enabled."
        )
    )
    parser.add_argument("--bin-dst", type=Path, required=True, help="Bin directory to uninstall from.")
    args = parser.parse_args(argv)

    removed = uninstall_door(args.bin_dst)
    if not removed:
        print(f"[door-uninstall] nothing to remove at {args.bin_dst}")
        return 0
    for path in removed:
        print(f"[door-uninstall] removed {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
