"""
coordinator_core.ops.fleet.prune_emitted — fleet.prune_emitted_output op.

Purpose: delete untracked `<plan>[.<variant>].workflow.mjs` scripts and their
`.emitted.json` receipts from docs/plans/ when their plan is missing or not
`executing`. One scandir, zero spawns, no YAML parse; tracked-at-HEAD comes from
`read_tree_spine` (object store). Fails closed: an unreadable HEAD tree deletes
nothing.

Negative-spec: never touches a tracked file, a live-claim plan, a stranded run
or a file inside the fresh-emission grace window; never recurses; commits nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.fleet.archive_plans import _is_claim_live, _primary_for_sidecar

_LOG = logging.getLogger(__name__)

OP_KEY = "fleet.prune_emitted_output"
_FRESH_EMIT_GRACE_S = 3600.0

_SCRIPT_SUFFIX = ".workflow.mjs"
_RECEIPT_SUFFIX = ".workflow.mjs.emitted.json"
_PLANS_REL = "docs/plans"
_EMPTY_SCALARS = frozenset({"", "null", "~"})


def _unit_key(name: str) -> str:
    return name[: -len(".emitted.json")] if name.endswith(_RECEIPT_SUFFIX) else name


def _receipt_plan(receipt: Path) -> Optional[str]:
    """The receipt's `plan` basename, or None when absent/untrusted."""
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plan = data.get("plan") if isinstance(data, dict) else None
    if not isinstance(plan, str) or not plan.endswith(".md"):
        return None
    if "/" in plan or "\\" in plan or plan in (".md", "..md"):
        return None
    return plan


class _PlanFacts:
    __slots__ = ("status", "stamped", "unreadable")

    def __init__(self, status: str, stamped: bool, unreadable: bool) -> None:
        self.status = status
        self.stamped = stamped
        self.unreadable = unreadable


def _read_plan(path: Path) -> _PlanFacts:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _PlanFacts("", False, True)
    split = split_frontmatter(text)
    fm = split.fm_text if split else ""
    status = read_fm_field_unquoted(fm, "status") or ""
    stamp = read_fm_field_unquoted(fm, "review_stamp") or ""
    return _PlanFacts(status.strip(), stamp.strip() not in _EMPTY_SCALARS, False)


def prune_emitted_output(worktree_root: Path, common_dir: Path, *, dry_run: bool) -> dict:
    """Classify (and, unless dry_run, delete) prunable emitted output.

    -> {"dry_run", "candidates": [{path, plan, reason}], "pruned": [path],
        "retained": [{path, reason}], "failed": [{path, error}],
        "tracked_state_unknown": bool}. Paths are repo-relative POSIX.
    """
    plans_dir = Path(worktree_root) / "docs" / "plans"
    result: dict = {
        "dry_run": dry_run,
        "candidates": [],
        "pruned": [],
        "retained": [],
        "failed": [],
        "tracked_state_unknown": False,
    }

    members: Dict[str, os.DirEntry] = {}
    try:
        with os.scandir(plans_dir) as it:
            for entry in it:
                if entry.name.endswith((_SCRIPT_SUFFIX, _RECEIPT_SUFFIX)):
                    members[entry.name] = entry
    except OSError:
        return result
    if not members:
        return result

    units: Dict[str, List[str]] = {}
    for name in sorted(members):
        units.setdefault(_unit_key(name), []).append(name)

    def rel(name: str) -> str:
        return f"{_PLANS_REL}/{name}"

    spine = read_tree_spine(worktree_root, [f"{_PLANS_REL}/_"])
    tracked_dir = spine.get(_PLANS_REL) if spine is not None else None
    if tracked_dir is None:
        result["tracked_state_unknown"] = True
        for name in sorted(members):
            result["retained"].append({"path": rel(name), "reason": "tracked-state-unknown"})
        return result

    plan_cache: Dict[str, Optional[_PlanFacts]] = {}
    live_cache: Dict[str, bool] = {}
    now = time.time()
    to_delete: List[str] = []

    def retain(names: List[str], reason: str) -> None:
        for n in names:
            result["retained"].append({"path": rel(n), "reason": reason})

    for key, names in units.items():
        if any(n in tracked_dir for n in names):
            retain(names, "tracked-at-head")
            continue

        plan_name: Optional[str] = None
        for n in names:
            if n.endswith(_RECEIPT_SUFFIX):
                plan_name = _receipt_plan(plans_dir / n)
                break
        if plan_name is None:
            plan_name = _primary_for_sidecar(plans_dir / names[0]).name

        if plan_name not in plan_cache:
            plan_path = plans_dir / plan_name
            plan_cache[plan_name] = _read_plan(plan_path) if os.path.lexists(plan_path) else None
        facts = plan_cache[plan_name]

        if facts is None:
            reason = "plan-missing"
        else:
            if facts.unreadable:
                retain(names, "plan-unreadable")
                continue
            if facts.status == "executing":
                retain(names, "plan-executing")
                continue
            if plan_name not in live_cache:
                live_cache[plan_name] = _is_claim_live(common_dir, plans_dir / plan_name)
            if live_cache[plan_name]:
                retain(names, "live-claim")
                continue
            stem = plan_name[: -len(".md")]
            if (
                not facts.stamped
                and facts.status != "implemented"
                and f"{stem}{_RECEIPT_SUFFIX}" in members
            ):
                retain(names, "stranded-run")
                continue
            reason = "plan-not-executing"

        try:
            fresh = any(now - members[n].stat().st_mtime < _FRESH_EMIT_GRACE_S for n in names)
        except OSError:
            retain(names, "stat-failed")
            continue
        if fresh:
            retain(names, "fresh-emission")
            continue

        for n in names:
            result["candidates"].append(
                {"path": rel(n), "plan": plan_name if facts is not None else None, "reason": reason}
            )
            to_delete.append(n)

    if not dry_run:
        for n in to_delete:
            try:
                (plans_dir / n).unlink(missing_ok=True)
                result["pruned"].append(rel(n))
            except OSError as exc:
                result["failed"].append({"path": rel(n), "error": str(exc)})
    return result


@register_op(OP_KEY)
async def _handler(params: dict, repo_root=None) -> dict:
    """fleet.prune_emitted_output — delete untracked emitted workflow output.

    dry_run defaults to true. repo_root is the git common dir
    (_OP_KEY_SCOPE="common_dir"); the worktree is main_worktree_root(common_dir).
    """
    dry_run = params.get("dry_run", True)
    if not isinstance(dry_run, bool):
        return {"error": "dry_run must be a bool", "exit_code": 1}
    if repo_root is None:
        _LOG.error("%s: repo_root is None — keying-table misconfiguration", OP_KEY)
        return {"error": "repo_root is None; cannot derive worktree root", "exit_code": 1}
    common_dir = Path(repo_root)
    return await asyncio.to_thread(
        prune_emitted_output, main_worktree_root(common_dir), common_dir, dry_run=dry_run
    )
