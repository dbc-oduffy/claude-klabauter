"""
coordinator_core.ops.configure_git — ported from
coordinator/bin/coordinator-configure-git (DOE-PORT bin-entrypoint variant).

Purpose: applies coordinator's git-config hardening to a repo — two content-neutral,
idempotent local-config settings:

1. `gc.autoDetach false`: git's default auto-gc/maintenance DETACHES into a background
   process on Git-for-Windows, and that detached child is the likely concurrent
   handle-holder that blocks a commit's `index.lock` cleanup under concurrent-EM
   sessions. Setting autoDetach=false makes auto-gc run SYNCHRONOUSLY in the foreground
   of the triggering command, eliminating the "lock outlived the process that created
   it" race while preserving automatic repacking.

2. `core.checkStat minimal`: Git-for-Windows records nanosecond mtimes in the index, but
   NTFS reports them back at coarser precision, and the default `checkStat` also
   compares the unstable ctime/ino/dev fields — so under concurrent-EM sessions
   continuously rewriting the shared .git/index, entries perpetually re-flag as "racy"
   and `git status` reports a phantom dirty tree. `minimal` compares only mtime+size,
   which are stable across the NTFS round-trip, curing the phantom-dirty churn. Benign
   and content-neutral on every platform (no-op effect where ctime/ino are already
   stable).

Behavior contract: per-repo (local git config) by default; `--global` sets the
machine-wide default instead. Idempotent — re-running with matching config already set
is a no-op (reported on stderr, exit 0). Exit codes: 0 — configured (or already
correct); 1 — not a git repository (per-repo mode only) or `git config` failed to set a
key. All diagnostic/status output goes to stderr; stdout is unused, matching the bash
oracle's contract.

Spec backlink: cross-repo/inbox/2026-05-30-index-lock-leak-concurrent-em.md (example-game-repo
consult); docs/wiki/concurrent-em-hazards.md § H21.
Prior bash implementation: coordinator/bin/coordinator-configure-git.

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix" mid-port):
    - Does NOT validate that `--global` mode is run inside a git repository — the bash
      oracle's `git rev-parse --git-dir` guard only fires in per-repo mode; `--global`
      config writes happen regardless of cwd.
    - Does NOT support any flag other than `--global` (or no flag) — any other first
      argument is silently treated as "no flag" (per-repo mode), exactly matching the
      bash oracle's `[[ "${1:-}" == "--global" ]]` single-value comparison.
    - Does NOT batch `git config` calls — invokes `git config --get` then `git config
      <key> <value>` per setting, one subprocess pair per key, matching the oracle's
      per-key loop and its per-key partial-failure exit behavior (a failure on the
      second key exits 1 even if the first key already changed).
"""
from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence

from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)

# Review: code-reviewer — Finding 4 (2026-07-22 sidecar, nit): dropped typing.List/
# Tuple/Optional/Sequence in favor of builtin generics for consistency with
# machine_local_forwarder.py, landed in the same port wave and already using
# builtin-generic form — both files carry `from __future__ import annotations`,
# which makes this legal.
_SETTINGS: tuple[tuple[str, str], ...] = (
    ("gc.autoDetach", "false"),
    ("core.checkStat", "minimal"),
)

WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="configure-git",
    source_module="coordinator_core.ops.configure_git",
    clauses=(
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(kind="git-config-key", key=key) for key, _ in _SETTINGS
            ),
        ),
    ),
)
"""This writer's declared write surface — derived FROM `_SETTINGS` (never a
restated literal list), so a future edit to `_SETTINGS` alone cannot make
this declaration under-report without its test going red. See spec
backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md,
chunk C2."""


def _git_config_get(scope: Sequence[str], key: str) -> str | None:
    """Return the current value of `key` in the given scope, or None if unset/failed."""
    try:
        res = subprocess.run(
            ["git", "config", *scope, "--get", key],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip()


def _git_config_set(scope: Sequence[str], key: str, value: str) -> bool:
    """Set `key` to `value` in the given scope. Returns True on success."""
    try:
        res = subprocess.run(
            ["git", "config", *scope, key, value],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return False
    return res.returncode == 0


def _is_git_repo() -> bool:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return False
    return res.returncode == 0


def main(argv: list[str]) -> int:
    """CLI entrypoint: coordinator-configure-git [--global]."""
    is_global = bool(argv) and argv[0] == "--global"
    scope_label = "global" if is_global else "repo"
    scope: tuple[str, ...] = ("--global",) if is_global else ()

    if not is_global:
        if not _is_git_repo():
            print("coordinator-configure-git: not a git repository", file=sys.stderr)
            return 1

    changed = False
    for key, want in _SETTINGS:
        cur = _git_config_get(scope, key)
        if cur == want:
            continue
        if _git_config_set(scope, key, want):
            print(
                f"coordinator-configure-git: set {scope_label} {key}={want} "
                f"(was: {cur if cur is not None else '<unset>'})",
                file=sys.stderr,
            )
            changed = True
        else:
            print(
                f"coordinator-configure-git: ERROR — failed to set {key}={want}",
                file=sys.stderr,
            )
            return 1

    if not changed:
        print(
            f"coordinator-configure-git: {scope_label} git config already hardened (no change)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
