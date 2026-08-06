"""
coordinator_core.hooks.example_retrieval_repo_detect — SessionStart banner: generic
Example-retrieval-repo freshness (Port of: example-retrieval-repo-detect.sh, example-doctrine-repo d39ab164,
2026-07-16 / example-retrieval-repo-detect.ps1, W4b bash-to-python-migration cohort).

Emits one of four banner shapes as a single string (empty string == silent
exit, no stdout):
  fresh         — graph.db mtime >= HEAD commit time
  STALE (N)     — N commits behind HEAD (escalates to a wrapped
                  <system-reminder> block when N > 50 or age > 7 days)
  UNINITIALIZED — marker present but no graph.db, or graph.db predates all
                  git history
  SKIP          — fail-open message when a required probe (stat/git) itself
                  fails
  ""            — kill-switch set, example-game-repo context detected, or no
                  .example-retrieval-repo/manifest.json marker found walking up from cwd

Disposition: PORT (the Staff Engineer adjudication item, W4b §2.8). Both `.ps1` and `.sh`
oracles implement IDENTICAL logic — a bounded upward directory walk for two
marker files, a single file-mtime stat, and two `git` subprocess calls. There
is no pwsh-only primitive (no registry read, no WMI query) anywhere in the
oracle pair, so this ports cleanly; nothing here falls back to stays-bash.

Kill-switch: env COORDINATOR_HOOK_EXAMPLE_RETRIEVAL_REPO_DETECT_DISABLED=1 disables.

Example-Game-Repo dedupe: `.example-game-repo/` or `Saved/ExampleGameRepoProjectRag` found walking up
from cwd => silent exit; the example-game-repo-specific hook owns that banner.

Called IN-PROCESS by the thin example-doctrine-repo SessionStart stub
(coordinator/hooks/scripts/example-retrieval-repo-detect.py), mirroring
preuse-write-dispatch.py's direct-import shape — no IPC round trip, no
register_op(); this is a pure, side-effect-free banner computation, not a
mutating op.

Spec backlink: X:/example-doctrine-repo/scratch/subagent-sandbox/bash-to-python-migration/W4a-sessionstart-recipe.md § 2.8
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

_MAX_LEVELS = 6


def _find_marker_upward(start_dir: str, marker: str, max_levels: int = _MAX_LEVELS) -> str | None:
    """Walk up from start_dir looking for <dir>/<marker> (file or dir).

    Mirrors find_marker_upward() (bash) / Find-MarkerUpward (ps1): checks the
    starting dir itself first (i=0), then up to max_levels-1 additional
    parents, stopping early if the filesystem root is reached (parent == dir).
    """
    d = start_dir
    for _ in range(max_levels):
        candidate = os.path.join(d, marker)
        if os.path.exists(candidate):
            return candidate
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
    return None


def _git(repo_root: str, *args: str) -> str | None:
    """Run `git -C repo_root <args>`, return stripped stdout or None on any failure."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def detect_banner(cwd: str) -> str:
    """Compute the example-retrieval-repo freshness banner for cwd. "" means silent exit."""
    if os.environ.get("COORDINATOR_HOOK_EXAMPLE_RETRIEVAL_REPO_DETECT_DISABLED") == "1":
        return ""

    # --- Example-Game-Repo dedupe: positive context detection ---
    if _find_marker_upward(cwd, ".example-game-repo") is not None:
        return ""
    if _find_marker_upward(cwd, os.path.join("Saved", "ExampleGameRepoProjectRag")) is not None:
        return ""

    # --- Generic example-retrieval-repo detection via marker file ---
    manifest_path = _find_marker_upward(cwd, os.path.join(".example-retrieval-repo", "manifest.json"))
    if not manifest_path:
        return ""

    example_retrieval_repo_dir = os.path.dirname(manifest_path)
    repo_root = os.path.dirname(example_retrieval_repo_dir)

    db_path = os.path.join(example_retrieval_repo_dir, "graph.db")

    if not os.path.isfile(db_path):
        return (
            f"example-retrieval-repo: UNINITIALIZED — marker found at {manifest_path} but "
            "no graph.db; run the example-retrieval-repo indexer before querying"
        )

    try:
        db_mtime_epoch = Path(db_path).stat().st_mtime
    except OSError:
        return "example-retrieval-repo: SKIP — could not stat graph.db (fail open)"

    now_epoch = time.time()
    age_days = int((now_epoch - db_mtime_epoch) // 86400)

    import shutil

    if shutil.which("git") is None:
        return "example-retrieval-repo: SKIP — git not found (fail open)"

    mtime_str = datetime.fromtimestamp(db_mtime_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    built_commit = _git(repo_root, "log", "-1", f"--before={mtime_str}", "--format=%H")
    if not built_commit:
        return "example-retrieval-repo: UNINITIALIZED — graph.db predates git history; run the example-retrieval-repo indexer"

    delta_raw = _git(repo_root, "rev-list", f"{built_commit}..HEAD", "--count")
    if delta_raw is None:
        return "example-retrieval-repo: SKIP — git rev-list failed (fail open)"
    try:
        delta = int(delta_raw)
    except ValueError:
        return "example-retrieval-repo: SKIP — git rev-list failed (fail open)"

    if delta == 0:
        return "example-retrieval-repo: fresh (HEAD aligned)"

    base_msg = (
        f"example-retrieval-repo: STALE — {delta} commits behind HEAD; "
        "example-retrieval-repo queries may miss recent changes"
    )

    if delta > 50 or age_days > 7:
        lines = ["<system-reminder>", base_msg]
        if delta > 50:
            lines.append(
                f"  WARNING: {delta} commits — index is significantly out of date. "
                "Run the example-retrieval-repo indexer to rebuild."
            )
        if age_days > 7:
            lines.append(f"  WARNING: index is {age_days} days old. Run the example-retrieval-repo indexer to rebuild.")
        lines.append("</system-reminder>")
        return "\n".join(lines)

    return base_msg
