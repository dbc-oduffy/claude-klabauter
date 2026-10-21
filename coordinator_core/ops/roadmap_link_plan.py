"""
coordinator_core.ops.roadmap_link_plan — JSON-RPC "roadmap.link_plan".

Purpose: write ``governing_plan: <plan path>`` onto one live roadmap baton, so
``roadmap.plan_gate`` links the baton to a plan whose work landed outside the roadmap's own
plan flow. A separate op, not a ``roadmap.link_stubs`` param: DR-264 bounds that op to the
``blocked_by``/``blocks`` fields and forbids it becoming a general frontmatter writer.

Params: ``stub_id`` (the baton's stub_id or file stem), ``plan`` (repo-relative path under
docs/plans/), ``reason`` (mandatory, echoed). Optional ``roadmap_id`` must match the baton's.

Refuses (exit_code 1): missing params; no live roadmap baton for ``stub_id``; the plan file
missing, outside docs/plans/ or not a plan record; the baton already carries a
``deliverable_id``/``deliverable_ids`` (``governing_plan`` is ignored by the gate then) or a
different ``governing_plan``/``origin_plan_id``/``plan_ids`` link. Re-linking the same plan is
an idempotent no-op.

Negative-spec: one field only; no body write; no git spawn and no commit (commit timing stays
the EM session's); never touches archive/handoffs/.
"""

from __future__ import annotations

import difflib
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.frontmatter.primitives import insert_fm_field, rebuild, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.roadmap import plan_gate as pg
from coordinator_core.roadmap.gate_v2 import _head_fields
from coordinator_core.session.record_homes import home_dir

_LOG = logging.getLogger(__name__)

_BATON_WANT = frozenset({
    "stub_id", "kind", "roadmap_id", "deliverable_id", "deliverable_ids",
    "governing_plan", "origin_plan_id", "plan_ids",
})
_PLAN_WANT = frozenset({"title", "status", "plan_id", "deliverable_id", "sizing_object", "kind", "plan"})
_HEAD_BYTES = 6144
_ROADMAP_KINDS = frozenset(kind_values_for_canonical("roadmap-baton"))


def _err(msg: str) -> dict:
    _LOG.warning("roadmap.link_plan: %s", msg)
    return {"exit_code": 1, "applied": False, "error": msg}


def _find_baton(worktree: Path, stub_id: str):
    """``(path, fields)`` of the live baton whose stem or ``stub_id`` is ``stub_id``, else None."""
    live = Path(home_dir(str(worktree), "handoffs"))
    direct = live / f"{stub_id}.md"
    if direct.is_file():
        try:
            return direct, _head_fields(direct, _HEAD_BYTES, _BATON_WANT)
        except OSError:
            return None
    try:
        names = sorted(e.path for e in os.scandir(live) if e.name.endswith(".md"))
    except OSError:
        return None
    for name in names:
        try:
            fm = _head_fields(name, _HEAD_BYTES, _BATON_WANT)
        except OSError:
            continue
        if fm.get("stub_id") == stub_id:
            return Path(name), fm
    return None


def _closest_baton_name(worktree: Path, wanted: str) -> Optional[str]:
    """The roadmap baton ``stub_id`` or file stem nearest ``wanted`` (substring either way, else
    difflib ratio >= 0.5), preferring a ``stub_id`` over a stem; None when nothing is close."""
    live = Path(home_dir(str(worktree), "handoffs"))
    try:
        entries = sorted((e for e in os.scandir(live) if e.name.endswith(".md")), key=lambda e: e.path)
    except OSError:
        return None
    names: list = []
    for entry in entries:
        try:
            fm = _head_fields(entry.path, _HEAD_BYTES, _BATON_WANT)
        except OSError:
            continue
        if fm.get("kind") not in _ROADMAP_KINDS:
            continue
        sid = fm.get("stub_id")
        if sid:
            names.append(str(sid))
        names.append(entry.name[:-3])
    low = wanted.lower()
    hits = [n for n in names if n.lower() in low or low in n.lower()]
    if hits:
        return max(hits, key=lambda n: (len(n) if n.lower() in low else -len(n)))
    near = difflib.get_close_matches(wanted, names, n=1, cutoff=0.5)
    return near[0] if near else None


def _present(value: Any) -> list:
    return [str(v) for v in (value if isinstance(value, list) else [value]) if v]


def _link(*, stub_id: str, plan: str, reason: str, roadmap_id: str, repo_root: Path) -> dict:
    worktree = Path(main_worktree_root(repo_root))
    found = _find_baton(worktree, stub_id)
    if found is None:
        near = _closest_baton_name(worktree, stub_id)
        hint = f"; closest roadmap baton: {near!r}" if near else ""
        return _err(f"stub_id {stub_id!r} names no live baton under state/handoffs/{hint}")
    path, fm = found
    if fm.get("kind") not in _ROADMAP_KINDS:
        return _err(f"{stub_id!r} is not a roadmap baton (kind {fm.get('kind')!r})")
    if roadmap_id and fm.get("roadmap_id") != roadmap_id:
        return _err(f"{stub_id!r} carries roadmap_id {fm.get('roadmap_id')!r}, not {roadmap_id!r}")

    rel = plan.strip().replace("\\", "/")
    plan_path = contained_path(worktree / rel, [worktree / "docs" / "plans"])
    if plan_path is None or plan_path.suffix != ".md" or not plan_path.is_file():
        return _err(f"plan {rel!r} is not an existing .md file under docs/plans/")
    try:
        plan_fm = _head_fields(plan_path, 2048, _PLAN_WANT)
    except OSError as exc:
        return _err(f"plan {rel!r} unreadable: {exc}")
    if not plan_fm or not pg.is_plan_record(plan_fm):
        return _err(f"{rel!r} is not a plan record")

    for field in ("deliverable_id", "deliverable_ids", "origin_plan_id", "plan_ids"):
        if _present(fm.get(field)):
            return _err(f"{stub_id!r} already carries {field}: {fm.get(field)!r}; governing_plan is not written over a plan link")
    existing = _present(fm.get("governing_plan"))
    if existing and existing[0].replace("\\", "/") != rel:
        return _err(f"{stub_id!r} already carries governing_plan: {existing[0]!r}")
    if existing:
        return {"exit_code": 0, "applied": False, "reason": reason, "message": f"{stub_id} already links {rel}"}

    def mutate(old: str) -> str:
        split = split_frontmatter(old)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {path}")
        return rebuild(split, insert_fm_field(split.fm_text, "governing_plan", rel))

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {path}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _err(f"cannot read/write {path}: {exc}")
    _LOG.info("roadmap.link_plan: %s governing_plan %s (reason: %s)", stub_id, rel, reason)
    return {
        "exit_code": 0, "applied": True, "reason": reason,
        "message": f"linked: {stub_id} governing_plan {rel}",
    }


@register_op("roadmap.link_plan")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.link_plan" handler. See module docstring."""
    p: Dict[str, str] = {k: (params.get(k) or "").strip() for k in ("stub_id", "plan", "reason", "roadmap_id")}
    for key in ("stub_id", "plan", "reason"):
        if not p[key]:
            return _err(f"missing required param: {key} (non-empty)")
    if repo_root is None:
        return _err("repo_root is required")
    return _link(stub_id=p["stub_id"], plan=p["plan"], reason=p["reason"], roadmap_id=p["roadmap_id"], repo_root=repo_root)
