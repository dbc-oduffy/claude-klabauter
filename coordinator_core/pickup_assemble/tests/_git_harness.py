"""One implementation of the throwaway-git-repo harness these suites share.

Purpose: `_isolated_git_env` / `_git` / `_init_repo` were copy-pasted into 22
test modules across `pickup_assemble/tests/` and `coordinator_core/tests/`
(coordinator:overengineering-reviewer, 2026-09-03). They had already drifted
into THREE distinct implementations, which is the cost the duplication was
always going to charge:

  - 18 files carried the version below.
  - 3 files hand-rolled `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)`
    instead of the repo's own `win_portability.no_console_creationflags()` seam,
    and one of those ALSO dropped `GIT_TERMINAL_PROMPT=0` — so a git invocation
    in that suite could block on a credential prompt instead of failing fast.
  - 1 file carried the hand-rolled `creationflags` alone.

Nothing detected that drift, because each copy passed its own suite. Collapsing
them onto the majority implementation is therefore a fix, not only tidying: it
restores the console-suppression seam and the terminal-prompt guard everywhere.

Negative-spec: this module is test scaffolding and must not grow into a general
git façade. It exists to build a disposable repo in `tmp_path` and run plumbing
against it. Anything a PRODUCTION caller needs belongs in `coordinator_core.git`,
which is the real seam and is already published; this file is not.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

__all__ = ["git", "init_repo"]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    """Environment that pins git away from the developer's real config.

    Points both `GIT_CONFIG_GLOBAL` and `GIT_CONFIG_SYSTEM` at an empty file
    under *anchor* so the box's own `~/.gitconfig` cannot influence a test, and
    sets `GIT_TERMINAL_PROMPT=0` so a git call that wants credentials fails
    immediately rather than hanging a suite on an invisible prompt.
    """
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run one git command against *repo*, captured and never interactive."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def init_repo(repo: Path) -> None:
    """Create *repo* as a git repo on a work-shaped branch, identity configured.

    The branch name matters: several guards under test refuse to act on `main`,
    so a harness that defaulted to it would exercise the refusal path rather
    than the behaviour the suite is after.
    """
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-b", "work/test/2026-01-01")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    # Review: coordinator:code-reviewer — every one of the 21 inline
    # `_init_repo` implementations this harness replaced ended with a real
    # "init" commit, so `git rev-list --count HEAD` had a born HEAD to count.
    # Without it, `rev-list --count HEAD` on an unborn branch exits non-zero
    # with empty stdout, making before/after `_rev_count()` comparisons in
    # consumers like test_drop_holder_gate.py vacuously equal ("" == "")
    # instead of proving anything.
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "init")
