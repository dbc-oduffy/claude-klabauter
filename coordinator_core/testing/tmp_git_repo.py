"""Committing-test git helper: every call is pinned to one tmp repo and refuses any other root."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

_ENGINE_REPO = Path(__file__).resolve().parents[2]


class RefusedRepoRoot(RuntimeError):
    """The resolved root is not a private tmp repo this helper may write to."""


def _check_root(root: Path, *, init: bool) -> Path:
    resolved = Path(root).resolve()
    if resolved == _ENGINE_REPO:
        raise RefusedRepoRoot(f"{resolved} is a real fleet repo; a test repo lives under tmp_path")
    if not init and not (resolved / ".git").exists():
        raise RefusedRepoRoot(
            f"{resolved} has no .git of its own; git would resolve the enclosing repo"
        )
    return resolved


def tmp_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` in `root`, pinned with GIT_DIR/GIT_WORK_TREE to `root`; refuses the engine repo and a root with no `.git` of its own."""
    resolved = _check_root(root, init=args[:1] == ("init",))
    env = {
        **os.environ,
        "GIT_DIR": str(resolved / ".git"),
        "GIT_WORK_TREE": str(resolved),
    }
    return subprocess.run(
        ["git", *args],
        cwd=str(resolved),
        env=env,
        capture_output=True,
        text=True,
        check=check,
        **no_console_creationflags(),
    )


def init_tmp_git_repo(root: Path, *, identity: str = "tmp-git-repo-test") -> None:
    """Seed `root` as an origin-tracking repo with one commit; a no-op when it already has a `.git`."""
    resolved = _check_root(root, init=True)
    if (resolved / ".git").is_dir():
        return
    tmp_git(resolved, "init", "-b", "main")
    tmp_git(resolved, "config", "user.email", f"{identity}@claude-klabauter.test")
    tmp_git(resolved, "config", "user.name", identity)
    tmp_git(resolved, "config", "commit.gpgsign", "false")
    (resolved / ".gitkeep").write_text("", encoding="utf-8")
    tmp_git(resolved, "add", ".gitkeep")
    tmp_git(resolved, "commit", "-m", "chore: init")
    tmp_git(resolved, "remote", "add", "origin", str(resolved))
    tmp_git(resolved, "fetch", "--no-tags", "origin")
    tmp_git(resolved, "branch", "--set-upstream-to=origin/main", "main")
