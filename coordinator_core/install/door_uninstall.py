"""coordinator_core.install.door_uninstall -- reverses `door_install.py`'s
writes: the door binary itself, its build-provenance sidecar, and the
engine-root sidecar `write_sidecar()` drops beside it.

Spec backlink: state/dispatch-briefs/2026-08-22-warm-engine-and-door-install-from-published-root/C9.md

WHY THIS EXISTS. Claude-klabauter ships no uninstall leg for anything it installs.
DoE-claude's `coordinator:uninstall` reverses only coordinator-claude's own
writes from a hand-listed disposition table it owns -- it has no awareness
of claude-klabauter's installs, and getting this module's removal leg named there is
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
(and a rollback-only `.cmd` sibling) that `install_door()`
claimed/shadowed-out at the same bare name -- see
`_reemit_fallback_forwarder`. This is still reversal, not new scope:
`install_door()`'s own docstring documents this as its uninstall
counterpart, and it fires only on a genuine removal (never on an
already-clean or never-installed `bin_dst`).

THE `.cmd` SIBLING, NOT `.ps1` -- resolved 2026-08-29,
docs/plans/2026-08-26-every-forwarder-that-can-reach-the-door-does.md, C12.
Default Windows PATHEXT (`.COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;
.MSC;.CPL`) does not contain `.PS1`, so a `.ps1` fallback here restores a
bare `coordinator-invoke` for PowerShell callers only -- never for
`cmd.exe` or a bare `CreateProcess`, which is exactly the class of caller
`door_install.py`'s Hard Invariant 1 ("a box must never be left with no
`coordinator-invoke` on PATH") must cover. `.CMD` IS in PATHEXT, so the
writer below (`_write_uninstall_fallback_cmd_forwarder`) targets that
extension instead. It is defined in THIS module, not `substrate.py` --
deliberately: it must be reachable from no install path (kept alive solely
for this rollback), and living outside the generator is what makes that
true by construction rather than by convention.

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
from coordinator_core.install.substrate import _write_agent_forwarder
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

#: Distinct, on purpose, from `substrate.py`'s `_AGENT_CMD_FORWARDER_MARKER`
#: (the now-gravestoned install-time `.cmd` writer's marker) and from
#: `_LEGACY_CMD_MARKER`. This file must never be mistaken for either by
#: `_sweep_orphaned_agent_helpers`'s marker-match branch -- an install run
#: has no knowledge this function exists and must never regenerate or sweep
#: what it writes.
_UNINSTALL_FALLBACK_CMD_MARKER = (
    "GENERATED by door_uninstall._write_uninstall_fallback_cmd_forwarder -- "
    "rollback-only, never written by an install run"
)


def _write_uninstall_fallback_cmd_forwarder(name: str, dst: Path, *, target: str) -> None:
    """Rollback-only Windows `.cmd` writer, scoped to `uninstall_door()`'s
    own safety net. Reachable from NO install path -- deliberately not a
    `substrate.py` writer, see this module's docstring for why.

    `.ps1` never discharged `door_install.py`'s Hard Invariant 1 on Windows
    (default PATHEXT carries no `.PS1`), so the fallback this function
    writes uses `.cmd` instead -- `.CMD` IS in default PATHEXT.

    Minimal by design: no baked-interpreter fast path, no `%LOCALAPPDATA%`
    resolution cache -- this is a rollback safety net for the single bare
    name the door just vacated, not the hot per-call path 393 forwarders
    used to pay. `where python.exe` then `py -3`, the same ladder's tail
    rungs, without the caching machinery a hot path needs and this one
    does not.

    `name` is accepted (mirroring every other writer in this family) but
    unused in the body itself -- the emitted body execs `target` relative
    to its own directory (`%~dp0`), never a re-derived stem, matching
    `_write_agent_forwarder`'s own convention for the same reason.
    """
    content = (
        "@echo off\r\n"
        f":: {_UNINSTALL_FALLBACK_CMD_MARKER}\r\n"
        ":: Do not hand-edit. Written once, on door uninstall, to restore a\r\n"
        ":: PATHEXT-resolvable bare-name entrypoint after the door binary\r\n"
        ":: this name was claimed by is removed.\r\n"
        "setlocal\r\n"
        "set \"_here=%~dp0\"\r\n"
        "where python.exe >nul 2>nul\r\n"
        "if not errorlevel 1 (\r\n"
        f"    python.exe \"%_here%{target}\" %*\r\n"
        "    exit /b %errorlevel%\r\n"
        ")\r\n"
        f"py -3 \"%_here%{target}\" %*\r\n"
        "exit /b %errorlevel%\r\n"
    )
    dst.write_text(content, encoding="utf-8", newline="")


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
    (`.py` half, via `install_bin_forwarders`'s own writer
    `_write_agent_forwarder` so this is not a second, drift-prone template
    of the same body) plus a `.cmd` sibling written by THIS module's own
    `_write_uninstall_fallback_cmd_forwarder` -- not the `.ps1` sibling
    `_write_agent_ps1_forwarder` used to write here; see this module's
    docstring, "THE `.cmd` SIBLING, NOT `.ps1`", for why that changed.

    Only fills gaps -- a name already present is left untouched, both
    because overwriting a file this function did not just delete is out of
    this module's stated scope (its own module docstring: "removes exactly
    what install_door() writes ... does NOT touch" anything else) and
    because re-running uninstall against an already-clean `bin_dst` must
    stay a true no-op (`test_uninstall_is_idempotent`).

    The `.cmd` write carries no baked interpreter path -- re-resolving the
    box's actual interpreter here would need the same machinery
    `_install_bin_resolvers` runs at full-install time,
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

    cmd_dst = bin_dst / f"{BARE_FORWARDER_NAME}.cmd"
    if not cmd_dst.exists():
        try:
            _write_uninstall_fallback_cmd_forwarder(BARE_FORWARDER_NAME, cmd_dst, target=_BARE_FORWARDER_TARGET)
            written.append(cmd_dst)
        except OSError as exc:
            print(f"[door-uninstall] could not re-emit fallback forwarder at {cmd_dst}: {exc}", file=sys.stderr)

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
