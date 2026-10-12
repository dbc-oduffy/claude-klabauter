"""Architecture survey commit: one scoped add and commit of the atlas, and scratch cleanup."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from coordinator_core.architecture_survey.contract import Decision
from coordinator_core.contract.apply_base import _mirror_session_env_for_subprocess
from coordinator_core.git.git_state import head_branch, head_sha
from coordinator_core.git_lock_retry import run_with_lock_retry
from coordinator_core.git.run import run_git

ATLAS_DIR = "docs/architecture/"
LEDGER = "state/health-ledger.md"
SCRATCH_ROOT = Path("state/scratch/deep-architecture-survey")
DEFAULT_BRANCHES: frozenset[str] = frozenset({"main", "master"})
_MODE_LABEL = {"bootstrap": "first run", "refresh": "refresh", "targeted": "targeted"}
_RULE = "scoped-atlas-commit"


def _git(repo_root: Path, args: list[str]) -> Any:
    with _mirror_session_env_for_subprocess():
        return run_with_lock_retry(
            lambda: run_git(args, cwd=str(repo_root), env={**os.environ, "LC_ALL": "C"})
        )


def _subject(mode: str, systems_mapped: int, ledger_stamped: bool, commits_behind: int | None) -> str:
    subject = f"deep-architecture-survey: {_MODE_LABEL[mode]} — {systems_mapped} systems mapped"
    if ledger_stamped:
        subject += "; Last full audit bumped"
    if commits_behind:
        subject += f"; index {commits_behind} behind"
    return subject


def commit_atlas(
    repo_root: Path,
    *,
    mode: str,
    systems_mapped: int,
    ledger_stamped: bool,
    commits_behind: int | None,
    budget: Any,
) -> Decision:
    """Commit the atlas (and, off a targeted run, the health ledger) with explicit pathspecs.

    Refuses on the default branch; returns `no-change` when the pathspecs hold nothing new and
    raises RuntimeError on a git failure. Both git spawns are counted against `budget`.
    """
    if mode not in _MODE_LABEL:
        raise ValueError(f"unknown mode: {mode}")
    repo_root = Path(repo_root)
    branch = head_branch(repo_root)
    if branch in DEFAULT_BRANCHES:
        return Decision("commit", "refused-default-branch", _RULE, f"branch {branch} is the default branch")

    pathspec = ["--", ATLAS_DIR] if mode == "targeted" else ["--", ATLAS_DIR, LEDGER]
    subject = _subject(mode, systems_mapped, ledger_stamped, commits_behind)

    add = _git(repo_root, ["add", *pathspec])
    budget.record_invocation("git")
    if add.returncode != 0:
        raise RuntimeError(f"git add failed (rc={add.returncode}): {add.stderr.strip() or '<no stderr>'}")

    commit = _git(repo_root, ["commit", "-m", subject, *pathspec])
    budget.record_invocation("git")
    if commit.returncode != 0:
        out = f"{commit.stdout}\n{commit.stderr}".lower()
        if "nothing to commit" in out or "no changes added to commit" in out or "nothing added to commit" in out:
            return Decision("commit", "no-change", _RULE, "the run changed no page")
        raise RuntimeError(f"git commit failed (rc={commit.returncode}): {commit.stderr.strip() or '<no stderr>'}")

    sha = head_sha(repo_root) or "unknown"
    return Decision("commit", "committed", _RULE, f"{sha} {subject}")


def cleanup_scratch(repo_root: Path, run_id: str) -> None:
    """Remove `state/scratch/deep-architecture-survey/<run_id>/`; refuse any other target."""
    root = (Path(repo_root) / SCRATCH_ROOT).resolve()
    target = (root / run_id).resolve()
    if not run_id or target.parent != root:
        raise ValueError(f"run id {run_id!r} is not a direct child of {SCRATCH_ROOT}")
    if target.is_dir():
        shutil.rmtree(target)
