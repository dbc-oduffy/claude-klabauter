"""Zero-spawn predicate: is a commit about to land on a published `main`?

Pure file reads (HEAD, common-dir config); shared by the ops commit routes and
the Bash guard layer. `git/` never imports `ops/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_branch

REMEDY = "workday-start-day-branch-resolve day-branch-assert"


def _has_remote(repo_root: Union[str, Path]) -> bool:
    try:
        text = (resolve_git_common_dir(repo_root) / "config").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[remote") and '"' in s:
            return True
    return False


def main_commit_refusal(
    repo_root: Union[str, Path], *, override_reason: Optional[str]
) -> Optional[str]:
    """Refusal text when HEAD is exactly `main` and a remote is configured; else None.

    A non-empty string `override_reason` admits; any other type is ignored. A
    remote-less repo, a work branch and a detached HEAD are never refused.
    Zero git spawns.
    """
    if isinstance(override_reason, str) and override_reason.strip():
        return None
    if head_branch(repo_root) != "main":
        return None
    if not _has_remote(repo_root):
        return None
    name = Path(repo_root).resolve().name
    return (
        f"commit refused: {name} is on main; run `{REMEDY}` "
        "to re-enter the day branch, or pass a main-commit override reason."
    )
