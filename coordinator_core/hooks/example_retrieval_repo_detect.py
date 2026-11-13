"""
coordinator_core.hooks.example_retrieval_repo_detect — SessionStart banner: generic
Example-retrieval-repo freshness (Port of: example-retrieval-repo-detect.sh, DoE d39ab164,
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
  ""            — kill-switch set, example-game-repo context detected, or no index
                  marker found walking up from cwd (see find_example_retrieval_repo_dir)

Disposition: PORT (the Staff Engineer adjudication item, W4b §2.8). Both `.ps1` and `.sh`
oracles implement IDENTICAL logic — a bounded upward directory walk for two
marker files, a single file-mtime stat, and two `git` subprocess calls. There
is no pwsh-only primitive (no registry read, no WMI query) anywhere in the
oracle pair, so this ports cleanly; nothing here falls back to stays-bash.

Kill-switch: env COORDINATOR_HOOK_PROJECT_RAG_DETECT_DISABLED=1 disables.

Example-Game-Repo dedupe: `.example-game-repo/` or `Saved/ExampleGameRepoProjectRag` found walking up
from cwd => silent exit; the example-game-repo-specific hook owns that banner.

Called IN-PROCESS by the thin DoE SessionStart stub
(coordinator/hooks/scripts/example-retrieval-repo-detect.py), mirroring
preuse-write-dispatch.py's direct-import shape — no IPC round trip, no
register_op(); not a mutating op. Its only side effect is the detached refresh
that `rag_freshness.freshness_precondition` queues for a stale or absent index
(advisory, fail-open). An index under the settings home counts as indexed.

Spec backlink: C:/coordinator-content-repo/scratch/subagent-sandbox/bash-to-python-migration/W4a-sessionstart-recipe.md § 2.8
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

_MAX_LEVELS = 6


def _find_marker_upward(start_dir: str, marker: str, max_levels: int = _MAX_LEVELS) -> str | None:
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


_INDEX_MARKERS = ("graph.db", "state.json")


def find_example_retrieval_repo_dir(start_dir: str, max_levels: int = _MAX_LEVELS) -> str | None:
    d = start_dir
    for _ in range(max_levels):
        rag_dir = os.path.join(d, ".project-rag")
        if any(os.path.isfile(os.path.join(rag_dir, m)) for m in _INDEX_MARKERS):
            return rag_dir
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
    return None


def _git(repo_root: str, *args: str) -> str | None:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    try:
        result = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_root_upward(start_dir: str, max_levels: int = 8) -> str | None:
    d = start_dir
    for _ in range(max_levels):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
    return None


def _settings_index_db(repo_root: str) -> str | None:
    """Settings-home graph.db for ``repo_root`` (`project-rag/indexes/<name, '-'->'_'>/root/`)."""
    try:
        from coordinator_core._settings_home import settings_home

        slug = os.path.basename(os.path.abspath(repo_root)).replace("-", "_")
        db = settings_home() / "project-rag" / "indexes" / slug / "root" / "graph.db"
        return str(db) if db.is_file() else None
    except Exception:  # noqa: BLE001 -- locator failure reads as no settings-home index
        return None


def _acted(pre: dict) -> str:
    """The refresh fact the adapter reports, in banner words."""
    if pre.get("refresh_queued"):
        return "refresh queued"
    if "already queued" in str(pre.get("message", "")):
        return "refresh already queued"
    return "refresh not queued"


def detect_banner(cwd: str, *, verdict_fn=None, spawn_fn=None, marker_root=None) -> str:
    """Session-start banner. A stale or absent index queues example-retrieval-repo's refresh
    through ``rag_freshness.freshness_precondition`` and the banner states that
    outcome. The keyword seams pass through to the adapter."""
    if os.environ.get("COORDINATOR_HOOK_PROJECT_RAG_DETECT_DISABLED") == "1":
        return ""

    if _find_marker_upward(cwd, ".example-game-repo") is not None:
        return ""
    if _find_marker_upward(cwd, os.path.join("Saved", "ExampleGameRepoProjectRag")) is not None:
        return ""

    from coordinator_core.ops import rag_freshness

    def _precondition(root: str) -> dict:
        return rag_freshness.freshness_precondition(
            root, verdict_fn=verdict_fn, spawn_fn=spawn_fn, marker_root=marker_root
        )

    example_retrieval_repo_dir = find_example_retrieval_repo_dir(cwd)
    if example_retrieval_repo_dir:
        repo_root = os.path.dirname(example_retrieval_repo_dir)
        db_path = os.path.join(example_retrieval_repo_dir, "graph.db")
    else:
        repo_root = _git_root_upward(cwd)
        if repo_root is None:
            return ""
        db_path = _settings_index_db(repo_root)
        if db_path is None:
            pre = _precondition(repo_root)
            if pre["state"] == "absent":
                return f"project-rag: UNINITIALIZED — no index; {_acted(pre)}"
            return ""

    if not os.path.isfile(db_path):
        return f"project-rag: UNINITIALIZED — no graph.db; {_acted(_precondition(repo_root))}"

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
        return f"project-rag: UNINITIALIZED — graph.db predates git history; {_acted(_precondition(repo_root))}"

    delta_raw = _git(repo_root, "rev-list", f"{built_commit}..HEAD", "--count")
    if delta_raw is None:
        return "example-retrieval-repo: SKIP — git rev-list failed (fail open)"
    try:
        delta = int(delta_raw)
    except ValueError:
        return "example-retrieval-repo: SKIP — git rev-list failed (fail open)"

    if delta == 0:
        return "example-retrieval-repo: fresh (HEAD aligned)"

    acted = _acted(_precondition(repo_root))
    base_msg = (
        f"project-rag: STALE — {delta} commits behind HEAD; {acted}; "
        "example-retrieval-repo queries may miss recent changes"
    )

    if delta > 50 or age_days > 7:
        lines = ["<system-reminder>", base_msg]
        if delta > 50:
            lines.append(f"  WARNING: {delta} commits — index is significantly out of date.")
        if age_days > 7:
            lines.append(f"  WARNING: index is {age_days} days old.")
        lines.append("</system-reminder>")
        return "\n".join(lines)

    return base_msg
