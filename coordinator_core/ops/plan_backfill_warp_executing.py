"""
coordinator_core.ops.plan_backfill_warp_executing — JSON-RPC "plan.backfill_warp_executing".

Purpose: advance, once, the plans a warp run coded rows from but left at ``draft``, ``reviewed``
or ``approved``. A plan whose every open row is withheld (uncleared ``external_gate`` or
``needs_slot``) and that has a coded row advances to ``landed`` instead, the rung the gate counts
as coded; an ``executing`` plan of that shape is advanced to ``landed`` too. A run halting before close never stamps its plan, and ``roadmap.plan_gate``
reads such a plan as ``plan-drafted`` for good. The warp stage leg now advances the plan it
stages; this op clears the plans stranded before that leg existed. The receiving repo runs it
in its own tree.

Wire params (all optional):
    apply     (bool)  — false (default) lists candidates and writes nothing; true flips them.
    plan_ids  (list)  — plan_id values; narrows ``apply`` to these candidates. The candidate
                        list stays complete.

Reply fields:
    candidates   [{plan, plan_id, status, target_status, coded_rows, runs}] — every pre-executing plan that a
                 warp manifest names and whose spine carries a coded row with a disposition_ref.
    applied      [plan] — plans flipped to their ``target_status`` (empty on a dry run).
    skipped      [{plan, reason}] — apply-narrowed-in candidates left alone (dirty or untracked
                 against HEAD, or no longer pre-executing at write time).
    unmatched_plan_ids  [plan_id] — ``plan_ids`` entries naming no candidate.
    commit_sha / commit_error — the one scoped commit over every flipped plan.

Evidence: a warp manifest naming a plan (its ``plan_id``) proves a run staged that plan; a spine
row at ``disposition: coded`` with a ``disposition_ref`` proves the run's rows landed. The row ids
need not appear in the manifest: a resume manifest can carry ``rows: []`` and still name its plan.

Negative-spec:
  - Never reads git history and never verifies that a ``disposition_ref`` is an ancestor of HEAD.
  - Never touches a plan past ``executing``, or in any other status; ``executing`` only advances to ``landed``.
  - Never runs from the gate: ``roadmap.plan_gate`` stays a header-only read.
  - Never spawns on the dry-run leg; the apply leg adds only the scoped commit's own spawns.
  - Never overwrites a plan whose bytes differ from HEAD, or that HEAD does not track.
  - Does not accept a caller-supplied root: the root is the per-request ``repo_root`` promoted
    to the main worktree via ``main_worktree_root``.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import CallerFacingValidationError, register_op

GENERATES: list = []

_PARAMS = frozenset({"apply", "plan_ids"})
_PLAN_ID_RE = re.compile(r'"plan_id"\s*:\s*"([^"]+)"')
_CODED_HINT_RE = re.compile(r"disposition:\s*[\"']?coded")
_PLANS_DIR = "docs/plans"
_HEAD_WANT = frozenset({"plan_id", "status"})
_HEAD_BYTES = 2048
_SCAN_BYTES = 8192
_HEAD_PLAN_ID_RE = re.compile(r"^plan_id:[ \t]*[\"']?([^\s\"'#]+)", re.MULTILINE)
_TARGET_STATUS = "executing"
_LANDED_STATUS = "landed"


def _pre_executing_statuses() -> frozenset:
    from coordinator_core.ops.plan_status_transition import _FLIPPABLE_STATUSES_ORDER

    return frozenset(_FLIPPABLE_STATUSES_ORDER[: _FLIPPABLE_STATUSES_ORDER.index(_TARGET_STATUS)])


def _manifest_runs(root: Path) -> Dict[str, List[str]]:
    """``{plan_id: [run dir name]}`` from one regex pass over each warp manifest's raw text."""
    from coordinator_core.session import record_homes

    warp_dir = Path(record_homes.home_dir(str(root), "warp"))
    runs: Dict[str, List[str]] = {}
    try:
        entries = sorted(os.scandir(warp_dir), key=lambda e: e.name)
    except OSError:
        return runs
    for entry in entries:
        try:
            text = Path(entry.path, "manifest.json").read_text(encoding="utf-8")
        except OSError:
            continue
        for plan_id in set(_PLAN_ID_RE.findall(text)):
            runs.setdefault(plan_id, []).append(entry.name)
    return runs


def _pre_executing_plans(root: Path, linked: Dict[str, List[str]]) -> List[tuple]:
    """``(rel, plan_id, status)`` for each linked plan still before ``executing``."""
    from coordinator_core.roadmap.gate_v2 import _head_fields

    pre = _pre_executing_statuses()
    plans_dir = root / _PLANS_DIR
    try:
        entries = sorted(os.scandir(plans_dir), key=lambda e: e.name)
    except OSError:
        return []
    found: List[tuple] = []
    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        try:
            with open(entry.path, "rb") as fh:
                head = fh.read(_SCAN_BYTES).decode("utf-8", "replace")
            named = _HEAD_PLAN_ID_RE.search(head)
            if named is None or named.group(1) not in linked:
                continue
            fm = _head_fields(entry.path, _HEAD_BYTES, _HEAD_WANT)
        except OSError:
            continue
        plan_id, status = fm.get("plan_id"), fm.get("status")
        if isinstance(plan_id, str) and plan_id in linked and (status in pre or status == _TARGET_STATUS):
            found.append((f"{_PLANS_DIR}/{entry.name}", plan_id, status))
    return found


def _coded_rows(text: str) -> List[str]:
    """Ids of spine rows at ``disposition: coded`` that carry a ``disposition_ref``."""
    if not _CODED_HINT_RE.search(text):
        return []
    import yaml

    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return []
    try:
        rows = yaml_load(located.body) or []
    except yaml.YAMLError:
        return []
    if not isinstance(rows, list):
        return []
    return [
        str(r["id"])
        for r in rows
        if isinstance(r, dict) and r.get("id") and r.get("disposition") == "coded" and r.get("disposition_ref")
    ]


def _candidates(root: Path) -> List[Dict[str, Any]]:
    linked = _manifest_runs(root)
    if not linked:
        return []
    out: List[Dict[str, Any]] = []
    for rel, plan_id, status in _pre_executing_plans(root, linked):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        coded = _coded_rows(text)
        if not coded:
            continue
        from coordinator_core.ops.dispatch_emit.spine_read import coded_or_withheld

        target = _LANDED_STATUS if coded_or_withheld(text) else _TARGET_STATUS
        if status == _TARGET_STATUS and target != _LANDED_STATUS:
            continue
        out.append(
            {"plan": rel, "plan_id": plan_id, "status": status, "target_status": target,
             "coded_rows": coded, "runs": linked[plan_id]}
        )
    return out


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 -- git object id, not security


def _clean_against_head(root: Path, rels: List[str]) -> Dict[str, Optional[str]]:
    """``{rel: None}`` when the plan's bytes equal HEAD's blob, else the reason it is not clean.

    A CRLF checkout of an LF blob (``core.autocrlf``) counts as clean."""
    from coordinator_core.git.git_state import head_blobs

    tracked = head_blobs(root, rels)
    verdict: Dict[str, Optional[str]] = {}
    for rel in rels:
        entry = tracked.get(rel)
        data = (root / rel).read_bytes()
        if entry is None:
            verdict[rel] = "untracked: HEAD carries no such plan"
        elif entry[1] not in (_blob_sha(data), _blob_sha(data.replace(b"\r\n", b"\n"))):
            verdict[rel] = "dirty: plan differs from HEAD"
        else:
            verdict[rel] = None
    return verdict


def _flip(root: Path, rel: str, target_status: str = _TARGET_STATUS) -> Optional[str]:
    """Set the plan's status to ``target_status`` under ``locked_rmw``, keeping CRLF. A skip reason or None."""
    from coordinator_core.locked_write import MutateAbort, locked_rmw
    from coordinator_core.roadmap.blitz_land import LandingRefused, _read_field, _set_field

    target = root / rel
    crlf = b"\r\n" in target.read_bytes()
    pre = _pre_executing_statuses() | ({_TARGET_STATUS} if target_status == _LANDED_STATUS else frozenset())

    def _stamp(old: str) -> str:
        current = (_read_field(old, "status") or "").lower()
        if current not in pre:
            raise MutateAbort(f"status is now {current or 'absent'}, no longer advanceable")
        new = _set_field(old, "status", target_status)
        return new.replace("\n", "\r\n") if crlf else new

    try:
        locked_rmw(target, _stamp, repo_root=root)
    except (MutateAbort, LandingRefused) as exc:
        return str(exc)
    return None


def _commit(root: Path, rels: List[str], noun_status: str = _TARGET_STATUS) -> Dict[str, Any]:
    from coordinator_core.roadmap.blitz_stage import _commit_paths

    noun = "plan" if len(rels) == 1 else "plans"
    message = f"plan.backfill_warp_executing: advance {len(rels)} warp-coded {noun} to {noun_status}"
    try:
        reply = _commit_paths(root, rels, message)
    except Exception as exc:  # noqa: BLE001 -- the flips stand on disk; the failure is reported
        return {"commit_sha": None, "commit_error": f"{type(exc).__name__}: {exc}"}
    if reply.get("committed"):
        return {"commit_sha": reply.get("sha"), "commit_error": None}
    return {"commit_sha": None, "commit_error": str(reply.get("error") or reply)}


@register_op("plan.backfill_warp_executing")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "plan.backfill_warp_executing" handler. See module docstring."""
    if repo_root is None:
        raise CallerFacingValidationError("plan.backfill_warp_executing requires a resolved repo_root")
    extra = sorted(set(params) - _PARAMS)
    if extra:
        raise CallerFacingValidationError(
            f"unknown param(s): {', '.join(repr(k) for k in extra)}. Valid: apply, plan_ids"
        )
    apply = params.get("apply", False)
    if not isinstance(apply, bool):
        raise CallerFacingValidationError("apply must be a boolean when supplied")
    plan_ids = params.get("plan_ids")
    if plan_ids is not None and (
        not isinstance(plan_ids, list) or not plan_ids or not all(isinstance(p, str) and p for p in plan_ids)
    ):
        raise CallerFacingValidationError("plan_ids must be a non-empty list of non-empty strings when supplied")

    from coordinator_core.lifecycle import main_worktree_root

    root = Path(main_worktree_root(repo_root))
    candidates = _candidates(root)
    reply: Dict[str, Any] = {
        "candidates": candidates,
        "applied": [],
        "skipped": [],
        "unmatched_plan_ids": sorted(set(plan_ids) - {c["plan_id"] for c in candidates}) if plan_ids else [],
        "commit_sha": None,
        "commit_error": None,
    }
    if not apply:
        return reply

    picked = [c for c in candidates if plan_ids is None or c["plan_id"] in plan_ids]
    chosen = [c["plan"] for c in picked]
    targets = {c["plan"]: c["target_status"] for c in picked}
    verdicts = _clean_against_head(root, chosen) if chosen else {}
    flipped: List[str] = []
    for rel in chosen:
        reason = verdicts[rel] or _flip(root, rel, targets[rel])
        if reason:
            reply["skipped"].append({"plan": rel, "reason": reason})
        else:
            flipped.append(rel)
    reply["applied"] = flipped
    if flipped:
        kinds = {targets[r] for r in flipped}
        reply.update(_commit(root, flipped, kinds.pop() if len(kinds) == 1 else "executing/landed"))
    return reply
