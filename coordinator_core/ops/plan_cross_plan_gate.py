"""
coordinator_core.ops.plan_cross_plan_gate — JSON-RPC "plan.cross_plan_gate" operation.

Purpose: reports whether a plan's cross-plan execution preconditions are
satisfied, so a wakeup or loop can poll one cheap read instead of re-deriving it.
A precondition is a ``depends_on_plan`` edge — in the plan's frontmatter (the
whole plan waits) or on a spine row (that row waits) — shaped
``{plan, chunk}`` (the named row is ``coded``) or ``{plan, status}`` (the plan
reached that lifecycle status). The evaluation is ``spine_read``'s own, the
same one ``read_spine`` withholds rows on.

Wire params:
    plan (str, required) — repo-relative or absolute path of the dependent plan.

Reply fields:
    {"plan": str, "satisfied": bool, "holds": [{"scope": "plan"|row id,
     "detail": str}], "dangling": [{"scope": ..., "detail": str}]}

    ``dangling`` edges can never be satisfied (predecessor absent, abandoned,
    superseded, row never coded); they are reported, not raised, and
    ``satisfied`` is False.

Negative-spec:
  - Does NOT write anything, spawn, or invoke git.
  - Does NOT accept a caller-supplied root or fall back to cwd.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops._param_alias import aliased_param, spellings
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.dispatch_emit.spine_read import (
    SpineReadError,
    frontmatter_plan_edges,
    unlanded_plan_edges,
)
from coordinator_core.ops.plan_tasks_render import load_rows

GENERATES: list = []


def _scopes(text: str):
    yield "plan", "plan frontmatter", frontmatter_plan_edges(text)
    loaded = load_rows(text)
    if loaded.status is LocateStatus.LOCATED:
        for row in loaded.rows:
            if isinstance(row, dict) and row.get("depends_on_plan"):
                if row.get("disposition") in ("coded", "done"):
                    continue
                yield str(row.get("id")), f"row {row.get('id')!r}", row["depends_on_plan"]


@register_op("plan.cross_plan_gate")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.cross_plan_gate" handler. See module docstring."""
    if repo_root is None:
        raise ValueError("plan.cross_plan_gate requires a resolved repo_root")
    raw_plan = aliased_param(params, "plan", "plan_path")
    if not isinstance(raw_plan, str) or not raw_plan.strip():
        raise ValueError(
            f"{spellings('plan', 'plan_path')} must be a non-empty string naming one plan file"
        )
    root = Path(main_worktree_root(repo_root))
    path = Path(raw_plan.strip())
    if not path.is_absolute():
        path = root / path
    if contained_path(path, [root]) is None:
        raise ValueError(f"plan escapes the resolved worktree: {raw_plan!r}")
    if not path.is_file():
        raise ValueError(f"no such plan: {raw_plan!r}")

    holds: list = []
    dangling: list = []
    cache: dict = {}
    for scope, owner, edges in _scopes(path.read_text(encoding="utf-8")):
        try:
            detail = unlanded_plan_edges(owner, edges, root, cache)
        except SpineReadError as exc:
            dangling.append({"scope": scope, "detail": str(exc)})
            continue
        if detail is not None:
            holds.append({"scope": scope, "detail": detail})
    return {
        "plan": path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix(),
        "satisfied": not holds and not dangling,
        "holds": holds,
        "dangling": dangling,
    }
