"""Throwaway-repo helpers shared by the authoring_leaks tests that build real git repos under tmp_path."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags


def git(root: Path, *args: str) -> None:
    """Run `git <args>` in `root` with a fixed identity and no signing; raises on a non-zero exit."""
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def write(root: Path, rel: str, text: str) -> None:
    """Write `text` as UTF-8 bytes (no newline translation) to `root/rel`, creating parents."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
