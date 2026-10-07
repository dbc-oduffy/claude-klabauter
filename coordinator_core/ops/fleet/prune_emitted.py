"""
coordinator_core.ops.fleet.prune_emitted — fleet.prune_emitted_output op.

Purpose: delete untracked `<plan>[.<variant>].workflow.mjs` scripts and their
`.emitted.json` receipts from docs/plans/ when their plan is missing or not
`executing`. Also sweeps `state/**/fire-*.mjs` and `state/**/*.mjs.emitted.json`
older than 24h whose owning plan (the receipt's `plan` field, resolved in
docs/plans/, or every plan in its `plans` list) is implemented or abandoned. An unresolvable
owner is kept until 7 days old, then deleted as `aged-out`.
A plan-blitz fire/repair-fire unit is also closed by a landing file in its dir. Zero
spawns, no YAML parse; tracked-at-HEAD comes from one `read_tree_spine` call
(object store). Fails closed: an unreadable HEAD tree deletes nothing.

Negative-spec: never touches a tracked file, a live-claim plan, a stranded run
or a file inside the fresh-emission grace window; never recurses; commits nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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

_STATE_REL = "state"
_STATE_AGE_S = 24 * 3600.0
_STATE_AGED_OUT_S = 7 * 24 * 3600.0
_STATE_CLOSED_STATUSES = frozenset({"implemented", "abandoned"})
_STATE_RECEIPT_SUFFIX = ".mjs.emitted.json"
_BLITZ_DIR_PREFIX = f"{_STATE_REL}/plan-blitz/"
_FIRE_RE = re.compile(r"^fire-(\d+)-\d+\.mjs$")
_REPAIR_RE = re.compile(r"^repair-fire-\d+\.mjs$")
_LANDING_RE = re.compile(r"^wave-(\d+)\.landing\.json$")


def _is_state_member(name: str) -> bool:
    return name.endswith((".mjs", _STATE_RECEIPT_SUFFIX))


def _unit_key_state(name: str) -> str:
    return name[: -len(".emitted.json")] if name.endswith(_STATE_RECEIPT_SUFFIX) else name


def _scan_state(root: Path) -> Dict[str, Dict[str, os.DirEntry]]:
    """{repo-relative dir: {name: entry}} for every state/** file matching a swept glob."""
    found: Dict[str, Dict[str, os.DirEntry]] = {}
    stack = [(root, _STATE_REL)]
    while stack:
        path, rel_dir = stack.pop()
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), f"{rel_dir}/{entry.name}"))
                        elif _is_state_member(entry.name) and entry.is_file(follow_symlinks=False):
                            found.setdefault(rel_dir, {})[entry.name] = entry
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _blitz_landings(path: Path) -> Dict[int, float]:
    """{wave number: mtime} of `wave-<N>.landing.json` in one blitz dir; one listing, no per-unit stat."""
    out: Dict[int, float] = {}
    try:
        with os.scandir(path) as it:
            for entry in it:
                m = _LANDING_RE.match(entry.name)
                if m:
                    try:
                        out[int(m.group(1))] = entry.stat().st_mtime
                    except OSError:
                        continue
    except OSError:
        pass
    return out


def _blitz_closed(script: str, script_mtime: float, landings: Dict[int, float]) -> bool:
    """A fire is closed by its wave's landing; a repair fire by any landing newer than itself."""
    m = _FIRE_RE.match(script)
    if m:
        return int(m.group(1)) in landings
    if _REPAIR_RE.match(script):
        return any(t > script_mtime for t in landings.values())
    return False


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


def _receipt_plans(receipt: Path) -> Optional[List[str]]:
    """The receipt's `plans` basenames, or None when absent, empty or any entry untrusted."""
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plans = data.get("plans") if isinstance(data, dict) else None
    if not isinstance(plans, list) or not plans:
        return None
    for p in plans:
        if (
            not isinstance(p, str)
            or not p.endswith(".md")
            or "/" in p
            or "\\" in p
            or p in (".md", "..md")
        ):
            return None
    return list(dict.fromkeys(plans))


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
        pass
    state_dirs = _scan_state(Path(worktree_root) / _STATE_REL)
    if not members and not state_dirs:
        return result

    units: Dict[str, List[str]] = {}
    for name in sorted(members):
        units.setdefault(_unit_key(name), []).append(name)

    def rel(name: str) -> str:
        return f"{_PLANS_REL}/{name}"

    spine_paths = [f"{d}/_" for d in sorted(state_dirs)]
    if members:
        spine_paths.append(f"{_PLANS_REL}/_")
    spine = read_tree_spine(worktree_root, spine_paths)
    if spine is None:
        result["tracked_state_unknown"] = True
        for name in sorted(members):
            result["retained"].append({"path": rel(name), "reason": "tracked-state-unknown"})
        for d in sorted(state_dirs):
            for name in sorted(state_dirs[d]):
                result["retained"].append({"path": f"{d}/{name}", "reason": "tracked-state-unknown"})
        return result
    tracked_dir = spine.get(_PLANS_REL, {})
    if members and _PLANS_REL not in spine:
        result["tracked_state_unknown"] = True
        for name in sorted(members):
            result["retained"].append({"path": rel(name), "reason": "tracked-state-unknown"})
        units = {}

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

    to_delete_state: List[str] = []
    for d in sorted(state_dirs):
        _classify_state_dir(
            d, state_dirs[d], spine.get(d) or {}, plans_dir, common_dir, now,
            plan_cache, live_cache, result, to_delete_state,
        )

    if not dry_run:
        for n in to_delete:
            try:
                (plans_dir / n).unlink(missing_ok=True)
                result["pruned"].append(rel(n))
            except OSError as exc:
                result["failed"].append({"path": rel(n), "error": str(exc)})
        for r in to_delete_state:
            try:
                (Path(worktree_root) / r).unlink(missing_ok=True)
                result["pruned"].append(r)
            except OSError as exc:
                result["failed"].append({"path": r, "error": str(exc)})
    return result


def _classify_state_dir(
    rel_dir: str,
    entries: Dict[str, os.DirEntry],
    tracked: Dict[str, object],
    plans_dir: Path,
    common_dir: Path,
    now: float,
    plan_cache: Dict[str, Optional[_PlanFacts]],
    live_cache: Dict[str, bool],
    result: dict,
    to_delete: List[str],
) -> None:
    """Classify one state/ directory's fire-*.mjs / *.mjs.emitted.json units.

    A `<name>.mjs` script and its `<name>.mjs.emitted.json` receipt are one unit; a bare
    non-fire script is never touched. Owner = the receipt's `plan` (or, for a multi-plan fire,
    every plan in `plans`) resolved in docs/plans/. An open owner keeps the unit; an
    unresolved owner (no receipt, null plan, missing plan file) keeps it until every file is
    older than _STATE_AGED_OUT_S, then it is deleted as `aged-out` unless a claim is live.
    """
    units: Dict[str, List[str]] = {}
    for name in sorted(entries):
        units.setdefault(_unit_key_state(name), []).append(name)
    landings: Optional[Dict[int, float]] = None

    for key, names in units.items():
        if not (
            any(n.endswith(_STATE_RECEIPT_SUFFIX) for n in names)
            or names[0].startswith(("fire-", "repair-fire-"))
        ):
            continue  # a bare non-fire script is outside both globs
        paths = [f"{rel_dir}/{n}" for n in names]

        def retain(reason: str) -> None:
            for p in paths:
                result["retained"].append({"path": p, "reason": reason})

        if any(n in tracked for n in names):
            retain("tracked-at-head")
            continue
        plan_name: Optional[str] = None
        for n in names:
            if n.endswith(_STATE_RECEIPT_SUFFIX):
                plan_name = _receipt_plan(Path(entries[n].path))
                break
        multi: Optional[List[str]] = None
        if plan_name is None:
            for n in names:
                if n.endswith(_STATE_RECEIPT_SUFFIX):
                    multi = _receipt_plans(Path(entries[n].path))
                    break
        owners = [plan_name] if plan_name is not None else (multi or [])
        owner_facts: List[Optional[_PlanFacts]] = []
        for owner in owners:
            if owner not in plan_cache:
                plan_path = plans_dir / owner
                plan_cache[owner] = _read_plan(plan_path) if os.path.lexists(plan_path) else None
            owner_facts.append(plan_cache[owner])
        if any(f is not None and f.unreadable for f in owner_facts):
            retain("plan-unreadable")
            continue
        facts = owner_facts[0] if plan_name is not None else None
        closed_reason: Optional[str] = None
        if multi:
            if any(f is not None and f.status not in _STATE_CLOSED_STATUSES for f in owner_facts):
                retain("owner-open")
                continue
            if all(f is not None for f in owner_facts):
                closed_reason = "plans-closed"
        elif facts is not None and facts.status in _STATE_CLOSED_STATUSES:
            closed_reason = f"plan-{facts.status}"
        if closed_reason is None and rel_dir.startswith(_BLITZ_DIR_PREFIX):
            if landings is None:
                landings = _blitz_landings(plans_dir.parent.parent / rel_dir)
            try:
                mtime = entries[names[0]].stat().st_mtime
            except OSError:
                retain("stat-failed")
                continue
            if _blitz_closed(key, mtime, landings):
                closed_reason = "landed"
        aging_out = False
        if closed_reason is None:
            if facts is not None:
                retain("owner-open")
                continue
            aging_out = True
            closed_reason = "aged-out"
        live = False
        for owner, f in zip(owners, owner_facts):
            if f is None:
                continue
            if owner not in live_cache:
                live_cache[owner] = _is_claim_live(common_dir, plans_dir / owner)
            live = live or live_cache[owner]
        if live:
            retain("live-claim")
            continue
        limit = _STATE_AGED_OUT_S if aging_out else _STATE_AGE_S
        try:
            young = any(now - entries[n].stat().st_mtime < limit for n in names)
        except OSError:
            retain("stat-failed")
            continue
        if young:
            retain("owner-unresolved" if aging_out else "too-young")
            continue
        for p in paths:
            result["candidates"].append({"path": p, "plan": plan_name, "reason": closed_reason})
            to_delete.append(p)


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
