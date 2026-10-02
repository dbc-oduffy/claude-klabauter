"""Scan tracked files for unmarked uses of the retired root names.

`unmarked_hits()` is the assertion every area row and the whole-tree gate share.
The pattern is assembled from fragments so this file never matches itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_PATTERN = re.compile("d" "oe[-_]root|(repos|working_repos)\\.d" "oe_", re.IGNORECASE)
_MARKER = "private-name-ok"
_EXCLUDED_PREFIXES = ("archive/", "state/", "docs/", "tasks/", ".coordinator-local/", ".structural-index/")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("utf-8", "replace")
    return [p for p in out.split("\0") if p]


def unmarked_hits(paths=None, *, root: Path | None = None) -> list[tuple[str, int, str]]:
    """(repo-relative path, 1-based line, stripped line) for each unmarked hit.

    `paths=None` scans `git ls-files` (one spawn); a given list spawns nothing. Excluded
    top-level dirs and unreadable or non-UTF-8 files are skipped.
    """
    base = Path(root) if root else _REPO_ROOT
    rels = _tracked(base) if paths is None else [str(p) for p in paths]
    hits: list[tuple[str, int, str]] = []
    for rel in rels:
        path = Path(rel)
        if path.is_absolute():
            try:
                path = path.relative_to(base)
            except ValueError:
                continue
        posix = path.as_posix()
        if posix.startswith(_EXCLUDED_PREFIXES):
            continue
        try:
            text = (base / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _MARKER not in line and _PATTERN.search(line):
                hits.append((posix, lineno, line.strip()))
    return hits
