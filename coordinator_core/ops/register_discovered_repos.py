"""
coordinator_core.ops.register_discovered_repos — DOE-PORT, variant #1 — direct-import
trampoline, no registered op.

Purpose: bridges `discover-working-repos.sh`'s tier-gated discovery output into the
machine-local `repos.*` registry that cross-repo addressing (cross-repo-memo
--list-receivers, sibling-path resolution) depends on. Closes F16: coordinator:install
Step 4 ran three-tier discovery but never registered any discovered repo into
`repos.*`, leaving a fresh-machine operator unable to address any fleet sibling by
name until manually running `machine-local set repos.<name> <path>`.

Design (unchanged from the bash oracle):
  - Tier-gated: repos come ONLY from discover-working-repos.sh's own three-tier
    qualification (activity record / registry / dev-folder probe). This module does
    NOT walk the filesystem itself — a stray git clone that discover-working-repos.sh
    doesn't already surface is never registered. Over-registration is bounded by
    that upstream script.
  - Only-if-absent: never clobbers an existing repos.<key> value. A prior manual
    registration (possibly pointing elsewhere on purpose) always wins. Re-runs are
    no-ops for anything already registered.
  - Key derivation matches cross-repo-memo's `_receiver_repo_key` resolver: lowercase
    basename, non-alnum runs collapsed to a single `_`, leading/trailing `_` stripped.

Modes:
  (default)          interactive when stdin is a tty: prints the list of not-yet-
                      registered repos, asks Y/n, registers on accept (or on any
                      non-interactive/non-tty context).
  --non-interactive   silent absent-only seed, no prompt.
  --check-only        report what WOULD register; writes nothing.

Negative-spec: this module does not read or write ~/.claude/working-repos.yaml (that
manifest is a separate concern — see install.md Step 4). It only reads
coordinator_core.ops.discover_working_repos's discovery output and the machine-local
registry.

Negative-spec: does NOT reimplement discover-working-repos.sh's tier-gating logic —
calls the canonical native port (coordinator_core.ops.discover_working_repos.main, an
in-process import, not a subprocess) so there is exactly one implementation of the
tier-gating logic. Retired the `bash discover-working-repos.sh` subprocess bridge
(2026-07-21, sh_argv-class retirement, docs/plans/2026-07-21-claude-klabauter-pure-python-shop-
retire-all-bash.md C18): the example-doctrine-repo-side `.sh` this used to shell out to is itself only a
polyglot trampoline back onto this exact same claude-klabauter module (see that file's own
header), so the subprocess hop was pure indirection with no logic on the other end to
preserve. Still shells out to the machine-local CLI (PATH/sibling-dir resolved) — a
real separate binary, not a claude-klabauter module.

Never-block contract: like the bash oracle, almost every failure mode (discovery
error, machine-local missing, no candidates, operator declines) is a silent skip that
exits 0 — this bridge is advisory best-effort registration during install, never a
gate. The one exception (preserved from the oracle) is an unrecognized CLI flag,
which exits 1 with a usage message.

Spec backlink: F16 (install discovers working repos but never registers them into the
machine-local repos.* registry).
Port of: register-discovered-repos.sh (example-doctrine-repo b644d5a9, 2026-07-22)
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from coordinator_core import launchable
from coordinator_core._settings_home import home_dir
from coordinator_core.install.resolution_journal import record_resolution
from coordinator_core.install.write_surface import (
    ShapedClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.ops.discover_working_repos import main as _discover_working_repos_main
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs

_PROG = "register-discovered-repos.sh"  # literal program-name prefix — see negative-spec

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="register-discovered-repos",
    source_module="coordinator_core.ops.register_discovered_repos",
    clauses=(
        ShapedClause(
            discovered_by="discover_working_repos",
            entry_template=WriteSurfaceEntry(
                kind="machine-local-key",
                key="repos.<derived-key>",
            ),
        ),
    ),
)
"""This writer's surface is not knowable in source — it depends on which
not-yet-registered repos `discover_working_repos` (tier-gated activity
record / registry / dev-folder probe, never a filesystem walk of its own)
finds on the machine running install. `<derived-key>` is `_derive_key`'s
lowercase, non-alnum-collapsed basename. Only-if-absent: a key already
present in `repos.*` is never clobbered, so this clause's runtime set is a
subset of "discovered minus already-registered," not "discovered."
"""


_SHAPED_CLAUSE_INDEX = 0
"""Index of `WRITE_SURFACE`'s sole `ShapedClause` within its `clauses`
tuple — the index `record_resolution`/`derive_receipt_entries` key
resolutions by. Kept as a named constant (not a bare `0` at each call
site) so a future second clause on this declaration cannot silently
misroute a call site that forgot to update it."""


def _journal_registered(keys: Sequence[str]) -> None:
    """Record what this clause actually resolved to on THIS run: the
    `repos.<key>` machine-local entries that were genuinely written (or,
    for a definitively-empty resolution, none at all — see module-level
    negative spec in `main`'s call sites). Never called for a path where
    registration was not actually attempted/determined this run."""
    entries = tuple(
        WriteSurfaceEntry(kind="machine-local-key", key=f"repos.{key}") for key in keys
    )
    record_resolution(WRITE_SURFACE.writer_id, _SHAPED_CLAUSE_INDEX, entries)


def _derive_key(basename: str) -> str:
    """Lowercase, collapse non-alnum runs to '_', strip leading/trailing '_'.

    Mirrors the bash oracle's `_derive_key` (tr-based, bash-3.2-safe) and
    cross-repo-memo's `_receiver_repo_key` resolver.
    """
    lowered = basename.lower()
    collapsed = re.sub(r"[^a-z0-9]+", "_", lowered)
    return collapsed.strip("_")


def _resolve_machine_local(self_dir: Path) -> Optional[str]:
    """Resolve the `machine-local` CLI: PATH first, then the CLAUDE_HOME/.claude/bin
    fallback, exactly as the bash oracle's `command -v machine-local` / fixed-path
    fallback ladder does. Returns None (never raises) if unresolvable.

    Existence alone gates the fallback candidate -- an exec bit is no longer
    required now that every rung is launched through an interpreter (see
    `_machine_local_launch_argv`); a PATH hit via `shutil.which` is already
    exec-bit-verified by the OS's own PATH search.
    """
    found = shutil.which("machine-local")
    if found:
        return found
    fallback = home_dir() / ".claude" / "bin" / "machine-local"
    if fallback.is_file():
        return str(fallback)
    return None


def _machine_local_launch_argv(ml_bin: str) -> List[str]:
    """Interpreter-prefix `ml_bin` for a bare-exec launch: `machine-local` is an
    extensionless coordinator/bin sibling, so `resolve_launchable()` is POSIX-bare
    by design and is not the fix there -- prefix `sys.executable` directly on
    POSIX. On Windows keep `resolve_launchable()`'s `.cmd`-twin preference and
    shebang sniffing, which are load-bearing on this repo's P0 primary platform.
    """
    if launchable._is_windows():
        return launchable.resolve_launchable(ml_bin)
    return [sys.executable, ml_bin]


def main(argv: Sequence[str], self_dir: Optional[Path] = None) -> int:
    """Port of register-discovered-repos.sh's top-level flow.

    self_dir: base directory the caller resolves itself. Defaults to the caller's
    cwd only as a defensive fallback — the caller is expected to always pass its
    own directory explicitly.
    """
    non_interactive = False
    check_only = False
    for arg in argv:
        if arg == "--non-interactive":
            non_interactive = True
        elif arg == "--check-only":
            check_only = True
        else:
            print(f"{_PROG}: unknown argument: {arg}", file=sys.stderr)
            print(f"Usage: {_PROG} [--non-interactive] [--check-only]", file=sys.stderr)
            return 1

    base_dir = self_dir if self_dir is not None else Path.cwd()

    ml_bin = _resolve_machine_local(base_dir)
    if ml_bin is None:
        print(
            f"{_PROG}: machine-local CLI not found — skipping registration. "
            "Register later: machine-local set repos.<name> <path>",
            file=sys.stderr,
        )
        return 0

    ml_argv = _machine_local_launch_argv(ml_bin)

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            _discover_working_repos_main([])
    except Exception as exc:  # noqa: BLE001 — never-block contract (matches discover_working_repos.main()'s own posture)
        print(f"{_PROG}: working-repo discovery failed: {exc}", file=sys.stderr)
        return 0

    candidates = [line for line in buf.getvalue().splitlines() if line.strip()]
    if not candidates:
        # Discovery ran and definitively found nothing — a real, knowable
        # "resolved to nothing" answer, journaled as an empty resolution
        # (not skipped), per the design note's negative spec.
        _journal_registered([])
        return 0

    to_register: List[Tuple[str, str]] = []
    for repo_path in candidates:
        key = _derive_key(os.path.basename(repo_path.rstrip("/")))
        if not key:
            # Review: code-reviewer — repo basenames that collapse to "" (all
            # non-alnum, e.g. "---") were silently dropped with no diagnostic;
            # mirrors the bash oracle's behavior (not a regression) but the
            # silence erodes trust in "why didn't repo X register" (Finding 2).
            print(
                f"{_PROG}: skipping {repo_path!r} — basename yields an empty key",
                file=sys.stderr,
            )
            continue
        has_rc = subprocess.run(
            [*ml_argv, "has", f"repos.{key}"],
            capture_output=True,
            check=False,
            **no_console_creationflags(),
        ).returncode
        if has_rc == 0:
            continue
        to_register.append((key, repo_path))

    if not to_register:
        # Every discovered repo is already registered — a definitive
        # "nothing left to register" resolution, journaled empty, same as
        # the zero-candidates case above.
        _journal_registered([])
        return 0

    if check_only:
        # Preview mode: no registration is actually performed, so nothing
        # is journaled — this run's ShapedClause resolution stays unknown,
        # not falsely "empty" (would-register is not registered).
        print(f"{_PROG}: would register {len(to_register)} repo(s):")
        for key, path in to_register:
            print(f"  repos.{key} = {path}")
        return 0

    if not non_interactive and sys.stdin.isatty():
        print(
            f"{_PROG}: {len(to_register)} discovered repo(s) not yet in the "
            "machine-local registry:"
        )
        for key, path in to_register:
            print(f"  repos.{key} = {path}")
        try:
            reply = input("Register them now? [Y/n] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("", "y", "yes"):
            print(
                f"{_PROG}: registration skipped by operator. Register later: "
                "machine-local set repos.<name> <path>"
            )
            # The operator's decision is itself a definitive, knowable
            # outcome for this run: nothing got registered — journal empty
            # rather than leaving this writer unreported.
            _journal_registered([])
            return 0

    registered: List[str] = []
    for key, path in to_register:
        print(f"{_PROG}: registering repos.{key} = {path}")
        sys.stdout.flush()
        set_rc = subprocess.run(
            [*ml_argv, "set", f"repos.{key}", path],
            check=False,
            **no_console_passthrough_kwargs(),
        ).returncode
        if set_rc != 0:
            print(f"{_PROG}: WARNING: failed to register repos.{key} — skipping.", file=sys.stderr)
        else:
            registered.append(key)

    # Journal exactly the repos.<key> entries that were actually written
    # this run — a failed `ml_bin set` is never journaled, per the design
    # note's "never journal entries for a registration that did not happen".
    _journal_registered(registered)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], self_dir=Path(__file__).resolve().parent))
