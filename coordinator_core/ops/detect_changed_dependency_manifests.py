"""
coordinator_core.ops.detect_changed_dependency_manifests — JSON-RPC
"dependency.detect_changed_manifests" operation.

Purpose: a read-only, two-stage gate that decides whether a repo is worth
dispatching a CVE/dependency auditor against. Stage 1 asks "does this repo
track any dependency-manifest files at all?" (`has_manifests`); stage 2, run
only when stage 1 is True, asks "have any of them changed in the last
`since_days` days?" (`changed_recently`, `changed_files`). Port of the
`workweek-complete` fence's inline `git ls-files` presence check + `git log
--since --name-only` change-detection + `sort -u` shell pipeline (see
`state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv`,
row `detect-changed-dependency-manifests`).

Op-key / contract (frozen — see
`state/audits/2026-07-22-command-payload-inventory/op-classification.tsv`):
    dependency.detect_changed_manifests
    params:   {repo_root: str, since_days: int = 14}
    response: {has_manifests: bool, changed_recently: bool,
               changed_files: list[str]}

Scope-verdict `show_top`: the tracked-manifest set and recent-change window
reflect whichever worktree's checkout is asking — this op always answers
for the CALLING worktree, never a shared `common_dir` key, so a repo with
multiple linked worktrees never gets one worktree's answer misapplied to
another.

Negative-spec:
    - Read-only. Never runs a mutating git command (no add/commit/stash/
      checkout/reset) — `git ls-files` and `git log` only.
    - Stage 2 is skipped entirely (not merely reported False) when stage 1
      is False — mirrors the oracle's two-stage short-circuit, and avoids
      running `git log` against every commit in a repo that never tracked
      a manifest to begin with.
    - Not a git repo (or `git` unavailable) is NOT an error: both stages
      degrade to `False`/`[]` — a directory that isn't a git worktree
      simply has nothing to gate on.
    - Matching is by tracked-file BASENAME against a fixed cross-ecosystem
      manifest/lockfile glob list (module-level `_MANIFEST_GLOB_PATTERNS`),
      not a hardcoded absolute path list — a manifest nested under any
      subdirectory still counts.
    - `changed_files` is de-duplicated and sorted (mirrors the oracle's
      trailing `sort -u`), not insertion-ordered.

Idempotency (AC7): manifest row rates BOTH idempotency-hazard and
platform-hazard "none" — this is a pure read (two git queries, no writes),
so a second invocation against identical repo state returns a
byte-identical result; no DEC-7 note is required.
"""

from __future__ import annotations

import fnmatch
import posixpath
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op

_DEFAULT_SINCE_DAYS = 14

# Cross-ecosystem dependency-manifest / lockfile basenames this detector
# gates on. Matched against `git ls-files`/`git log --name-only` basenames
# via fnmatch, so a manifest under any subdirectory still counts.
_MANIFEST_GLOB_PATTERNS: Tuple[str, ...] = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "requirements*.txt",
    "Pipfile",
    "Pipfile.lock",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
    "composer.json",
    "composer.lock",
)

_GIT_TIMEOUT_SECONDS = 15


def _is_manifest_path(path: str) -> bool:
    """True iff `path`'s basename matches one of `_MANIFEST_GLOB_PATTERNS`.

    Git always reports paths with forward slashes (even on Windows), so the
    basename split uses `posixpath`, never `os.path`, to stay correct
    regardless of host OS.
    """
    basename = posixpath.basename(path)
    return any(fnmatch.fnmatch(basename, pattern) for pattern in _MANIFEST_GLOB_PATTERNS)


def _run_git(repo_root: Path, args: List[str]) -> Optional[str]:
    """Run a read-only `git` subprocess in `repo_root`; return stdout on
    success, None on any failure (not a git repo, `git` missing, no
    commits yet, timeout) — every failure mode collapses to "nothing to
    report" rather than raising, per the module negative-spec.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _run_git: proc = subprocess.run(...) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def tracked_manifest_files(repo_root: Path) -> List[str]:
    """Stage 1 I/O: list every git-tracked path under `repo_root` whose
    basename matches a manifest pattern. Empty list when `repo_root` is not
    a git worktree, has no tracked files, or tracks no manifest.
    """
    stdout = _run_git(repo_root, ["ls-files"])
    if stdout is None:
        return []
    return sorted({path for path in stdout.splitlines() if path and _is_manifest_path(path)})


def changed_manifest_files_since(repo_root: Path, since_days: int) -> List[str]:
    """Stage 2 I/O: list git-tracked manifest paths touched by any commit
    in the last `since_days` days. Empty list when `repo_root` is not a
    git worktree, has no commits yet, or no manifest path was touched in
    the window.
    """
    stdout = _run_git(
        repo_root,
        ["log", f"--since={since_days} days ago", "--name-only", "--pretty=format:"],
    )
    if stdout is None:
        return []
    return sorted({path for path in stdout.splitlines() if path and _is_manifest_path(path)})


def detect(repo_root: Path, since_days: int = _DEFAULT_SINCE_DAYS) -> dict:
    """Pure-ish two-stage gate over an already-resolved `repo_root`.

    Stage 2 (`changed_manifest_files_since`) is only invoked when stage 1
    (`tracked_manifest_files`) found at least one manifest — mirrors the
    oracle's short-circuit and skips walking commit history for a repo
    that never tracked a manifest to begin with.
    """
    manifests = tracked_manifest_files(repo_root)
    if not manifests:
        return {"has_manifests": False, "changed_recently": False, "changed_files": []}

    changed = changed_manifest_files_since(repo_root, since_days)
    return {
        "has_manifests": True,
        "changed_recently": bool(changed),
        "changed_files": changed,
    }


def _resolve_since_days(raw: object) -> int:
    """Coerce `params["since_days"]` to int, falling back to the default on
    anything non-coercible (mirrors the standalone `python3 -m` invocation
    path, which can plausibly hand this through as a string).
    """
    if raw is None:
        return _DEFAULT_SINCE_DAYS
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_SINCE_DAYS


@register_op("dependency.detect_changed_manifests")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'dependency.detect_changed_manifests' handler.

    Plain sync `def`, not `async def`: `detect()` calls blocking
    `subprocess.run(..., timeout=15)` (up to twice, via `_run_git`) for
    `git ls-files` / `git log`. ipc.py's register_op contract (AC-3
    Gap-3) requires this be either sync (auto-offloaded to
    asyncio.to_thread by the engine) or the blocking call explicitly
    wrapped in asyncio.to_thread if kept async — sync is simpler here
    since the whole handler body is blocking.
    (review: code-reviewer — Finding 1, async-handler-blocking-subprocess)

    `params["repo_root"]` (contractual, per the frozen op-classification
    contract column) is the primary source of the repo to scan — this op is
    fired from a fence naming an explicit target repo, not necessarily the
    calling worktree. Falls back to the dispatch-supplied `repo_root` kwarg
    (scope-verdict `show_top` — the calling worktree), then `Path.cwd()` for
    the standalone `python3 -m` invocation path.
    """
    param_repo_root = params.get("repo_root")
    if param_repo_root:
        root = Path(param_repo_root)
    elif repo_root is not None:
        root = Path(repo_root)
    else:
        root = Path.cwd()

    since_days = _resolve_since_days(params.get("since_days"))
    return detect(root, since_days)
