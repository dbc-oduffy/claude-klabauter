"""
coordinator_core.install.migrations.whoami_pin_migration — repoints the
machine-local `coordinator.whoami_python` pin off `.coordinator-venv` and
onto the machine interpreter, for boxes that predate the venv's retirement.

Purpose: `coordinator.whoami_python` (machine-local `registry.local.toml`)
was written unconditionally by `ensure_venv._set_pin` at every successful
venv build/rebuild, and named the venv's own interpreter
(`<settings-home>/.coordinator-venv/Scripts/python.exe` on Windows,
`.../bin/python` on POSIX). It will NOT self-heal as the venv is retired:
nothing else in the install chain ever overwrites this key once written.
This module is the one-time repoint leg that closes that gap — detect the
old value, verify the target interpreter can actually import
`coordinator_whoami`, and only then write it. A box already repointed is a
no-op (idempotent, AC2); a target interpreter that cannot import the
package is a REFUSAL, never a blind repoint (do not repoint blind, per the
chunk brief).

`coordinator.python` (`ensure_venv._PIN_KEY`, read via `_ml_get`, same key
literal) is the resolved machine interpreter this leg repoints onto — the
same key `coordinator.python` already names per the plan's measured state
(`registry.local.toml:8` -> `...\\Python313\\python.exe` on this box,
2026-08-18). This module does not re-derive machine-interpreter resolution
of its own; it reads what the operator/install chain already pinned there.

Verification runs the target interpreter with `cwd` set to a neutral
directory (`tempfile.gettempdir()`), never the repo root — `coordinator_core`
resolves spuriously off this repo's own tree when cwd is the repo root,
which would make the import probe pass for the wrong reason (see the
plan's own "Run this verification from a neutral cwd" instruction, C1).

Negative-spec:
  - Does NOT decide what `coordinator.python` should be. It only reads the
    existing pin at that key; a distinct concern (machine-interpreter
    resolution, `ensure_venv`/`scripts/setup.py`) owns setting it.
  - Does NOT touch `coordinator.python` itself, only `coordinator.whoami_python`.
  - Does NOT run when the machine-local CLI is unavailable — returns
    `"noop-no-cli"` rather than raising; a migration leg is advisory
    infrastructure, not a hard install-time dependency.
  - Does NOT repoint when `coordinator.python` itself still names the venv
    (nothing sane to repoint onto) — refuses loud instead.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from typing import Optional

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

#: Same key literal as `coordinator_core.install.ensure_venv._WHOAMI_PIN_KEY`
#: — not imported from there (a migration leg intentionally does not take a
#: runtime dependency on the module it is migrating boxes away from), but
#: the two spellings must never drift, so this constant carries an explicit
#: cross-reference rather than silently duplicating the literal.
WHOAMI_PIN_KEY = "coordinator.whoami_python"

#: Same key literal as `coordinator_core.install.ensure_venv._PIN_KEY` — see
#: `WHOAMI_PIN_KEY`'s docstring for why this is a parallel literal, not an
#: import.
GENERAL_PIN_KEY = "coordinator.python"

#: Substring test for "does this pin value point inside `.coordinator-venv`"
#: — a plain substring check is deliberately used (not a path-parents walk):
#: both Windows (`...\.coordinator-venv\Scripts\python.exe`) and POSIX
#: (`.../.coordinator-venv/bin/python`) spellings of the pin contain this
#: literal directory name verbatim, backslash or forward slash either way.
_VENV_MARKER = ".coordinator-venv"

#: The package whose importability under the target interpreter this leg
#: verifies before repointing — the same package `coordinator.whoami_python`
#: exists to pin an interpreter for.
_VERIFY_IMPORT = "coordinator_whoami"

# Repoint outcomes this leg can return, each a distinct fact about what
# happened (or didn't) — never a bare bool, since "did nothing because
# already migrated" and "did nothing because there is nothing to repoint
# onto" are different diagnostics a caller/operator needs to tell apart.
REPOINTED = "repointed"
NOOP_ALREADY_MIGRATED = "noop-already-migrated"
NOOP_NO_CLI = "noop-no-cli"
REFUSED_NO_MACHINE_PIN = "refused-no-machine-pin"
REFUSED_TARGET_UNHEALTHY = "refused-target-unhealthy"


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="whoami-pin-migration",
    source_module="coordinator_core.install.migrations.whoami_pin_migration",
    clauses=(
        # The sole surface this leg touches: `WHOAMI_PIN_KEY`, written only
        # on the REPOINTED outcome (`migrate_whoami_pin`'s success leg).
        # `GENERAL_PIN_KEY` is read-only here — this leg never writes it
        # (see module negative-spec).
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="machine-local-key",
                    key=WHOAMI_PIN_KEY,
                    reason=(
                        "migrate_whoami_pin: repoints coordinator.whoami_python off "
                        "the venv onto the value already at coordinator.python, "
                        "once the target interpreter is verified to import "
                        "coordinator_whoami"
                    ),
                ),
            ),
        ),
    ),
)


def _ml_get(ml_cli: list, key: str) -> str:
    """Mirrors `ensure_venv._ml_get` exactly (same quiet-failure contract) —
    a small, deliberate duplication rather than an import, per this module's
    own negative-spec (no runtime dependency on the module being migrated
    away from)."""
    try:
        proc = subprocess.run(
            [*ml_cli, "get", key],
            capture_output=True,
            text=True,
            timeout=15,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"whoami-pin-migration: {ml_cli[0] if ml_cli else '<empty argv>'} get {key} failed: {exc}",
            file=sys.stderr,
        )
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _ml_set(ml_cli: list, key: str, value: str) -> None:
    subprocess.run([*ml_cli, "set", key, value], **no_console_creationflags())


def _target_imports_whoami(target_interpreter: str) -> bool:
    """Verify `_VERIFY_IMPORT` imports under `target_interpreter`, run from
    a NEUTRAL cwd (`tempfile.gettempdir()`) rather than the repo root — see
    module docstring for why the repo root makes this probe pass for the
    wrong reason."""
    try:
        proc = subprocess.run(
            [target_interpreter, "-c", f"import {_VERIFY_IMPORT}"],
            capture_output=True,
            timeout=30,
            cwd=tempfile.gettempdir(),
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def migrate_whoami_pin(ml_cli: Optional[list]) -> str:
    """Idempotent migration leg (AC1/AC2): repoints `coordinator.whoami_python`
    off `.coordinator-venv` onto whatever `coordinator.python` already
    names, verifying the target actually imports `coordinator_whoami` first
    (fail loud, never repoint blind).

    Returns one of the module-level outcome constants above. Never raises
    for an ordinary refusal/no-op path — this is advisory install-chain
    infrastructure, consistent with `ensure_venv._set_pin`'s own
    graceful-degradation contract when the machine-local CLI is absent.
    """
    if ml_cli is None:
        print(
            "whoami-pin-migration: WARNING: machine-local CLI not found; "
            "cannot inspect or repoint coordinator.whoami_python.",
            file=sys.stderr,
        )
        return NOOP_NO_CLI

    current_whoami = _ml_get(ml_cli, WHOAMI_PIN_KEY)
    if not current_whoami or _VENV_MARKER not in current_whoami:
        # Either unset, or already repointed at something outside the venv
        # (this leg's own prior run, or an operator's own choice) — a
        # box already repointed is a no-op (AC2).
        return NOOP_ALREADY_MIGRATED

    target = _ml_get(ml_cli, GENERAL_PIN_KEY)
    if not target or _VENV_MARKER in target:
        # Nothing sane to repoint onto: no machine pin at all, or the
        # machine pin itself still names the venv. Refuse rather than
        # invent a fallback (no fallback escape hatches).
        print(
            f"whoami-pin-migration: REFUSED — {GENERAL_PIN_KEY} does not name "
            "a machine interpreter outside .coordinator-venv; cannot repoint "
            f"{WHOAMI_PIN_KEY}.",
            file=sys.stderr,
        )
        return REFUSED_NO_MACHINE_PIN

    if not _target_imports_whoami(target):
        print(
            f"whoami-pin-migration: REFUSED — target interpreter '{target}' "
            f"does not import {_VERIFY_IMPORT}; {WHOAMI_PIN_KEY} left at "
            f"'{current_whoami}' rather than repointed blind.",
            file=sys.stderr,
        )
        return REFUSED_TARGET_UNHEALTHY

    _ml_set(ml_cli, WHOAMI_PIN_KEY, target)
    print(
        f"whoami-pin-migration: repointed {WHOAMI_PIN_KEY}: "
        f"'{current_whoami}' -> '{target}'"
    )
    return REPOINTED
