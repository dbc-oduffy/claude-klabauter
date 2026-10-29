"""Checkpoint refusal for a staged removal of a symbol an outside caller still uses."""

from __future__ import annotations

from pathlib import Path
from typing import List, Mapping, Optional

from coordinator_core.git.run import run_git
from coordinator_core.ops.reverify_callers import callers_of, removed_symbols
from coordinator_core.pyimports import module_name_for_file


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _candidate_paths(root: Path, names: List[str]) -> List[str]:
    """Tracked and untracked (non-ignored) .py paths containing any of `names` (one git grep)."""
    argv = ["-C", str(root), "grep", "--untracked", "-l", "-F"]
    for name in names:
        argv += ["-e", name]
    argv += ["--", "*.py"]
    res = run_git(argv)
    if res.returncode not in (0, 1):
        return []
    return [_norm(line) for line in res.stdout.splitlines() if line.strip()]


def removed_symbol_refusal(
    root: Path,
    staged: Mapping[str, Optional[bytes]],
    head_bytes: Mapping[str, Optional[bytes]],
) -> Optional[str]:
    """Refusal text when `staged` removes a top-level symbol that a .py file
    outside `staged` still uses; None otherwise.

    `staged` maps repo-relative path to the bytes to commit (None = deletion);
    `head_bytes` maps the same paths to their HEAD bytes (None = absent).
    """
    staged_norm = {_norm(p): b for p, b in staged.items()}
    removed_by_path = {}
    for path, after in staged_norm.items():
        if not path.endswith(".py"):
            continue
        before = head_bytes.get(path)
        if before is None:
            before = head_bytes.get(path.replace("/", "\\"))
        if before is None:
            continue
        removed = removed_symbols(before, after if after is not None else b"")
        if removed:
            removed_by_path[path] = removed
    if not removed_by_path:
        return None

    names = sorted({n for s in removed_by_path.values() for n in s})
    sources = {}
    for cand in _candidate_paths(root, names):
        if cand in staged_norm:
            continue
        try:
            sources[cand] = (Path(root) / cand).read_bytes()
        except OSError:
            continue
    if not sources:
        return None

    for path in sorted(removed_by_path):
        rows = callers_of(module_name_for_file(path), removed_by_path[path], sources)
        if rows:
            row = rows[0]
            return (
                f"refusing checkpoint: removes {row['symbol']} from {path}; "
                f"still used by {row['caller']}. "
                "Stage the caller's update in this checkpoint."
            )
    return None
