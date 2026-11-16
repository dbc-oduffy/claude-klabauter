"""
coordinator_core.ops.plan_status_done — JSON-RPC "plan.status_done" operation.

Purpose: move one plan to its terminal done status (``implemented``) once no spine row is open,
for a plan whose run never closed (an abandoned warp run has no close-out to stamp it).

Wire params:
    plan (str, required) — plan path, repo-relative or absolute; must resolve inside the main worktree.

Reply fields:
    {"plan": "docs/plans/....md", "prior_status": str|None, "status": str|None, "stamped": bool,
     "log": str}

    A row counts closed when its disposition is closed, or an ``em-discharge`` entry in the plan's
    ``.evidence.yaml`` settles it (``archive_stamp._open_spine_row_ids``). Any open row refuses with a
    ``ValueError`` naming the rows; the file is not touched. With none open, the flip is
    ``plan_status_transition``'s ``stamp-implemented`` (its own refusals, commit and cascade), reached
    through ``archive_stamp.cs_stamp_plan_implemented``; a non-zero exit raises with its output.

Negative-spec:
  - Does NOT write a status itself; the one stamper is ``plan_status_transition``.
  - Does NOT sweep: one named plan per call, never reached from another op.
  - Does NOT accept a caller-supplied root or fall back to the process cwd.
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import contained_path

GENERATES: list = []


def _status_of(path: Path) -> Optional[str]:
    try:
        split = split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    return read_fm_field_unquoted(split.fm_text, "status") if split else None


@register_op("plan.status_done")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.status_done" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.status_done requires a resolved repo_root")
    raw = params.get("plan")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("plan must be a non-empty plan path string")
    worktree_root = Path(main_worktree_root(repo_root))
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        candidate = worktree_root / candidate
    if contained_path(candidate, [worktree_root]) is None:
        raise ValueError(f"plan escapes the resolved worktree: {raw!r}")
    if not candidate.is_file():
        raise ValueError(f"no such plan: {raw!r}")
    rel = candidate.relative_to(worktree_root).as_posix()

    from coordinator_core.archive_stamp import _open_spine_row_ids, cs_stamp_plan_implemented

    open_ids = _open_spine_row_ids(candidate)
    if open_ids:
        raise ValueError(
            f"{rel}: {len(open_ids)} spine row(s) still open: {', '.join(open_ids)}; "
            "land, discharge or disposition them first"
        )

    prior = _status_of(candidate)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = cs_stamp_plan_implemented(str(candidate))
    log = out.getvalue().strip()
    if rc != 0:
        raise ValueError(f"{rel}: stamp-implemented exited {rc}: {log}")
    status = _status_of(candidate)
    return {"plan": rel, "prior_status": prior, "status": status, "stamped": prior != status, "log": log}
