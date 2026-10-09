"""Emit-time refusal over uncommitted work in a wave's union write set.

The union is the commit phase's own derivation (declared `writes:`, the
concrete-`surface:` fallback, `writes_under` prefixes) over the rows
`read_spine` returns. One scoped `git status` call reads only that union;
dirt outside it never refuses, and a git failure refuses (fail closed).
"""

from __future__ import annotations

import subprocess
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.git.literal_pathspec import git_pathspec
from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.pathspec import _declared_paths
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.resolve_validation_cmd import cs_read_local_md_key


class DirtyWriteSetError(ValueError):
    """The wave's union write set holds uncommitted work, or the check could not run."""


def _norm(path: str) -> str:
    """Forward-slash form with a leading `./` and trailing `/` removed."""
    out = path.replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out.rstrip("/")


def _default_ignorecase() -> bool:
    return sys.platform in ("win32", "darwin")


def _dirty_in_write_set(
    paths,
    repo_root: Path,
    run: Optional[Callable] = None,
    ignorecase: Optional[bool] = None,
) -> Optional[list[str]]:
    """Return the sorted subset of `paths` that is modified, staged, or untracked.

    One scoped `git status` spawn; `[]` without spawning for an empty `paths`;
    `None` when git raised or exited non-zero (the caller fails closed). A path
    counts when it equals a pathspec or sits under one as a directory prefix.
    Declared paths are compared in forward-slash form; with `ignorecase` (default:
    Windows and macOS, whose git sets core.ignorecase) the pathspecs carry
    `:(icase,literal)` — git's own pathspec match stays case-sensitive otherwise —
    and the comparison folds case.
    `run` is a test seam shaped like `run_git(args, timeout=)`; omitted, the shared `run_git` runs.
    """
    ordered = sorted({_norm(p) for p in paths if _norm(p)})
    if not ordered:
        return []
    if ignorecase is None:
        ignorecase = _default_ignorecase()
    specs = [f":(icase,literal){p}" for p in ordered] if ignorecase else [git_pathspec(p) for p in ordered]
    argv = [
        "-C", str(repo_root), "--no-optional-locks", "status",
        "--porcelain", "--untracked-files=all", "--", *specs,
    ]
    try:
        proc = (run or run_git)(argv, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None

    fold = (lambda s: s.lower()) if ignorecase else (lambda s: s)
    prefixes = [fold(p) for p in ordered]
    dirty: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if len(line) < 4:
            continue
        entry = line[3:]
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"').replace("\\", "/")
        folded = fold(entry)
        if any(folded == p or folded.startswith(p + "/") for p in prefixes):
            dirty.add(entry)
    return sorted(dirty)


GENERATED_OUTPUTS_KEY = "generated_outputs"
REGENERABLE_NOTE = "regenerable, owner unknown: the row that writes it regenerates and commits it"
_EXAMPLES = 5


def declared_generated_outputs(repo_root: Path) -> list[str]:
    """Globs from the `generated_outputs` key of `coordinator.local.md`.

    The value is a comma-separated list (an optional `[...]` wrapper and
    per-item quotes are stripped); absent or empty means no declaration.
    """
    raw = cs_read_local_md_key(str(repo_root), GENERATED_OUTPUTS_KEY).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [g for g in (_norm(p.strip().strip("\"'")) for p in raw.split(",")) if g]


def _matches_any(path: str, globs: list[str], ignorecase: bool) -> bool:
    """`*` and `**` both cross `/`, so `data/serving/**` covers every depth."""
    folded = path.lower() if ignorecase else path
    return any(fnmatchcase(folded, g.lower() if ignorecase else g) for g in globs)


def guard_against_dirty_write_set(
    plan_path, repo_root: Path, *, run=None, ignorecase=None, hold=frozenset(), landed=frozenset()
) -> Optional[dict]:
    """Refuse emission when a path the dispatchable rows will write and commit
    is already dirty or untracked — before any script or brief reaches disk.

    Dirty paths matching a declared `generated_outputs` glob do not refuse;
    they come back as `{"count", "examples", "note"}` (else `None`).

    Rows this emit will not dispatch are out of the write set: `hold` rows and
    their transitive dependents, and `landed` rows (an `--only-incomplete` re-emit).

    Negative spec: reads only the write-set union through one scoped porcelain
    call — never the unscoped tree, never claims.
    """
    rows = read_spine(Path(plan_path))
    skipped = set(hold) | set(landed)
    if hold:
        from coordinator_core.ops.dispatch_emit.wave_map import transitive_dependents

        skipped.update(transitive_dependents(rows, set(hold)))
    union: set[str] = set()
    for row in rows:
        if row.id in skipped:
            continue
        union.update(_declared_paths(row))
        union.update(row.writes_under)
    dirty = _dirty_in_write_set(sorted(union), Path(repo_root), run=run, ignorecase=ignorecase)
    regenerable: list[str] = []
    if dirty:
        globs = declared_generated_outputs(Path(repo_root))
        if globs:
            fold = _default_ignorecase() if ignorecase is None else ignorecase
            regenerable = [d for d in dirty if _matches_any(_norm(d), globs, fold)]
            dirty = [d for d in dirty if d not in set(regenerable)]
    if dirty is None:
        raise DirtyWriteSetError(
            "emission refused -- the write-set dirtiness check could not run "
            "(git failed); an unanswerable check is not a clean one. Fix git "
            "access to the repo, then re-emit."
        )
    if dirty:
        raise DirtyWriteSetError(
            "emission refused -- these paths in the wave's write set are already "
            "modified, staged, or untracked, and the commit phase would sweep them "
            "into this plan's commit:\n  - "
            + "\n  - ".join(dirty)
            + "\nCommit or reconcile them with their owner, then re-emit."
        )
    if regenerable:
        return {"count": len(regenerable), "examples": regenerable[:_EXAMPLES], "note": REGENERABLE_NOTE}
    return None
