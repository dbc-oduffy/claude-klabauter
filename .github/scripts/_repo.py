"""Shared repo-walking helpers for the mirror CI checks.

Deliberately named with a leading underscore so `run-all-checks.py`'s
convention-based discovery (`check-*.py`, `validate-*.py`) does not pick it up
as a check in its own right.

Two invariants the whole harness rests on:

REPO ROOT IS ANCHORED TO ``__file__``, NOT TO CWD
    percolate's pre-CI gate invokes ``<dest>/.github/scripts/run-all-checks.py``
    by absolute path from an arbitrary working directory, and a non-zero exit
    fails the publish. Resolving the root from cwd would make the gate's verdict
    depend on where the publisher happened to be standing.

FILE ENUMERATION MUST WORK IN A ZERO-COMMIT REPO
    The mirror is bootstrapped by pushing an initial commit; before that push,
    ``git ls-files --cached`` returns nothing and every content check passes
    vacuously. ``ls-files --cached --others --exclude-standard`` sees both
    tracked and not-yet-committed-but-not-ignored files, so the gate reports on
    the bytes that are about to ship rather than on an empty index. A pure
    filesystem walk is the fallback for a tree that is not a git repo at all
    (e.g. a percolate dry-run staging directory).
"""

from __future__ import annotations

import pathlib
import subprocess

SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    ".eggs",
}

SKIP_SUFFIXES = {".pyc", ".pyo"}

# Portable console suppression for every git subprocess this harness spawns.
# Resolves to CREATE_NO_WINDOW on Windows and to 0 (a no-op) on POSIX; the bare
# constant would raise ValueError off-Windows. Windows is a first-class target
# for this engine, so the harness must not pop a console per git call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def repo_root() -> pathlib.Path:
    """Repository root, anchored at this file's location (.github/scripts/_repo.py)."""
    return pathlib.Path(__file__).resolve().parents[2]


def _git_files(root: pathlib.Path) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others",
             "--exclude-standard", "-z"],
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.split("\0") if p]


def _walk_files(root: pathlib.Path) -> list[str]:
    found: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIR_NAMES:
                    stack.append(entry)
                continue
            if entry.suffix in SKIP_SUFFIXES:
                continue
            found.append(entry.relative_to(root).as_posix())
    return sorted(found)


def repo_files(root: pathlib.Path | None = None) -> list[str]:
    """Repo-relative POSIX paths of every candidate file, git-aware with a walk fallback."""
    root = root or repo_root()
    paths = _git_files(root)
    if paths is None:
        paths = _walk_files(root)
    cleaned = {
        p for p in paths
        if not p.startswith(".git/")
        and not any(part in SKIP_DIR_NAMES for part in p.split("/")[:-1])
        and not p.endswith(tuple(SKIP_SUFFIXES))
    }
    return sorted(cleaned)


def read_text(path: pathlib.Path) -> str | None:
    """Decoded text, or None when the file is binary or unreadable."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def load_allowlist(root: pathlib.Path, name: str) -> set[str]:
    """Load a `.github/<name>` allowlist file as a set of stripped, non-comment lines."""
    path = root / ".github" / name
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            entries.add(stripped)
    return entries


def bootstrap_empty_dirs(root: pathlib.Path, names: list[str]) -> list[str]:
    """Of ``names``, those that exist as directories but contain no files.

    The mirror publishes in stages: `coordinator_core/` lands as an empty
    directory before the engine itself is pushed. Checks consult this so a
    mid-bootstrap tree degrades to "nothing to inspect here" rather than to a
    hard failure — while still failing on any real leak in the bytes that ARE
    present.
    """
    empty = []
    for name in names:
        target = root / name
        if target.is_dir() and not any(p.is_file() for p in target.rglob("*")):
            empty.append(name)
    return empty
