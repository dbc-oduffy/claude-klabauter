"""
coordinator_core.ops.migrate_completion_log_legacy — idempotent monolith
migration port of DoE's migrate-completion-log-legacy.sh trampoline.

Purpose: moves legacy monthly monolith completion-log files
(`archive/completed/YYYY-MM.md`) under `archive/completed/legacy/` so the
completion-log query tooling can ignore them without special-case glob
exclusions.

Invocation contexts:
  PM-invoked one-shot: run manually when first adopting the per-entry layout.
  Per-close-out wrap: invoked automatically by coordinator-complete-entry.sh
    at each workstream close-out when a root monolith is detected. Full
    idempotency (re-entrant, safe to call repeatedly) makes this safe.

Not a JSON-RPC op — a plain module, NOT @register_op'd, called by direct
import from the DoE polyglot trampoline (template-variant #1, mirrors
coordinator-auto-push / handoff-gate-aging / check-no-monolith-completion-append).

Port of: migrate-completion-log-legacy.sh (DoE 290997c7, 2026-07-22)
Spec backlink: docs/plans/2026-05-19-completion-log-phase1-foundational-loop.md § Chunk 7
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec:
    - Root resolution order faithfully mirrors the bash oracle: explicit
      --root flag > MIGRATION_REPO_ROOT env var > `git rev-parse
      --show-toplevel` auto-discovery > hard error. No fourth fallback exists.
    - Monolith detection is a NON-recursive scan (maxdepth 1 equivalent) of
      `archive/completed/` for filenames matching `YYYY-MM.md` exactly
      (4-digit year, 2-digit month) — files under subdirectories are never
      touched, matching the bash `find -maxdepth 1` behavior.
    - Idempotency guard is per-file at the destination: if the legacy/
      destination path already exists, that source file is SKIPPED (not
      overwritten, not treated as an error) — mirrors the bash `[[ -e "$dst" ]]`
      skip branch exactly.
    - Uses `git mv` via subprocess (not a plain filesystem rename) so the
      move is staged as a git rename, matching the bash oracle's use of
      `git -C "$REPO_ROOT" mv "$src" "$dst"`.
    - Exit codes reproduced exactly: 0 = success (0+ files moved, or no-op
      when no monoliths found); 1 = usage/environment error (not in a repo
      with archive/completed/, or --root points at a non-existent path with
      no archive/completed/ under it); 2 = one or more `git mv` operations
      failed. A failed --root resolution (not a git repo, no env var, no
      flag) is ALSO exit 1, matching the bash oracle's single error-exit path
      for both "no repo root resolvable" and "archive/completed/ missing".
"""

from __future__ import annotations

import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from typing import List, Optional

from coordinator_core.ops._git_root_util import git_root
from coordinator_core.session.declared_writes import declare_write

_MONOLITH_RE = re.compile(r"^[0-9]{4}-[0-9]{2}\.md$")


def _resolve_repo_root(explicit_root: Optional[str], cwd: Optional[str] = None) -> Optional[str]:
    """Root resolution ladder: explicit --root > MIGRATION_REPO_ROOT env > git auto-discovery.

    Mirrors the bash oracle's precedence exactly. Returns None if no root
    could be resolved (caller emits the "not inside a git repository" error).
    """
    if explicit_root:
        return explicit_root

    env_root = os.environ.get("MIGRATION_REPO_ROOT")
    if env_root:
        return env_root

    return git_root(cwd)


def _find_monoliths(completed_dir: str) -> List[str]:
    """Non-recursive scan of completed_dir for YYYY-MM.md monolith files, sorted."""
    if not os.path.isdir(completed_dir):
        return []
    hits = []
    for name in os.listdir(completed_dir):
        full = os.path.join(completed_dir, name)
        if os.path.isfile(full) and _MONOLITH_RE.match(name):
            hits.append(full)
    return sorted(hits)


def _git_mv(repo_root: str, src: str, dst: str) -> bool:
    result = subprocess.run(
        ["git", "-C", repo_root, "mv", src, dst],
        capture_output=True,
        text=True,
        # Review: code-reviewer — Windows portability convention applied
        # inconsistently across this wave's siblings; align this call site.
        **no_console_creationflags(),
    )
    if result.returncode != 0 and result.stderr.strip():
        print(f"  git mv stderr: {result.stderr.strip()}", file=sys.stderr)
    return result.returncode == 0


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, root resolution, scan, migrate, print, return rc."""
    explicit_root: Optional[str] = None

    # Review: code-reviewer — `--root ""` (empty explicit value) previously fell
    # through unconsumed and silently degraded to env/git-auto-discovery instead
    # of erroring on the operator's explicit (if empty) flag. Now an explicit
    # usage error, matching `--root=` below.
    rest = argv[:]
    if rest and rest[0] == "--root" and len(rest) > 1 and not rest[1]:
        print("ERROR: --root requires a non-empty value.", file=sys.stderr)
        return 1
    if rest and rest[0] == "--root" and len(rest) > 1:
        explicit_root = rest[1]
        rest = rest[2:]
    elif rest and rest[0].startswith("--root="):
        explicit_root = rest[0][len("--root="):]
        if not explicit_root:
            print("ERROR: --root requires a non-empty value.", file=sys.stderr)
            return 1
        rest = rest[1:]

    repo_root = _resolve_repo_root(explicit_root)
    if not repo_root:
        print("ERROR: not inside a git repository.", file=sys.stderr)
        print(
            "  Remediation: run from a git repo root, or set MIGRATION_REPO_ROOT=<path>.",
            file=sys.stderr,
        )
        return 1

    completed_dir = os.path.join(repo_root, "archive", "completed")
    if not os.path.isdir(completed_dir):
        print(f"ERROR: archive/completed/ not found under: {repo_root}", file=sys.stderr)
        print(
            "  Remediation: run from a repo that uses the completion-log layout, or set",
            file=sys.stderr,
        )
        print("  MIGRATION_REPO_ROOT to the correct repo root.", file=sys.stderr)
        return 1

    legacy_dir = os.path.join(completed_dir, "legacy")

    print("=== migrate-completion-log-legacy.sh ===")
    print(f"Repo: {repo_root}")
    print(f"Source dir: {completed_dir}")
    print(f"Target dir: {legacy_dir}")
    print("")

    monoliths = _find_monoliths(completed_dir)

    if not monoliths:
        print("No legacy monolith files found at the root of archive/completed/.")
        print(
            "Nothing to migrate — this repo is already in the per-entry layout, or was"
        )
        print("already migrated. Exiting (no-op).")
        return 0

    print(f"Found {len(monoliths)} legacy monolith file(s):")
    for f in monoliths:
        print(f"  {f}")
    print("")

    if not os.path.isdir(legacy_dir):
        os.makedirs(legacy_dir, exist_ok=True)
        print(f"Created: {legacy_dir}")

    moved = 0
    failed = 0
    failed_files: List[str] = []

    for src in monoliths:
        filename = os.path.basename(src)
        dst = os.path.join(legacy_dir, filename)

        if os.path.exists(dst):
            print(f"SKIP (already at destination): {filename}")
            continue

        print(f"MOVE: archive/completed/{filename}  →  archive/completed/legacy/{filename}")
        if _git_mv(repo_root, src, dst):
            # DR-276: declared AFTER the move lands, never before — the
            # contract is a report of what was ACTUALLY written, not of an
            # intended surface. `dst` is the final destination `git mv`
            # rewrote src into, never the pre-move src path.
            declare_write(dst)
            moved += 1
        else:
            print(f"  FAIL: git mv returned non-zero for {filename}", file=sys.stderr)
            failed += 1
            failed_files.append(filename)

    print("")
    print("=== Summary ===")
    print(f"  Moved:  {moved}")
    print(f"  Failed: {failed}")
    print("")

    if moved > 0:
        print("Next step: commit the staged renames.")
        print(
            "  git commit -m 'migrate: move legacy completion monoliths under archive/completed/legacy/'"
        )
        print("")

    print("Note: legacy/ entries are excluded from query-completions results.")
    print("  query-completions.sh targets archive/completed/*/*.md (per-entry files).")
    print("  Files under archive/completed/legacy/ are in that glob's scope as one of")
    print("  the '*' segments, BUT query-records.js excludes the legacy/ bucket from")
    print("  result sets (absence-as-signal doctrine — monolith content is not backfilled).")
    print(
        "  See: docs/plans/2026-05-19-completion-log-phase1-foundational-loop.md § Chunk 2"
    )
    print("")

    if failed > 0:
        print("ERROR: the following files failed to move:", file=sys.stderr)
        for f in failed_files:
            print(f"  {f}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
