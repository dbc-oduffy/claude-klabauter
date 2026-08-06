"""
coordinator_core.ops.ceremony.commit_exec_bit — JSON-RPC "commit.exec_bit_change" op.

Purpose: restores the executable bit on a tracked file and commits that mode change
in the ONE form that survives Windows — the DR-151 sequence. Under
`core.fileMode=false` (the default git sets on Windows/NTFS clones), a
path-restricted `git commit <path>` silently RESETS the staged `update-index
--chmod=+x` mode back to 100644, so the exec-bit fix evaporates from the commit.
The only Windows-safe form is: stage the mode with `git update-index --chmod=+x`,
re-verify the staged mode, then commit WITHOUT any path restriction. That
unrestricted-commit branch runs unconditionally on EVERY platform — there is no
separate POSIX code path that could silently diverge from the Windows-safe form
(DR-151: Windows IS the spec, not a fallback).

Sequence (settlement B2, docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md):
    1. `git ls-files --stage -- <path>` — mode already 100755 → vacuous no-op
       `{committed: false, sha: null, already_executable: true}`.
    2. Foreign-staged-entries guard (B2 AMENDMENT): `git diff --cached --name-only`;
       if the index holds ANY entry other than the target path → structured error
       NAMING the foreign entries. Fail loud — never sweep a concurrent session's
       staged work into this commit, and never reset/unstage it either (the DEC-3
       refusal of the leading-`git reset` variant stands; see Negative-spec).
    3. dry_run short-circuit (CC-6): everything above resolved and validated, zero
       writes performed, mutation flags returned false.
    4. `git update-index --chmod=+x -- <path>`, re-verify the staged mode is
       100755 via `ls-files --stage` (fail loud on any other observed mode —
       CC-7: an unclassified half-state is a structured error, never a guess).
    5. `git commit -m <message>` — WITHOUT path restriction (the DR-151 point).

Idempotency (AC7 / DEC-7 note): achieved by the step-1 short-circuit, closed
explicitly rather than relying on git's own harmless-fail exit code — after a
successful run the index (and HEAD) hold the target at mode 100755, so a second
invocation with identical inputs reads that staged mode in step 1 and returns the
vacuous no-op `{committed: false, sha: null, already_executable: true}` without
staging or committing anything. The double-invocation test in
tests/test_commit_exec_bit.py proves this shape (CC-4).

Keying scope: common_dir — the handler receives repo_root = git_common_dir (the
.git directory) and derives the caller's worktree via
`main_worktree_root(common_dir)` from ops/fleet/_common.py (commit.anchors
precedent), so the exec-bit fix lands against the CALLER's tree, never claude-klabauter's
own clone. `params.repo_root` is the optional D3 consistency assertion only
(`check_repo_root`), never the worktree-resolution source.

Spec backlinks:
    docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § B2
    docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Wave 3
    DR-151 (Windows core.fileMode=false footgun — the op's reason to exist)

Negative-spec (hard-won):
  - Does NOT run `git reset` / `git restore --staged` / any unstage in ANY branch —
    on a foreign-staged index it fails loud with the entries named and leaves the
    peer session's staged work exactly as found (DEC-3 refusal; safe-commit stomp
    hazard). Sweeping OR clearing a concurrent session's index are both stomps.
  - Does NOT path-restrict the commit — `git commit -m ... -- <path>` is exactly
    the DR-151 footgun this op exists to avoid; there is no platform branch where
    a path-restricted commit is used.
  - Does NOT branch on sys.platform anywhere — the Windows-safe form IS the form.
  - Does NOT use shell=True or any shell interpreter (CC-1) — list-argv `git`
    only, via ceremony/git_native._git (Windows-safe flag set carried once).
  - Does NOT chmod the working-tree file — `git update-index --chmod=+x` mutates
    the INDEX mode only, which is the portable operation (an os.chmod would be a
    no-op on core.fileMode=false clones and a divergent side effect elsewhere).
  - Does NOT use params.repo_root as the worktree source (D3: socket-authoritative
    common_dir only) and does NOT inline `.parent` — main_worktree_root() is the
    single reviewed home for that derivation.
  - Does NOT invent recovery for an unexpected post-chmod staged mode — CC-7
    structured error naming the observed mode.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import _git
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root

#: Index mode of a tracked executable regular file.
_MODE_EXECUTABLE = "100755"

#: Index mode of a tracked non-executable regular file (the only mode this op
#: is defined to promote — anything else is an unclassified state, CC-7).
_MODE_REGULAR = "100644"


def _error(message: str, **extra: object) -> dict:
    """Build the structured-error result envelope for this op.

    Purpose: uniform fail-loud shape — contract fields present with mutation
    flags false, plus "error" (and any naming payload such as foreign_staged).
    """
    result: dict = {
        "committed": False,
        "sha": None,
        "already_executable": False,
        "error": message,
    }
    result.update(extra)
    return result


def _staged_mode(worktree_root: Path, rel_path: str) -> Tuple[Optional[str], Optional[str]]:
    """Read the index mode of rel_path via `git ls-files --stage -- <path>`.

    Returns (mode, None) when the path has exactly one index entry, (None, None)
    when the path is untracked (no entry), and (None, error_message) when the
    git invocation itself failed or the entry line is unparseable.
    """
    result = _git(["ls-files", "--stage", "--", rel_path], cwd=worktree_root)
    if not result.ok:
        return None, (
            f"git ls-files --stage failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        return None, None  # untracked — no index entry
    # Format: "<mode> <object> <stage>\t<path>". A merge-conflicted path yields
    # multiple stage entries — that is an unclassified half-state for this op.
    if len(lines) > 1:
        return None, (
            f"path {rel_path!r} has {len(lines)} index entries (merge conflict "
            f"stages?) — refusing to guess which mode governs (CC-7 fail-loud)"
        )
    parts = lines[0].split(None, 2)
    if not parts:
        return None, f"unparseable ls-files --stage line: {lines[0]!r}"
    return parts[0], None


def _foreign_staged_entries(worktree_root: Path, rel_path: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """List staged index entries other than rel_path (B2 foreign-staged guard).

    Returns (foreign_entries, None) on success — empty list means the index is
    clean or holds solely the target — or (None, error_message) when the git
    invocation failed.
    """
    result = _git(["diff", "--cached", "--name-only"], cwd=worktree_root)
    if not result.ok:
        return None, (
            f"git diff --cached --name-only failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    staged = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    foreign = [entry for entry in staged if entry != rel_path]
    return foreign, None


@register_op("commit.exec_bit_change")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "commit.exec_bit_change" handler — mutating, sync.

    Sync (not async): all I/O in the call tree is synchronous subprocess.run;
    ipc.py offloads sync handlers via asyncio.to_thread (commit_anchors pattern).

    Params:
        repo_root (str, optional) — D3 consistency assertion only (check_repo_root);
                                    NEVER the worktree-resolution source.
        path      (str, required) — repo-relative path of the tracked file whose
                                    executable bit is being restored. Compared
                                    against `git diff --cached` output, which is
                                    always /-separated, so any backslashes are
                                    normalized before comparison.
        dry_run   (bool, optional, default False) — CC-6: resolve and validate
                                    everything (tracked-path check, foreign-staged
                                    guard), perform zero writes, return the
                                    would-be result with mutation flags false.

    Returns:
        {"committed": bool, "sha": str|None, "already_executable": bool}
        — plus {"error": str, ...} on any structured failure (mutation flags
        false; "foreign_staged": [paths] names the guard's blocking entries),
        and {"dry_run": True} echoed on the dry-run short-circuit.

    Keying scope: common_dir — repo_root arg is the .git directory; the caller's
    worktree is main_worktree_root(repo_root).
    """
    if repo_root is None:
        return _error(
            "commit.exec_bit_change requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _error("params.path is required and must be a non-empty string")
    # git's plumbing output (diff --cached --name-only, ls-files) is always
    # /-separated repo-relative; normalize the param once so a Windows caller's
    # backslash path compares equal.
    rel_path = raw_path.strip().replace("\\", "/")

    dry_run = bool(params.get("dry_run", False))

    d3_mismatch = check_repo_root(params.get("repo_root"), repo_root)
    if d3_mismatch is not None:
        return _error(d3_mismatch)

    worktree_root = main_worktree_root(repo_root)

    # ------------------------------------------------------------------
    # 1. Idempotency short-circuit: staged mode already executable → no-op.
    # ------------------------------------------------------------------
    mode, mode_err = _staged_mode(worktree_root, rel_path)
    if mode_err is not None:
        return _error(mode_err)
    if mode is None:
        return _error(
            f"path {rel_path!r} is not tracked (no index entry) — "
            f"commit.exec_bit_change only promotes an already-tracked file's mode"
        )
    if mode == _MODE_EXECUTABLE:
        return {"committed": False, "sha": None, "already_executable": True}
    if mode != _MODE_REGULAR:
        # Symlink (120000), gitlink (160000), or anything else: promoting its
        # mode is undefined for this op — CC-7 fail-loud, never guess.
        return _error(
            f"path {rel_path!r} has index mode {mode}, not {_MODE_REGULAR} — "
            f"exec-bit promotion is defined only for regular files (CC-7 fail-loud)"
        )

    # ------------------------------------------------------------------
    # 2. Foreign-staged-entries guard (B2 AMENDMENT). Unrestricted commit under
    #    live concurrency is the safe-commit stomp hazard: it commits EVERYTHING
    #    staged. Any index entry other than the target → fail loud, name them,
    #    touch nothing (never sweep, never reset/unstage — DEC-3).
    # ------------------------------------------------------------------
    foreign, guard_err = _foreign_staged_entries(worktree_root, rel_path)
    if guard_err is not None:
        return _error(guard_err)
    if foreign:
        return _error(
            "index holds staged entries other than the target path — refusing "
            "the unrestricted commit rather than sweeping a concurrent "
            "session's staged work (and refusing to reset/unstage it — DEC-3): "
            + ", ".join(foreign),
            foreign_staged=foreign,
        )

    # ------------------------------------------------------------------
    # 3. dry_run: everything resolved and validated; zero writes (CC-6).
    # ------------------------------------------------------------------
    if dry_run:
        return {
            "committed": False,
            "sha": None,
            "already_executable": False,
            "dry_run": True,
        }

    # ------------------------------------------------------------------
    # 4. Stage the mode and re-verify it landed (DR-151 step).
    # ------------------------------------------------------------------
    chmod = _git(["update-index", "--chmod=+x", "--", rel_path], cwd=worktree_root)
    if not chmod.ok:
        return _error(
            f"git update-index --chmod=+x failed (exit {chmod.returncode}): "
            f"{chmod.stderr.strip()}"
        )

    staged_after, verify_err = _staged_mode(worktree_root, rel_path)
    if verify_err is not None:
        return _error(f"post-chmod staged-mode re-verify failed: {verify_err}")
    if staged_after != _MODE_EXECUTABLE:
        return _error(
            f"post-chmod re-verify: staged mode for {rel_path!r} is "
            f"{staged_after!r}, expected {_MODE_EXECUTABLE} — refusing to commit "
            f"an unverified mode (CC-7 fail-loud)"
        )

    # ------------------------------------------------------------------
    # 5. Commit WITHOUT path restriction — the DR-151 point. A path-restricted
    #    commit silently resets the staged mode under core.fileMode=false.
    #    This branch runs on every platform; there is no POSIX variant.
    # ------------------------------------------------------------------
    commit = _git(
        ["commit", "-m", f"chore: restore executable bit on {rel_path} (DR-151)"],
        cwd=worktree_root,
    )
    if not commit.ok:
        return _error(
            f"git commit failed (exit {commit.returncode}): {commit.stderr.strip()}"
        )

    head = _git(["rev-parse", "HEAD"], cwd=worktree_root)
    if not head.ok:
        return _error(
            f"commit landed but git rev-parse HEAD failed (exit "
            f"{head.returncode}): {head.stderr.strip()}"
        )

    return {
        "committed": True,
        "sha": head.stdout.strip(),
        "already_executable": False,
    }
