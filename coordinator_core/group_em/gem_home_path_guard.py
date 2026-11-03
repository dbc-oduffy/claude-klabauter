"""Advisory scan for absolute machine paths in the G-EM home, and its pre-commit hook.

The home is synced across boxes, so a tracked file holding a drive-letter or
``/Users/``/``/home/`` root is wrong on every other box. ``scan`` finds them;
``install`` writes a naked-Python pre-commit hook that prints one advisory line per
hit and always exits 0 (flags, never denies).
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

HOOK_MARKER = "# gem-home-path-guard"

# Shared by scan() and the generated hook so the two cannot drift.
_DRIVE = r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`<>|)]*"
_POSIX = r"(?<![\w.~$-])/(?:Users|home)/[^\s\"'`<>|)]*"
PATH_PATTERN = f"(?:{_DRIVE})|(?:{_POSIX})"
_PATH_RE = re.compile(PATH_PATTERN)


def _scan_text(text: str) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _PATH_RE.finditer(line):
            hits.append((lineno, m.group(0)))
    return hits


def scan(paths) -> list[tuple[str, int, str]]:
    """Return ``(path, line, match)`` for each absolute machine path in the given text files.

    Unreadable, missing and binary (NUL-bearing or non-UTF-8) files are skipped.
    """
    out: list[tuple[str, int, str]] = []
    for p in paths:
        try:
            data = Path(p).read_bytes()
            if b"\x00" in data:
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, match in _scan_text(text):
            out.append((str(p), lineno, match))
    return out


_HOOK_TEMPLATE = '''#!/usr/bin/env python3
__MARKER__
"""Advisory: flag absolute machine paths in staged files. Always exits 0."""
import re
import subprocess
import sys

PATTERN = re.compile(__PATTERN__)


def _git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def main():
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACM", "-z").stdout.split(b"\\0")
    for raw in names:
        if not raw:
            continue
        path = raw.decode("utf-8", "replace")
        blob = _git("show", ":" + path).stdout
        if b"\\0" in blob:
            continue
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in PATTERN.finditer(line):
                print(f"gem-home path guard: {path}:{lineno}: absolute machine path {m.group(0)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
'''


def hook_source() -> str:
    return _HOOK_TEMPLATE.replace("__MARKER__", HOOK_MARKER).replace("__PATTERN__", repr(PATH_PATTERN))


def install(home) -> Path | None:
    """Write the pre-commit hook into ``<home>/.git/hooks``; return its path.

    Returns None when ``home`` is not a git repo or holds a foreign pre-commit hook
    (never clobbered). Re-running over our own hook rewrites it.
    """
    hooks = Path(home) / ".git" / "hooks"
    if not (Path(home) / ".git").is_dir():
        return None
    hook = hooks / "pre-commit"
    if hook.exists():
        try:
            if HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
                return None
        except OSError:
            return None
    hooks.mkdir(parents=True, exist_ok=True)
    hook.write_text(hook_source(), encoding="utf-8", newline="\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook
