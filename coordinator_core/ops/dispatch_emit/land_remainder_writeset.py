"""Write-set legs of ``dispatch.land_verified_remainder``: refuse requested paths outside the run's
declared-write union, and split the declared dirty paths into those to stage and those left out.

The union is the commit phase's own derivation (``pathspec._declared_paths`` plus ``writes_under``
prefixes) over ``read_spine`` of the run's plan. Nothing here stages, commits or writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.dirty_write_set import (
    REGENERABLE_NOTE,
    DirtyWriteSetError,
    _default_ignorecase,
    _dirty_in_write_set,
    _matches_any,
    _norm,
    declared_generated_outputs,
)
from coordinator_core.ops.dispatch_emit.pathspec import _declared_paths
from coordinator_core.ops.dispatch_emit.run_write_set import _plan_rel, _sizing_rel
from coordinator_core.ops.dispatch_emit.spine_read import read_spine


def manifest(run_dir: Path) -> dict:
    try:
        data = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _union(root: Path, run_id: str) -> tuple[list[str], list[str]]:
    """``(declared paths, writes_under prefixes)`` of the run's plan, normalized and sorted.

    Raises ValueError when the run's plan cannot be resolved."""
    run_dir = record_dir(root, run_id)
    plan_rel = _plan_rel(root, manifest(run_dir), _sizing_rel(run_dir))
    if not plan_rel:
        raise ValueError(f"run {run_id!r} names no plan: no manifest plan key and no sizing plan line")
    files: set[str] = set()
    prefixes: set[str] = set()
    for row in read_spine(root / plan_rel):
        files.update(_norm(p) for p in _declared_paths(row))
        prefixes.update(_norm(p) for p in row.writes_under)
    files.discard("")
    prefixes.discard("")
    return sorted(files), sorted(prefixes)


def refuse_out_of_write_set(root: Path, run_id: str, paths: list[str]) -> list[str]:
    """The requested ``paths`` outside the run's declared-write union, in request order, each as given.

    A path is in when it equals a declared path or sits under a ``writes_under`` prefix."""
    files, prefixes = _union(Path(root), run_id)
    fold = (lambda s: s.lower()) if _default_ignorecase() else (lambda s: s)
    exact = {fold(f) for f in files}
    under = [fold(p) for p in prefixes]
    out: list[str] = []
    for path in paths:
        norm = fold(_norm(path))
        if norm in exact or any(norm == p or norm.startswith(p + "/") for p in under):
            continue
        out.append(path)
    return out


def dirty_declared(root: Path, run_id: str) -> tuple[list[str], list[tuple[str, str]]]:
    """``(to_stage, omitted)`` over the run's declared paths that are modified, staged or untracked.

    ``omitted`` pairs a path matching a declared ``generated_outputs`` glob with its reason; every
    other dirty declared path is in ``to_stage``. Stages nothing. Raises DirtyWriteSetError when git
    cannot answer."""
    root = Path(root)
    files, prefixes = _union(root, run_id)
    dirty: Optional[list[str]] = _dirty_in_write_set([*files, *prefixes], root)
    if dirty is None:
        raise DirtyWriteSetError("the declared-path dirtiness check could not run (git failed)")
    globs = declared_generated_outputs(root)
    fold = _default_ignorecase()
    to_stage: list[str] = []
    omitted: list[tuple[str, str]] = []
    for path in dirty:
        if globs and _matches_any(_norm(path), globs, fold):
            omitted.append((path, REGENERABLE_NOTE))
        else:
            to_stage.append(path)
    return to_stage, omitted
