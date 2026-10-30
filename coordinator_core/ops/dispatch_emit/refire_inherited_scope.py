"""The scope a refire inherits from the halted run it resumes.

``inherited_scope(root, resume)`` maps each path the predecessor withheld at its halt, each path its
reviewers applied a fix to, and each ``writes`` path of its completed rows, to the origin that makes it the refire's own declared scope. Pure read: no
git spawn.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

ORIGIN_PREDECESSOR_WITHHELD = "predecessor-withheld"
ORIGIN_REVIEWER_APPLIED = "reviewer-applied"
ORIGIN_COMPLETED_ROW = "completed-row"


def withheld_of(root: Path, resume: dict) -> dict:
    """``{path: reason}`` the predecessor withheld: the resume's own ``run_artifacts.withheld``, else
    the halt marker of ``resume["run_id"]``; ``{}`` when neither resolves."""
    arts = resume.get("run_artifacts")
    withheld = arts.get("withheld") if isinstance(arts, dict) else None
    if isinstance(withheld, dict):
        return withheld
    run_id = resume.get("run_id")
    from coordinator_core.ops._path_guard import safe_id

    if not (isinstance(run_id, str) and safe_id(run_id)):
        return {}
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.halted_run import halted_withheld

    return halted_withheld(root / RECORD_DIR_ROOT / run_id)


def _reviewer_applied_of(root: Path, resume: dict) -> frozenset:
    """Paths the predecessor's reviewers fixed, from the ``inline_review`` its run record holds."""
    run_id = resume.get("run_id")
    from coordinator_core.ops._path_guard import safe_id

    if not (isinstance(run_id, str) and safe_id(run_id)):
        return frozenset()
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_outcome

    outcome = read_run_outcome(root, run_id)
    if outcome is None:
        return frozenset()
    _at, paths = land_run._fixes_integrated_at(root, outcome["inline_review"])
    return paths


def _completed_row_writes_of(root: Path, resume: dict) -> frozenset:
    """Every ``writes`` path of every row the predecessor's ``manifest.json`` staged, finished or not;
    empty when the run id is unsafe or the manifest is absent or malformed. A refire re-executes its
    own unfinished rows, so their writes are its scope as much as the completed rows' are."""
    run_id = resume.get("run_id")
    if not (isinstance(run_id, str) and safe_id(run_id)):
        return frozenset()
    try:
        manifest = json.loads((record_dir(root, run_id) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    rows = manifest.get("rows") if isinstance(manifest, dict) else None
    if not isinstance(rows, list):
        return frozenset()
    out: set = set()
    for row in rows:
        writes = row.get("writes") if isinstance(row, dict) else None
        if isinstance(writes, list):
            out.update(w for w in writes if isinstance(w, str) and w)
    return frozenset(out)


def inherited_scope(root: Path, resume: object) -> dict:
    """``{path: origin}`` of the paths a refire of ``resume`` inherits as declared scope.

    Precedence on a shared path: ``predecessor-withheld``, ``reviewer-applied``, ``completed-row``. ``{}`` when ``resume``
    names no halted predecessor.
    """
    if not isinstance(resume, dict):
        return {}
    out: dict = {}
    for path in withheld_of(root, resume):
        if isinstance(path, str) and path:
            out[path] = ORIGIN_PREDECESSOR_WITHHELD
    for path in _reviewer_applied_of(root, resume):
        out.setdefault(path, ORIGIN_REVIEWER_APPLIED)
    for path in _completed_row_writes_of(root, resume):
        out.setdefault(path, ORIGIN_COMPLETED_ROW)
    return out
