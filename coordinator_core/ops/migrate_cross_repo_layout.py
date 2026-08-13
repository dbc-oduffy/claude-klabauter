"""
coordinator_core.ops.migrate_cross_repo_layout — one-time idempotent cross-repo
memo-channel layout migration (flat -> inbox/archive co-located layout).

Purpose: re-homes a repo's cross-repo memo channel from the legacy flat layout
to the inbox/archive co-located layout:
  - cross-repo/*.md (non-README)  -> cross-repo/inbox/
  - archive/cross-repo/*          -> cross-repo/archive/
  - removes the now-empty top-level archive/cross-repo/ directory.

Not a JSON-RPC op — a plain module, NOT @register_op'd, called by direct
import from the coordinator-claude polyglot trampoline (template-variant #1, mirrors
coordinator-auto-push / handoff-gate-aging / migrate_completion_log_legacy).

Port of: migrate-cross-repo-layout.sh (coordinator-claude 290997c7, 2026-07-22)
Spec backlink: docs/plans/2026-05-23-cross-repo-inbox-archive-restructure.md § Chunk F
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Idempotency basis: the glob `cross-repo/*.md` is non-recursive — it cannot
match `cross-repo/inbox/*.md` or `cross-repo/archive/*.md`. After the first
run, the only file at `cross-repo/*.md` is README.md (explicit exclusion), so
a re-run is a clean no-op. No special "skip-if-moved" logic needed; glob
geometry is the guard.

Target-collision behaviour: if a target file already exists in inbox/ or
archive/ (e.g. a partial prior run), the move FAILS LOUD naming the
conflicting file — never silently overwrites.

Concurrency: single-shot local op, non-resumable but idempotent.

Exit codes reproduced exactly from the bash oracle:
  0 — success (N moves or idempotent no-op)
  1 — usage/environment error, or target-collision detected
  2 — one or more move (`git mv` / plain-mv+add) operations failed

Negative-spec (faithful oracle-bug repro — do NOT silently "fix"):
  - No `MIGRATION_REPO_ROOT`-style environment-variable rung exists in the
    root-resolution ladder — the bash oracle only supports `--root <path>`
    then `git rev-parse --show-toplevel` auto-discovery (unlike the sibling
    migrate_completion_log_legacy.sh, which DOES have an env-var rung). Do
    not add one here — it would silently diverge from the byte-oracle.
  - `--root` supplied as the final token with no following value is a bash
    `set -u` unbound-variable crash in the oracle (implicit exit 1, no
    remediation text). This port surfaces the same exit code (1) via an
    explicit usage message rather than reproducing bash's raw crash text —
    the exit-code contract is preserved; the diagnostic wording is not
    byte-identical (the bash crash message is not meaningful to reproduce).
  - Phase 2's tracked/untracked branch runs `git ls-files --error-unmatch` on
    entries that may be DIRECTORIES (not just files) — this mirrors the bash
    oracle's identical dual-purpose loop (`find -maxdepth 1 -mindepth 1`
    enumerates both files and directories) applying the same tracked-check to
    both; git's own semantics for a directory pathspec are inherited
    unchanged, not special-cased here.
  - Phase 1's glob (`cross-repo/*.md`) and Phase 2's directory scan
    (`os.listdir`) both faithfully reproduce the oracle's dotfile handling:
    Phase 1 (shell glob, no dotglob) never matches a leading-dot filename;
    Phase 2 (`find -maxdepth 1 -mindepth 1`) enumerates dotfiles. Python's
    `glob.glob("*.md")` and `os.listdir()` have the identical dot-matching
    asymmetry, so no special-casing was needed to preserve it.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from typing import List, Optional

from coordinator_core.ops._git_root_util import git_root
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.session.scope import relocate_touched_path
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

_GIT_TIMEOUT = 30  # seconds — bounded per porter-brief addendum §2 (loop over disk-authored set)


def _run_git(args: List[str], timeout: int = _GIT_TIMEOUT) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **_CREATIONFLAGS,
    )


def _is_tracked(repo_root: str, relpath: str) -> bool:
    result = _run_git(["git", "-C", repo_root, "ls-files", "--error-unmatch", relpath])
    return result.returncode == 0


def _move_one(repo_root: str, src: str, dst: str, session_id: str = "") -> bool:
    """Move src -> dst via `git mv` (tracked) or plain move + `git add` (untracked).

    Returns True on success, False on any failure (mirrors the bash oracle's
    `git mv` / `mv` / `git add` failure branches, each of which is fatal and
    causes the caller to exit 2).

    Untracked branch (plan docs/plans/2026-08-06-relocation-re-declares-the-
    touch-claim.md, C5d): routed through
    coordinator_core.session.scope.relocate_touched_path when *session_id*
    resolved to something non-empty, so a T-claim this session holds on the
    untracked source (new content this session may have authored — exactly
    the shape invisible to git's own rename tracking) is re-declared at the
    destination rather than left to strand. relocate_touched_path itself
    does not catch a shutil.move failure (see its own docstring), so that
    failure still surfaces as the OSError this function has always caught
    here, preserving the existing skip/return-False contract unchanged. Any
    OTHER failure reaching the helper (e.g. its internal claim-bookkeeping)
    degrades to the plain shutil.move this branch used before C5d, mirroring
    the fail-open pattern coordinator_core/ops/priority_drain.py::_move and
    coordinator_core/percolate/rewrite_basename.py::_do_rename already use
    for this same helper — a migration step already committed to moving a
    file must never fail, or behave differently, because of a claim-
    bookkeeping nicety. A falsy *session_id* (unresolved/no live session)
    takes the plain move directly, same as before C5d.
    """
    rel_src = os.path.relpath(src, repo_root)
    rel_dst = os.path.relpath(dst, repo_root)

    if _is_tracked(repo_root, rel_src):
        result = _run_git(["git", "-C", repo_root, "mv", rel_src, rel_dst])
        if result.returncode == 0:
            # DR-276: declared AFTER the git-mv lands — dst is the final
            # write site for the tracked branch.
            declare_write(dst)
        return result.returncode == 0

    moved = False
    if session_id:
        try:
            relocate_touched_path(session_id, rel_src, rel_dst, cwd=repo_root)
            moved = True
        except OSError:
            print(f"skip: _move_one: shutil.move(src, dst) failed: {sys.exc_info()[1]}", file=sys.stderr)
            return False
        except Exception:
            moved = False

    if not moved:
        try:
            shutil.move(src, dst)
        except OSError:
            print(f"skip: _move_one: shutil.move(src, dst) failed: {sys.exc_info()[1]}", file=sys.stderr)
            return False

    result = _run_git(["git", "-C", repo_root, "add", rel_dst])
    if result.returncode == 0:
        # DR-276: declared AFTER the move lands at its destination — dst is
        # the real, final write site (never rel_src, never a temp path);
        # relocate_touched_path (untracked branch) re-declares the T-claim
        # at dst internally, but this declaration is the uniform one that
        # covers BOTH the git-mv and plain-move/git-add branches above.
        declare_write(dst)
    return result.returncode == 0


def _usage_text() -> str:
    return (
        "migrate-cross-repo-layout — One-time idempotent migration primitive.\n"
        "\n"
        "Re-homes a repo's cross-repo memo channel from the legacy flat layout to\n"
        "the inbox/archive co-located layout.\n"
        "\n"
        "Usage:\n"
        "  cd <repo-root>\n"
        "  migrate-cross-repo-layout\n"
        "\n"
        "  Or with explicit repo root:\n"
        "  migrate-cross-repo-layout --root /path/to/repo\n"
        "\n"
        "Exit codes:\n"
        "  0 — success (N moves or idempotent no-op)\n"
        "  1 — usage/environment error or target-collision detected\n"
        "  2 — one or more move operations failed\n"
    )


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, root resolution, Phase 1/2/3 migration, summary, rc."""
    explicit_root: Optional[str] = None

    rest = list(argv)
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--root":
            if i + 1 >= len(rest):
                print("ERROR: --root requires a value.", file=sys.stderr)
                return 1
            explicit_root = rest[i + 1]
            i += 2
            continue
        if tok in ("--help", "-h"):
            print(_usage_text())
            return 0
        print(f"Unknown argument: {tok}", file=sys.stderr)
        print("Usage: migrate-cross-repo-layout [--root <repo-root>]", file=sys.stderr)
        return 1

    if explicit_root:
        repo_root = explicit_root
    else:
        resolved = git_root()
        if not resolved:
            print("ERROR: not inside a git repo and --root not supplied.", file=sys.stderr)
            print(
                "Remediation: run from within a git repo or pass --root <path>.",
                file=sys.stderr,
            )
            return 1
        repo_root = resolved

    if not os.path.isdir(repo_root):
        print(f"ERROR: repo root does not exist: {repo_root}", file=sys.stderr)
        return 1

    # Confirm the resolved/explicit root is actually inside (or at) a git repo.
    check = _run_git(["git", "-C", repo_root, "rev-parse", "--show-toplevel"])
    if check.returncode != 0:
        print(f"ERROR: {repo_root} is not a git repository.", file=sys.stderr)
        return 1

    # Resolved ONCE per main() call, not per-file — every move below belongs
    # to the same invoking session; see _move_one's docstring for why an
    # unresolvable ("") session id takes the plain-move path directly.
    session_id = resolve_session_id(repo_root)

    cross_repo_dir = os.path.join(repo_root, "cross-repo")
    inbox_dir = os.path.join(cross_repo_dir, "inbox")
    archive_dir = os.path.join(cross_repo_dir, "archive")
    legacy_archive_dir = os.path.join(repo_root, "archive", "cross-repo")

    os.makedirs(inbox_dir, exist_ok=True)
    os.makedirs(archive_dir, exist_ok=True)

    inbox_moves = 0
    archive_moves = 0

    # Phase 1: flat cross-repo/*.md (non-README) -> cross-repo/inbox/
    for src in sorted(glob.glob(os.path.join(cross_repo_dir, "*.md"))):
        if not os.path.isfile(src):
            continue
        filename = os.path.basename(src)
        if filename == "README.md":
            continue

        target = os.path.join(inbox_dir, filename)
        if os.path.exists(target):
            print(f"ERROR: target already exists (collision): {target}", file=sys.stderr)
            print(f"  Source: {src}", file=sys.stderr)
            print("  Resolve the conflict manually, then re-run.", file=sys.stderr)
            return 1

        if not _move_one(repo_root, src, target, session_id):
            print(f"ERROR: move failed for: {src} -> {target}", file=sys.stderr)
            return 2

        print(f"  inbox: {filename}")
        inbox_moves += 1

    # Phase 2: archive/cross-repo/* -> cross-repo/archive/
    if os.path.isdir(legacy_archive_dir):
        for filename in sorted(os.listdir(legacy_archive_dir)):
            src = os.path.join(legacy_archive_dir, filename)
            target = os.path.join(archive_dir, filename)

            if os.path.exists(target):
                print(f"ERROR: target already exists (collision): {target}", file=sys.stderr)
                print(f"  Source: {src}", file=sys.stderr)
                print("  Resolve the conflict manually, then re-run.", file=sys.stderr)
                return 1

            if not _move_one(repo_root, src, target, session_id):
                print(f"ERROR: move failed for: {src} -> {target}", file=sys.stderr)
                return 2

            print(f"  archive: {filename}")
            archive_moves += 1

    # Phase 3: remove the now-empty top-level archive/cross-repo/ directory.
    archive_removed = False
    if os.path.isdir(legacy_archive_dir):
        remaining = len(os.listdir(legacy_archive_dir))
        if remaining == 0:
            try:
                os.rmdir(legacy_archive_dir)
                archive_removed = True
            except OSError as exc:
                print(f"WARNING: could not remove {legacy_archive_dir}: {exc}", file=sys.stderr)
        else:
            print(
                f"WARNING: archive/cross-repo/ not empty after moves ({remaining} items "
                "remain); leaving in place.",
                file=sys.stderr,
            )

    print("")
    print("migrate-cross-repo-layout: complete")
    print(f"  inbox moves:   {inbox_moves}")
    print(f"  archive moves: {archive_moves}")
    if archive_removed:
        print("  archive/cross-repo/ removed (was empty)")
    else:
        if os.path.isdir(legacy_archive_dir):
            print("  archive/cross-repo/ skipped (not empty or not present)")
        else:
            print("  archive/cross-repo/ not present (already clean)")

    if inbox_moves == 0 and archive_moves == 0:
        print("  (no-op: nothing to migrate)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
