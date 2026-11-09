"""Shared git runner for dispatch_emit tests that build a scratch repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags


def _git(args, cwd: Path) -> str:
    """Run ``git <args>`` in ``cwd``; raise on non-zero exit and return stdout."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


def seed_repo(root: Path) -> Path:
    """Create ``root`` as a git repo with one seed commit and a throwaway identity."""
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root
