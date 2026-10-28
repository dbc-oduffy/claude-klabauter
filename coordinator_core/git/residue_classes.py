"""Engine-residue and build-churn path classes for the commit helper's advisory lists.

Engine residue is dirty output the engine writes outside a run's landing pathspec; build churn is
generated build output no session owns. Both are pure path classes; `dirty_engine_residue` is the
one in-process worktree read and never spawns git.
"""

import fnmatch
import posixpath
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.session import record_homes

REPLY_KEY = "engine_residue"
RESIDUE_DIRECTIVE = "engine residue: commit or name why not"
RESIDUE_CAP = 100

_PLANS_ROOT = "docs/plans"
_SEAM_GLOB = "*.seam.yaml"


def _home_rel(kind: str) -> str:
    return Path(record_homes.home_dir("", kind)).as_posix()


_TREE_ROOTS = (
    _home_rel("sizings"),
    _home_rel("mise-inventory"),
    _home_rel("mise-workflows"),
    _home_rel("lessons"),
    _home_rel("apm-rulings"),
    "archive/releases",
)
ENGINE_RESIDUE_ROOTS = _TREE_ROOTS + (_PLANS_ROOT,)
BUILD_CHURN_GLOBS = ("*.generated.*",)


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def is_engine_residue(path: str) -> bool:
    """True for a `docs/plans/*.seam.yaml` sidecar or any path under a tree residue root."""
    p = _norm(path)
    if posixpath.dirname(p) == _PLANS_ROOT:
        return fnmatch.fnmatchcase(posixpath.basename(p), _SEAM_GLOB)
    return any(p.startswith(r + "/") for r in _TREE_ROOTS)


def is_build_churn(path: str) -> bool:
    """True when the basename matches a generated-build-artifact glob."""
    name = posixpath.basename(_norm(path))
    return any(fnmatch.fnmatchcase(name, g) for g in BUILD_CHURN_GLOBS)


def dirty_engine_residue(root, exclude: Iterable[str] = ()) -> Optional[dict]:
    """Dirty engine-residue paths under `root` minus `exclude`, sorted and capped.

    `{"engine_residue": None}` when the in-process read reports a gap: no git fallback, the list
    is advisory and an unknown stays unknown. `engine_residue_total` appears only when capped.
    """
    from coordinator_core.git.scoped_worktree_status import scoped_worktree_status

    snap = scoped_worktree_status(Path(root), [], list(ENGINE_RESIDUE_ROOTS), [])
    if snap is None:
        return {REPLY_KEY: None}
    skip = {_norm(e) for e in exclude}
    found = sorted({_norm(p) for p in snap[0]} - skip)
    found = [p for p in found if is_engine_residue(p)]
    out: dict = {REPLY_KEY: found[:RESIDUE_CAP]}
    if len(found) > RESIDUE_CAP:
        out[REPLY_KEY + "_total"] = len(found)
    return out
