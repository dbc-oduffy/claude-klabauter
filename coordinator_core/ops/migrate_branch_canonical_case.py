"""
coordinator_core.ops.migrate_branch_canonical_case — one-shot mixed-case
work/* branch remediation.

Purpose: DR-059 bash-to-naked-Python port of the DoE-owned script
`coordinator/bin/migrate-branch-canonical-case.sh` (~140 lines). Fixes two
distinct failure modes caused by Windows/macOS case-insensitive filesystems
diverging from git's case-sensitive ref model:

  1. `.git/HEAD` can store a mixed-case symbolic-ref target even though the
     underlying loose-ref lookup resolves case-insensitively to a lowercase
     on-disk ref. `git branch --show-current` echoes HEAD's stored (mixed)
     case; `git push origin <that>` then fails ref lookup against the
     canonical (lowercase) remote ref.
  2. A `work/*` branch itself can be created (or synced from a case-sensitive
     remote) under a mixed-case name. This step renames each mixed-case
     `refs/heads/work/*` ref to its lowercase canonical sibling, unless that
     sibling already exists (in which case it's flagged for manual removal,
     never auto-deleted).

Idempotent: a second invocation on an already-canonical repo is a pure no-op.

Spec backlink: docs/plans/2026-05-07-mixed-case-branch-creation-tripwire.md
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Port of: migrate-branch-canonical-case.sh (DoE b5a4192c, 2026-07-20)

Exit codes (parity-critical, preserved from the bash oracle):
    0 — no-op (no mixed-case refs found) OR migration succeeded
    1 — usage error (unknown argument)
    2 — migration failed mid-flight (partial; rerun is safe and resumes)

Negative-spec (faithfully reproduced bash-oracle bugs -- do NOT "fix" these
mid-port; behavior-parity with the retired .sh is the contract):
    - The original script enumerates candidates via `git for-each-ref
      --format='%(refname:short)' 'refs/heads/work/*'`. git's for-each-ref
      glob matching applies FNM_PATHNAME semantics: a single `*` does NOT
      match `/`. That pattern therefore matches ONLY single-path-segment
      names directly under `refs/heads/work/` (e.g. `refs/heads/work/foo`)
      and silently matches ZERO branches under this project's actual daily
      naming convention `work/{machine}/{YYYY-MM-DD}` (two segments after
      `work/`). This module reproduces that exact matching behavior (see
      `_enumerate_work_refs`) rather than "fixing" it to a recursive glob --
      confirmed empirically against a live git 2.54 sandbox during the port
      (see oracle capture referenced in the port's completion record).
    - `-h`/`--help` prints the raw source-file docstring text lines 2 through
      the first blank line (bash: `sed -n '2,/^$/p' "$0"`). This module
      reproduces that exact text (module-level `_USAGE_TEXT` below), not a
      freshly-authored help string, so operators see byte-identical output.

Intentional normalization (NOT a bug-parity item -- deliberately diverges from
the byte-for-byte oracle):
    - The bash oracle's banner (`=== migrate-branch-canonical-case.sh ===`)
      and its usage-error line (`Usage: $0 [--push-cleanup]`) are reproduced
      with a fixed literal `_PROG` constant rather than deriving from argv[0]
      / the invoking script's absolute path. `$0`-derived text is inherently
      machine-path-dependent and not a stable contract to byte-match; this
      mirrors the same `_PROG` normalization already used by
      ops/handoff_gate_aging.py. The banner/usage TEXT content is otherwise
      identical.
"""

from __future__ import annotations

import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path

from coordinator_core.install._shared import env_overlay

# Literal program-name prefix — preserved verbatim from the retired .sh's own
# $0-derived banner/usage text so oracle-diff stdout parity holds regardless
# of what the DoE-side trampoline file is actually named. Mirrors the same
# `_PROG` convention used by ops/handoff_gate_aging.py.
_PROG = "migrate-branch-canonical-case.sh"

# Verbatim reproduction of the bash oracle's `sed -n '2,/^$/p' "$0"` output
# (lines 2 through the first blank line of the retired .sh's header comment).
# See "Negative-spec" above -- kept byte-identical, not re-derived from this
# module's own docstring.
_USAGE_TEXT = """\
# migrate-branch-canonical-case.sh — One-shot rename of mixed-case work/* refs
# to their canonical lowercase form.
#
# Spec backlink: docs/plans/2026-05-07-mixed-case-branch-creation-tripwire.md
#
# Why: on Windows's case-insensitive FS, a ref created via `git checkout -b
# work/MACHINE-A/...` lands on disk as lowercase but `.git/HEAD` stores the
# mixed-case literal. `git branch --show-current` returns HEAD's stored case;
# `git push origin <that>` fails ref lookup (case-sensitive against canonical).
# The hook now blocks creating new mixed-case refs (cs_is_canonical_branch);
# this script remediates pre-existing ones.
#
# Idempotent: a second invocation finds no mixed-case refs and exits clean.
#
# Usage:
#   bin/migrate-branch-canonical-case.sh                # local rename only
#   bin/migrate-branch-canonical-case.sh --push-cleanup # also delete mixed-case
#                                                       # remote refs and push
#                                                       # canonical form
#
# Exit codes:
#   0 — no-op (no mixed-case refs found) OR migration succeeded
#   1 — usage error
#   2 — migration failed mid-flight (partial; rerun is safe and resumes)
"""


# Sibling precedent: coordinator/bin/check-install-divergence.py's
# _GIT_TIMEOUT_SECS = 60. Local ref/plumbing calls (rev-parse, symbolic-ref,
# show-ref, for-each-ref, branch -m) get that same local bound; the three
# network calls under --push-cleanup (ls-remote, push --delete, push -u) get
# a larger bound since they wait on the remote, not just local disk/plumbing.
_GIT_TIMEOUT_SECS = 60.0
_GIT_NETWORK_TIMEOUT_SECS = 120.0


def _git(repo_root: str, *args: str, timeout: float = _GIT_TIMEOUT_SECS) -> subprocess.CompletedProcess:
    """Degrade rather than raise on timeout: a timed-out git call returns a
    synthetic non-zero CompletedProcess so callers' existing `.returncode`
    checks treat it the same as any other git failure, instead of an
    uncaught TimeoutExpired aborting the whole migration mid-flight."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Review: code-reviewer — Windows portability convention applied
            # inconsistently across this wave's siblings; align this call site.
            **no_console_creationflags(),
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=["git", *args],
            returncode=1,
            stdout="",
            stderr=f"git command timed out after {timeout:g}s: git {' '.join(args)}",
        )


def _find_git_root(cwd: str) -> str | None:
    r = _git(cwd, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _fix_head_case(repo_root: str, out) -> None:
    """Step 0: rewrite `.git/HEAD`'s symbolic-ref text to its lowercase form
    when a lowercase canonical ref already resolves (case-insensitive-FS
    loose-ref-lookup divergence). No-op when HEAD's stored text is already
    lowercase or the lowercase sibling does not exist as a real ref.
    """
    r = _git(repo_root, "symbolic-ref", "HEAD")
    head_ref = r.stdout.strip() if r.returncode == 0 else ""
    if not head_ref:
        return
    head_ref_lc = head_ref.lower()
    if head_ref == head_ref_lc:
        return
    verify = _git(repo_root, "show-ref", "--verify", "--quiet", head_ref_lc)
    if verify.returncode == 0:
        print(f"HEAD-FIX: {head_ref} → {head_ref_lc}", file=out)
        _git(repo_root, "symbolic-ref", "HEAD", head_ref_lc)
    else:
        print(
            f"HEAD-FIX-SKIP: HEAD points at '{head_ref}' but canonical "
            f"'{head_ref_lc}' does not exist as a ref",
            file=out,
        )


def _enumerate_work_refs(repo_root: str) -> list[str]:
    """Enumerate local `work/*` branch short-names via the SAME git
    for-each-ref glob the bash oracle used. See module docstring
    Negative-spec: this pattern matches only single-segment names under
    `refs/heads/work/` (FNM_PATHNAME semantics) -- reproduced verbatim, not
    widened to a recursive match.
    """
    r = _git(
        repo_root,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/work/*",
    )
    if r.returncode != 0:
        return []
    return [line for line in r.stdout.splitlines() if line]


def main(argv: list[str]) -> int:
    push_cleanup = False
    if len(argv) == 0:
        push_cleanup = False
    elif len(argv) == 1 and argv[0] == "--push-cleanup":
        push_cleanup = True
    elif len(argv) == 1 and argv[0] in ("-h", "--help"):
        # bash oracle: `sed -n '2,/^$/p' "$0"` includes the trailing blank
        # line that terminates the header-comment block -- one extra `\n`
        # beyond _USAGE_TEXT's own trailing newline reproduces it exactly.
        sys.stdout.write(_USAGE_TEXT + "\n")
        return 0
    else:
        bad = argv[0] if argv else ""
        print(f"ERROR: unknown argument: {bad}", file=sys.stderr)
        print(f"Usage: {_PROG} [--push-cleanup]", file=sys.stderr)
        return 1

    # Override the daily-branch hook for the duration of the migration: rename ops
    # would self-deny once canonical-case enforcement is live, and we're the
    # legitimate remediation path. Logged via the hook's override audit log.
    #
    # SCOPED, not exported (2026-07-21): the bash oracle's `export` died with the
    # process, but this module's assignment persisted for the interpreter's life —
    # leaving the hook's self-deny disarmed for every subsequent in-process caller
    # and for every subprocess inheriting os.environ. "For the duration" is now
    # literally true. Consolidated onto the shared env_overlay (install/_shared.py)
    # rather than a hand-rolled per-key restore, per the same "one leak-guard
    # implementation" discipline that motivated env_overlay's own introduction.
    with env_overlay({
        "COORDINATOR_OVERRIDE_BRANCH": "1",
        "COORDINATOR_OVERRIDE_BRANCH_REASON": "migrate-branch-canonical-case",
    }):
        return _migrate(push_cleanup)


def _migrate(push_cleanup: bool) -> int:
    """The migration proper. Split out of ``main`` so the ``env_overlay`` block wraps
    the whole sequence without re-indenting it; not a separate seam."""
    git_root = _find_git_root(os.getcwd())
    if git_root is None:
        print("ERROR: not in a git repo", file=sys.stderr)
        return 1

    out = sys.stdout
    print(f"=== {_PROG} ===", file=out)
    print(f"Repo: {git_root}", file=out)
    mode = "local + remote cleanup" if push_cleanup else "local only"
    print(f"Mode: {mode}", file=out)
    print("", file=out)

    # --- Step 0: HEAD-text fix -------------------------------------------
    _fix_head_case(git_root, out)

    # --- Step 1: Mixed-case ref-file rename -------------------------------
    local_refs = _enumerate_work_refs(git_root)
    mixed_case_refs = [ref for ref in local_refs if ref != ref.lower()]

    # Batch primitive (test_no_unbatched_per_item_git_spawn.py _KNOWN_SITES
    # evidence): the per-ref `show-ref --verify` sibling-existence check is
    # collapsed into ONE `show-ref --verify` call over every candidate's
    # lowercase refname, rather than one call per mixed-case ref.
    #
    # NOT a plain membership check against `local_refs`: on a case-insensitive
    # filesystem (default macOS/Windows), a mixed-case ref's lowercase
    # sibling resolves via `show-ref --verify`'s FS-level lookup even when
    # `for-each-ref`'s listing enumerates only the ONE on-disk (mixed-case)
    # entry — `show-ref`'s own ref-name pattern matching does NOT reproduce
    # that FS-level case-fold. `show-ref --verify <ref1> <ref2> ...` accepts
    # N refnames in one call, exits non-zero if ANY is unresolvable, but
    # still prints one stdout line per refname that DOES resolve and keeps
    # checking the rest — empirically verified (git 2.55, this port's own
    # sandbox) against the exact multi-ref-with-one-missing shape used here.
    # So parsing stdout, not the exit code, recovers the same per-candidate
    # "does this canonical sibling already exist" answer the old per-ref
    # `--verify --quiet` loop gave, in one spawn instead of N.
    existing_siblings: set = set()
    if mixed_case_refs:
        verify_argv = [f"refs/heads/{ref.lower()}" for ref in mixed_case_refs]
        verify_result = _git(git_root, "show-ref", "--verify", *verify_argv)
        for line in verify_result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].startswith("refs/heads/"):
                existing_siblings.add(parts[1][len("refs/heads/"):])

    renamed = 0
    skipped = 0
    failed = 0

    for ref in local_refs:
        lc = ref.lower()
        if ref == lc:
            continue  # already canonical

        if lc in existing_siblings:
            print(
                f"SKIP: '{ref}' — canonical sibling '{lc}' already exists; "
                "remove the mixed-case form manually:",
                file=out,
            )
            print(
                f"       git branch -D '{ref}'  # only if its commits are "
                f"reachable from '{lc}'",
                file=out,
            )
            skipped += 1
            continue

        print(f"RENAME: {ref} → {lc}", file=out)
        rename_result = _git(git_root, "branch", "-m", ref, lc)
        if rename_result.returncode == 0:
            renamed += 1
        else:
            print(f"  FAIL: rename '{ref}' → '{lc}' returned non-zero", file=out)
            failed += 1
            continue

        if push_cleanup:
            ls_remote = _git(
                git_root, "ls-remote", "--exit-code", "origin", f"refs/heads/{ref}",
                timeout=_GIT_NETWORK_TIMEOUT_SECS,
            )
            if ls_remote.returncode == 0:
                print(f"  REMOTE-DELETE: origin/{ref}", file=out)
                push_delete = _git(
                    git_root, "push", "origin", "--delete", ref,
                    timeout=_GIT_NETWORK_TIMEOUT_SECS,
                )
                if push_delete.returncode != 0:
                    print(
                        f"  WARN: remote delete of '{ref}' failed (may already be gone)",
                        file=out,
                    )
            print(f"  REMOTE-PUSH: origin/{lc}", file=out)
            push_new = _git(
                git_root, "push", "-u", "origin", lc,
                timeout=_GIT_NETWORK_TIMEOUT_SECS,
            )
            if push_new.returncode != 0:
                print(
                    f"  WARN: push of '{lc}' returned non-zero (may need re-run)",
                    file=out,
                )

    print("", file=out)
    print("=== Summary ===", file=out)
    print(f"  Renamed: {renamed}", file=out)
    print(f"  Skipped: {skipped}", file=out)
    print(f"  Failed:  {failed}", file=out)

    if failed > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
