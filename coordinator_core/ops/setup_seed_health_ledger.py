"""
coordinator_core.ops.setup_seed_health_ledger — repo-setup-time helper: seed
state/health-ledger.md.

Purpose: create a skeleton health-ledger in a target repo so workday-complete
Step 4 can immediately append to it without the "create if missing" branch.
Called by skills/repo-setup/SKILL.md Phase 3j during coordinator setup of a
new project repo.

Idempotent: if state/health-ledger.md already exists in the target repo, this
prints a notice and exits 0 without modification. Never overwrites
user-authored state.

Usage:
    setup-seed-health-ledger.sh [REPO_ROOT]

    REPO_ROOT — path to the target repo root; defaults to current working
    directory.

Schema source: docs/wiki/daily-summary-procedure.md § "Health Ledger Entry
Schema" — two audit clocks above a per-system table; all system grades start
at "?".

Port of: setup-seed-health-ledger.sh (coordinator-claude 6fb5fb37, 2026-07-22)
Spec backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

State-root seam: the bash original routes a meta-repo REPO_ROOT through the
Claude-klabauter state seam (C4/stop-the-rot) via `command -v coordinator_is_meta_repo`
guards — a check that is only ever true when the calling shell had already
sourced coordinator-is-meta-repo.sh/coordinator-claude-klabauter-root.sh, which never
happens for THIS script's actual invocation (skills/repo-setup/SKILL.md always
runs it as a bare `bash <path>` subprocess — confirmed via grep, no
`export -f` of either function exists anywhere in the coordinator-claude tree). This port
performs the equivalent redirection unconditionally instead of gating on a
shell-function-existence probe that has no Python analogue — same intent,
strictly more correct than the always-false bash gate. Local
`_claude_home`/`_same_path`/`_claude_klabauter_root` helpers are a deliberate duplicate
of the same trio in `coordinator_core.orientation.regenerate_cache` (that
module's own docstring documents this as the established convention pending a
canonical `coordinator_core.state_root` seam — not yet landed as of this
port).

Negative-spec:
    - Does NOT fail loud when the meta-repo's CLAUDE_KLABAUTER_ROOT is unresolvable —
      falls back to REPO_ROOT/state, mirroring the bash original's
      `[[ -n "$_SSLH_MR" ]] && _SSLH_STATE_ROOT=...` (a silent no-op
      reassignment on empty, not a `return 1`). This is a narrower posture
      than `regenerate_cache._state_root` (which does raise) — deliberately
      preserved to match THIS script's own oracle, not "fixed" mid-port.
    - Does NOT pre-populate system rows in the table — seeding with
      fabricated system names would be wrong for every repo that isn't the
      coordinator meta-repo. The consumer (workday-complete Step 4) adds rows.
    - Does NOT overwrite an existing state/health-ledger.md under any
      circumstance (idempotency gate is unconditional).
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

_CLAUDE_HOME_ENV = "CLAUDE_HOME"
_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"

_LEDGER_TEMPLATE = """# Health Ledger

<!-- Two distinct audit clocks (do not conflate):
     - Last targeted audit  — written by /architecture-audit.
     - Last full audit      — written only by a PM-invoked /architecture-survey.
     A targeted audit must NOT touch the "Last full audit" field. -->

**Last full audit:** (none — run /architecture-survey)
**Last targeted audit:** (none)
**Next rotation target:** (none — add systems as they are touched)

<!-- Grade vocabulary: A-F (or ? = never graded). Grades are written only by
     /architecture-audit or /architecture-survey, never by workday-complete. -->

| System | Grade | Last Audited | Notes |
|--------|-------|-------------|-------|
"""


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _same_path(a: str, b: str) -> bool:
    """Thin alias onto ``coordinator_core.win_portability.same_path`` -- the
    consolidated primitive (state/sizings/2026-08-07-path-equality-
    consolidates-onto-one-prim.yaml). Import kept function-local, matching
    this module's other coordinator_core imports (module docstring: bare
    ``bash <path>`` subprocess invocation, no top-level coordinator_core
    dependency). Promoted from realpath-only to samefile-then-fallback
    semantics: broader (junction-aware) equality is correct here since this
    call site only checks "is repo_root the meta-repo home", where a
    junction-aliased home must compare equal."""
    from coordinator_core.win_portability import same_path

    return same_path(a, b)


def _machine_local_get(key: str) -> Optional[str]:
    """Best-effort `machine-local get <key>` subprocess call; None on any failure
    or when `machine-local` is not resolvable on PATH.

    `machine-local` is an extensionless coordinator/bin sibling -- a bare-path
    launch depends on the target's own shebang + exec bit, which is not
    guaranteed once C4 strips coordinator/bin/*.py shebangs. Resolve the full
    path via PATH search first, then launch through an interpreter:
    `resolve_launchable()` on Windows (its `.cmd`-twin preference and shebang
    sniffing are load-bearing on this repo's P0 primary platform), a direct
    `sys.executable` prefix on POSIX (`resolve_launchable()` is POSIX-bare by
    design and is not the fix for this bug class).
    """
    import shutil
    import subprocess

    from coordinator_core import launchable
    from coordinator_core.win_portability import no_console_creationflags

    ml_bin = shutil.which("machine-local")
    if ml_bin is None:
        return None

    if launchable._is_windows():
        ml_argv = launchable.resolve_launchable(ml_bin)
    else:
        ml_argv = [sys.executable, ml_bin]

    try:
        result = subprocess.run(
            [*ml_argv, "get", key],
            capture_output=True,
            text=True,
            timeout=5,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _machine_local_get: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root() -> Optional[str]:
    """Resolve the claude-klabauter repo root: CLAUDE_KLABAUTER_ROOT env, else machine-local registry."""
    override = os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV, "").strip()
    if override:
        return override
    return _machine_local_get("repos.claude_klabauter")


def _resolve_state_root(repo_root: str) -> str:
    """Resolve the state/ directory for repo_root, routing a meta-repo root
    through the claude-klabauter state seam. Mirrors the bash original's graceful
    (non-fail-loud) fallback when CLAUDE_KLABAUTER_ROOT is unresolvable.
    """
    if _same_path(repo_root, _claude_home()):
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root:
            return os.path.join(claude_klabauter_root, "state")
    return os.path.join(repo_root, "state")


def seed_health_ledger(repo_root: str) -> int:
    """Seed state/health-ledger.md in repo_root; idempotent. Returns process exit code."""
    if not os.path.isdir(repo_root):
        print(
            f"setup-seed-health-ledger: REPO_ROOT does not exist or is not a directory: {repo_root}",
            file=sys.stderr,
        )
        print("  Remediation: pass a valid directory path as the first argument.", file=sys.stderr)
        return 1

    state_root = _resolve_state_root(repo_root)
    ledger_path = os.path.join(state_root, "health-ledger.md")

    if os.path.isfile(ledger_path):
        print(
            f"[health-ledger] state/health-ledger.md already exists in {repo_root} — skipping (idempotent)."
        )
        return 0

    if not os.path.isdir(state_root):
        try:
            os.makedirs(state_root, exist_ok=True)
        except OSError:
            print(
                f"setup-seed-health-ledger: cannot create state/ directory at {state_root}",
                file=sys.stderr,
            )
            print(f"  Remediation: check directory permissions for {repo_root}.", file=sys.stderr)
            return 1

    print(f"[health-ledger] seeding state/health-ledger.md in {repo_root}")

    try:
        with open(ledger_path, "w", encoding="utf-8") as fh:
            fh.write(_LEDGER_TEMPLATE)
    except OSError:
        print(f"setup-seed-health-ledger: failed to write {ledger_path}", file=sys.stderr)
        print(f"  Remediation: check write permissions on {state_root}.", file=sys.stderr)
        return 1

    print(
        "[health-ledger] done — state/health-ledger.md seeded with two audit clocks and empty per-system table."
    )
    print(
        "[health-ledger] Systems are added automatically by workday-complete Step 4 (grade: ?) on first commit touch."
    )
    return 0


def main(argv: List[str]) -> int:
    repo_root = argv[0] if argv else os.getcwd()
    return seed_health_ledger(repo_root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
