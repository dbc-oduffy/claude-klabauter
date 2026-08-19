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

import fnmatch
import pathlib
import re
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

#: Directories that are publish-STAGING leftovers, not shipped payload --
#: `coordinator/bin/publish.py::_create_publish_staging_dir` mints these
#: destination-adjacent as `.{dest_dir.name}.publish-staging-<random>`
#: (`tempfile.mkdtemp(prefix=..., dir=dest_dir.parent)`), untracked and NOT
#: covered by any `.gitignore` entry in this repo, so `_git_files`'s
#: `--others --exclude-standard` genuinely returns them. A checker that
#: scans them fails the round on bytes that never ship.
#:
#: Same exclusion, same over-match trap, as the engine's own two copies --
#: `coordinator_core/percolate/store.py::_PUBLISH_STAGING_DIR_RE` (matched via
#: `.search()` against a directory NAME, unanchored -- correct here because the
#: mint prefix embeds `dest_dir.name` before the literal substring, e.g.
#: `.bin.publish-staging-gr7j6dpy`, so an anchored `^\.?publish-staging-` would
#: silently fail to match) and `coordinator_core/percolate/engine.py`'s
#: `_PARSE_SWEEP_STAGING_DIR_RE`. Mirrored as an unanchored substring pattern
#: to match `store.py`'s semantics, not `engine.py`'s anchored one.
#:
#: Matched against a DIRECTORY COMPONENT only, never a file's own basename --
#: `SKIP_DIR_NAMES` filtering below already slices `p.split("/")[:-1])` for
#: exactly this reason, so a genuine shipped payload file whose basename
#: happens to contain `publish-staging-` is still scanned.
PUBLISH_STAGING_DIR_RE = re.compile(r"publish-staging-")

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


def _parse_gitignore_fallback_patterns(root: pathlib.Path) -> tuple[set[str], list[str]]:
    """Minimal, dependency-free ``.gitignore`` reader for the ``_walk_files``
    fallback (a tree with no ``.git``, so ``git ls-files --exclude-standard``
    is unavailable).

    NOT a general gitignore engine -- deliberately covers only the two
    pattern shapes this repo's own ``.gitignore`` actually uses: a bare
    directory name (``state/``) and an unslashed basename glob (``*.bak``,
    ``.DS_Store``). No negation (``!``), no ``**``, no anchored (``/foo``) or
    nested-slash pattern. The git-aware path (``_git_files``, real gitignore
    semantics via ``git ls-files --exclude-standard``) stays authoritative
    whenever a real ``.git`` is present; this only closes the specific gap
    where the fallback walk sees a git-less copy of a tree (e.g. a percolate
    publish staging directory -- ``shutil.copytree`` excludes only ``.git``,
    never gitignored content) whose own tracked ``.gitignore`` already
    declares these paths unpublishable. Root cause + evidence:
    state/audits/2026-08-13-persona-guard-staging-gitignore-gap.md.

    Returns (dir_names, basename_patterns): directory names to exclude at
    any depth, and basename glob patterns (``fnmatch``-compatible) to
    exclude regardless of depth.
    """
    gitignore_path = root / ".gitignore"
    dir_names: set[str] = set()
    basename_patterns: list[str] = []
    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return dir_names, basename_patterns
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!") or "/" in line.rstrip("/"):
            # Negation and anchored/nested patterns are out of scope for this
            # minimal matcher -- unmatched entries simply are not filtered
            # here (fail toward MORE scanning, never less).
            continue
        if line.endswith("/"):
            dir_names.add(line[:-1])
        else:
            basename_patterns.append(line)
    return dir_names, basename_patterns


def _walk_files(root: pathlib.Path) -> list[str]:
    ignored_dir_names, ignored_basename_patterns = _parse_gitignore_fallback_patterns(root)
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
                if entry.name in SKIP_DIR_NAMES or entry.name in ignored_dir_names:
                    continue
                stack.append(entry)
                continue
            if entry.suffix in SKIP_SUFFIXES:
                continue
            if any(fnmatch.fnmatch(entry.name, pat) for pat in ignored_basename_patterns):
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
        and not any(PUBLISH_STAGING_DIR_RE.search(part) for part in p.split("/")[:-1])
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
