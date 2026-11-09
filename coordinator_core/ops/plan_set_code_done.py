"""
coordinator_core.ops.plan_set_code_done — JSON-RPC "plan.set_code_done" operation.

Purpose: one read-only answer to "does this named plan set have any open executor row?", so a
heavy launch (a UE build, a long test run) can be held behind a measurement instead of each
caller reading every plan's task spine and judging open rows itself.

Wire params:
    plans (list[str], required) — non-empty list of plan paths, repo-relative or absolute; each
                                  must resolve inside the main worktree.

Reply fields:
    {"code_complete": bool,
     "plans": [{"plan": "docs/plans/....md", "open_rows": [row_id, ...], "error": str|None}, ...]}

    ``code_complete`` is true only when every named plan was read and none carries an open
    executor row. An open row is a spine row whose canonical ``disposition`` is not closed
    (``spine_read.NON_DISPATCHABLE_DISPOSITIONS``; an unrecognized disposition counts as open).
    An executor row is one whose ``agent_type`` is absent or names an executor
    (``coordinator:executor`` and its variants); EM-, reviewer- and test-runner-owned rows do not
    hold the verdict. A plan that is missing, escapes the worktree, is unreadable or has no
    LOCATED or no non-empty task spine reports an ``error`` and forces ``code_complete`` false: a plan that cannot
    be read is never evidence of completion.

Negative-spec:
  - Does NOT write, stamp or mutate anything; the verdict is derived from each plan's spine.
  - Does NOT spawn and does NOT invoke git; plans are read as files and parsed by
    ``spine_read.load_rows_memo``, the engine's one spine parser.
  - Does NOT sweep the corpus: only the named plans are read.
  - Does NOT hold a launch; the caller acts on the verdict.
  - Does NOT accept a caller-supplied root or fall back to the process cwd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.spine_read import (
    NON_DISPATCHABLE_DISPOSITIONS,
    load_rows_memo,
    with_canonical_disposition,
)

GENERATES: list = []


def _is_executor_row(row: dict) -> bool:
    agent_type = row.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.strip():
        return True
    return agent_type.strip().rsplit(":", 1)[-1].startswith("executor")


def _open_executor_rows(text: str) -> list:
    result = load_rows_memo(text)
    if result.status is not LocateStatus.LOCATED:
        raise ValueError(f"task spine is {result.status.name}, not LOCATED")
    if not result.rows:
        raise ValueError("task spine has no rows")
    open_rows = []
    for raw in map(with_canonical_disposition, result.rows):
        if not isinstance(raw, dict):
            raise ValueError("task spine carries a non-mapping row")
        if raw.get("disposition") in NON_DISPATCHABLE_DISPOSITIONS:
            continue
        if _is_executor_row(raw):
            open_rows.append(str(raw.get("id")))
    return open_rows


def _one_plan(raw: object, worktree_root: Path) -> dict:
    name = raw if isinstance(raw, str) else repr(raw)
    entry = {"plan": name, "open_rows": [], "error": None}
    if not isinstance(raw, str) or not raw.strip():
        entry["error"] = "plan path must be a non-empty string"
        return entry
    candidate = Path(raw.strip())
    if not candidate.is_absolute():
        candidate = worktree_root / candidate
    if contained_path(candidate, [worktree_root]) is None:
        entry["error"] = "plan escapes the resolved worktree"
        return entry
    try:
        rel = candidate.relative_to(worktree_root).as_posix()
    except ValueError:
        rel = candidate.as_posix()
    entry["plan"] = rel
    if not candidate.is_file():
        entry["error"] = "no such plan"
        return entry
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
        entry["open_rows"] = _open_executor_rows(text)
    except (OSError, ValueError) as exc:
        entry["error"] = str(exc)
    return entry


@register_op("plan.set_code_done")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.set_code_done" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.set_code_done requires a resolved repo_root")
    plans = params.get("plans")
    if not isinstance(plans, list) or not plans:
        raise ValueError("plans must be a non-empty list of plan paths")
    worktree_root = Path(main_worktree_root(repo_root))
    entries = [_one_plan(raw, worktree_root) for raw in plans]
    return {
        "code_complete": all(e["error"] is None and not e["open_rows"] for e in entries),
        "plans": entries,
    }
