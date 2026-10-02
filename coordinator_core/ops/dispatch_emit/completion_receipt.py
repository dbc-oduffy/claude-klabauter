"""
coordinator_core.ops.dispatch_emit.completion_receipt -- the run-end receipt builder.

Purpose: § Design D2-D4 of docs/plans/2026-10-01-completion-receipts.md. Composes one
CompletionReceipt (frontmatter + prose) per baton a concluding execute-plan, warp or mise
run claimed, and writes them for ``dispatch.terminal_commit`` to fold into its single
commit. Pure-ish: reads claims, plans, batons, sizings and the emitted-script sidecar from
disk; spawns no subprocess.

Negative-spec:
  - Never writes ``commit_range.head``: a file cannot name the commit that contains it;
    readers recover it with ``store.introducing_commits``.
  - Never copies ``_source_rows_by_plan`` / ``_MINTED_SPINE_ORIGIN``; they are imported.
  - Never decides ``agent-delivered`` itself: ``verdict.judge_verdict`` does.
  - An empty ``build_run_receipts`` result means the run could not be tied to any baton or
    deliverable (``receipt_coverage: "unidentified"``); it is not an error.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.completion_receipts.model import (
    TSHIRTS,
    mint_receipt_id,
    receipt_rel_path,
)
from coordinator_core.completion_receipts.store import write_receipt
from coordinator_core.completion_receipts.verdict import judge_verdict, mint_refusal
from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.session import record_homes
from coordinator_core.ops.dispatch_emit.commit_request import (
    plan_deliverable_id,
    valid_deliverable_id,
)
from coordinator_core.ops.dispatch_emit.terminal_commit import (
    _MINTED_SPINE_ORIGIN,
    _SPINE_SUFFIX,
    _read_rel,
    _source_rows_by_plan,
)

_DERIVATION = "dispatch.terminal_commit"
_HANDOFF_CLASS = "handoff"


def _fm(worktree_root: Path, rel: Optional[str]) -> dict:
    """Frontmatter mapping of a repo-relative file; ``{}`` when absent or unparseable."""
    if not rel:
        return {}
    text = _read_rel(worktree_root, rel)
    split = split_frontmatter(text.replace("\r\n", "\n")) if text is not None else None
    if split is None:
        return {}
    try:
        doc = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _str(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _iso(value: Any) -> Optional[str]:
    """An ISO timestamp with an explicit UTC designator, or ``None``."""
    text = _str(value)
    if text is None or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text):
        return None
    return text if re.search(r"(Z|[+-]\d{2}:?\d{2})$", text) else text + "Z"


def _repo_key(worktree_root: Path) -> str:
    return re.sub(r"[^a-z0-9]+", "_", Path(worktree_root).resolve().name.lower()).strip("_")


def _estimate(worktree_root: Path, sizing_rel: Optional[str]) -> Optional[str]:
    estimate = _fm_sizing(worktree_root, sizing_rel).get("estimate")
    tshirt = estimate.get("tshirt") if isinstance(estimate, dict) else None
    return tshirt if tshirt in TSHIRTS else None


def _actual_loe(worktree_root: Path, session_id: Optional[str]) -> Optional[str]:
    """T-shirt from the session's dispatch ledger; ``None`` when it does not resolve."""
    if not session_id:
        return None
    from coordinator_core.loe_thresholds import compute_tshirt_nullable
    from coordinator_core.ops.coordinator_complete_entry import _count_session_dispatches
    from coordinator_core.session import core as session_core

    base = session_core.sessions_dir(str(worktree_root))
    if not base:
        return None
    ad, od = _count_session_dispatches(Path(base) / session_id / "dispatched-agents.txt")
    tshirt = compute_tshirt_nullable(ad, od, None)
    return tshirt if tshirt in TSHIRTS else None


def _started_at(worktree_root: Path, script_path: str) -> Optional[str]:
    rel = script_path + ".emitted.json"
    text = _read_rel(worktree_root, rel)
    if text is None:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return _iso(doc.get("emitted_at")) if isinstance(doc, dict) else None


def _handoff_claims(worktree_root: Path, session_id: Optional[str]) -> list[str]:
    """Repo-relative baton paths of every handoff-class claim this session holds."""
    if not session_id:
        return []
    from coordinator_core.session.claims import list_claims_by_session_checked

    matches, _errors = list_claims_by_session_checked(session_id, str(worktree_root))
    return [
        Path(record_homes.record_path("", "handoffs", name)).as_posix()
        for class_, name in matches
        if class_ == _HANDOFF_CLASS
    ]


def _plan_rows_landed(
    worktree_root: Path, plan_rel: Optional[str], landed: set
) -> bool:
    """Every spine row of the plan is closed (not ``open``) or in ``landed``."""
    text = _read_rel(worktree_root, plan_rel) if plan_rel else None
    if text is None:
        return False
    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return False
    try:
        rows = yaml.safe_load(located.body) or []
    except yaml.YAMLError:
        return False
    if not isinstance(rows, list):
        return False
    return all(
        str(r.get("id")) in landed or _row_disposition(r) != "open"
        for r in rows
        if isinstance(r, dict) and r.get("id")
    )


def _observed(record: Optional[dict]) -> str:
    if not isinstance(record, dict):
        return "no review record reached the terminal commit"
    prep, build_test = record.get("prep"), record.get("tests")
    if isinstance(prep, dict) and isinstance(build_test, dict):
        refusal = mint_refusal(record, prep, build_test)
        if refusal is not None:
            return refusal
    criterion = record.get("criterion")
    if isinstance(criterion, dict):
        observation = _str(criterion.get("observation"))
        status = _str(criterion.get("status")) or "unrecorded"
        return f"criterion {status}: {observation}" if observation else f"criterion {status}"
    return "criterion not recorded"


def _prose(done_ids: list, incomplete_ids: list, record: Optional[dict]) -> str:
    concluded = ", ".join(done_ids) if done_ids else "none"
    remains = ", ".join(incomplete_ids) if incomplete_ids else "none"
    return (
        f"## Concluded\n\n{concluded}\n\n"
        f"## Observed\n\n{_observed(record)}\n\n"
        f"## Remains\n\n{remains}\n"
    )


def _baton_id(baton_rel: str, fm: dict) -> str:
    return _str(fm.get("handoff_id")) or Path(baton_rel).stem


def _compose(
    worktree_root: Path,
    *,
    slug_source: str,
    baton_id: Optional[str],
    deliverable_id: Optional[str],
    workstream_id: Optional[str],
    plan_path: Optional[str],
    run_kind: str,
    run_id: str,
    verdict_pair: tuple,
    started_at: Optional[str],
    loe: dict,
    branch: Optional[str],
    base_sha: Optional[str],
    now: str,
    prose: str,
) -> tuple[dict, str]:
    receipt_id = mint_receipt_id(slug_source)
    verdict, judge = verdict_pair
    fm = {
        "schema": "completion-receipt",
        "receipt_id": receipt_id,
        "baton_id": baton_id,
        "deliverable_id": deliverable_id,
        "workstream_id": workstream_id,
        "repo": _repo_key(worktree_root),
        "plan_path": plan_path,
        "branch": branch,
        "run": {"kind": run_kind, "id": run_id},
        "verdict": verdict,
        "judge": judge,
        "commit_range": {"base": base_sha, "head": None},
        "started_at": started_at,
        "concluded_at": now,
        "loe": loe,
        "prose_ref": receipt_rel_path(receipt_id, now),
        "supersedes": None,
        "approved_by": None,
        "provenance": {
            "observed_at": now,
            "derivation": _DERIVATION,
            "ref": {"branch": branch, "sha": base_sha},
        },
    }
    return fm, prose


def build_run_receipts(
    worktree_root: Path,
    request: Any,
    *,
    done_ids: list,
    incomplete_ids: list,
    record: Optional[dict],
    script_path: str,
    session_id: Optional[str],
    base_sha: Optional[str],
    branch: Optional[str],
    now: str,
) -> list[tuple[dict, str]]:
    """One ``(frontmatter, prose)`` receipt per baton the run claimed.

    Plan and warp runs record ``run.kind: execute-plan``; a run whose marker names a
    minted mise spine records ``mise-en-place`` (plan § D3). ``[]`` when no baton or
    deliverable identifies the run.
    """
    worktree_root = Path(worktree_root)
    plan_rel = request.plan_path
    plan_fm = _fm(worktree_root, plan_rel)
    run_id = Path(script_path).stem
    started_at = _started_at(worktree_root, script_path)
    actual = _actual_loe(worktree_root, session_id)
    prose = _prose(list(done_ids), list(incomplete_ids), record)
    common = dict(
        run_id=run_id, started_at=started_at, branch=branch, base_sha=base_sha, now=now,
        prose=prose,
    )

    def loe_for(sizing_rel: Optional[str]) -> dict:
        return {"estimated": _estimate(worktree_root, sizing_rel), "actual": actual}

    claims = _handoff_claims(worktree_root, session_id)

    if plan_fm.get("derived_from") == _MINTED_SPINE_ORIGIN:
        return _build_inventory(
            worktree_root, request, claims, done_ids, incomplete_ids, record, loe_for, common
        )

    deliverable = request.deliverable_id or valid_deliverable_id(plan_fm.get("deliverable_id"))
    sizing_rel = _str(plan_fm.get("sizing_object"))
    landed = (
        _source_rows_by_plan(worktree_root, plan_rel, list(done_ids)).get(plan_rel, set())
        if plan_rel
        else set()
    )
    all_landed = not incomplete_ids and _plan_rows_landed(worktree_root, plan_rel, landed)
    verdict_pair = judge_verdict(record, all_rows_landed=all_landed, now=now)

    batons = []
    for baton_rel in claims:
        fm = _fm(worktree_root, baton_rel)
        if deliverable and fm.get("deliverable_id") == deliverable:
            batons.append((baton_rel, fm))
    if not batons:
        sized_baton = _str(_fm_sizing(worktree_root, sizing_rel).get("baton"))
        if sized_baton and _read_rel(worktree_root, sized_baton) is not None:
            batons.append((sized_baton, _fm(worktree_root, sized_baton)))
    if not batons and not deliverable:
        return []
    if not batons:
        batons.append((None, {}))

    out = []
    for baton_rel, fm in batons:
        slug = Path(baton_rel).stem if baton_rel else (Path(plan_rel).stem if plan_rel else run_id)
        out.append(
            _compose(
                worktree_root,
                slug_source=slug,
                baton_id=_baton_id(baton_rel, fm) if baton_rel else None,
                deliverable_id=deliverable or _str(fm.get("deliverable_id")),
                workstream_id=_str(fm.get("workstream")) or _str(plan_fm.get("workstream")),
                plan_path=plan_rel,
                run_kind="execute-plan",
                verdict_pair=verdict_pair,
                loe=loe_for(sizing_rel),
                **common,
            )
        )
    return out


def _fm_sizing(worktree_root: Path, sizing_rel: Optional[str]) -> dict:
    text = _read_rel(worktree_root, sizing_rel) if sizing_rel else None
    if text is None:
        return {}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _build_inventory(
    worktree_root: Path,
    request: Any,
    claims: list,
    done_ids: list,
    incomplete_ids: list,
    record: Optional[dict],
    loe_for,
    common: dict,
) -> list[tuple[dict, str]]:
    plan_rel = request.plan_path
    inventory_rel = (
        plan_rel[: -len(_SPINE_SUFFIX)] + ".md" if plan_rel.endswith(_SPINE_SUFFIX) else None
    )
    batons = list(claims)
    source = _str(_fm(worktree_root, inventory_rel).get("source_baton")) if inventory_rel else None
    if source:
        source_rel = _resolve_source_baton(worktree_root, inventory_rel, source)
        if source_rel and source_rel not in batons:
            batons.append(source_rel)

    planned = _source_rows_by_plan(
        worktree_root, plan_rel, list(done_ids) + list(incomplete_ids)
    )
    landed = _source_rows_by_plan(worktree_root, plan_rel, list(done_ids))
    plans_by_deliverable: dict = {}
    for source_plan in planned:
        text = _read_rel(worktree_root, source_plan)
        did = plan_deliverable_id(text) if text is not None else None
        if did:
            plans_by_deliverable.setdefault(did, []).append(source_plan)

    out = []
    for baton_rel in batons:
        fm = _fm(worktree_root, baton_rel)
        deliverable = valid_deliverable_id(fm.get("deliverable_id"))
        joined = plans_by_deliverable.get(deliverable, []) if deliverable else []
        baton_landed = bool(joined) and all(
            planned[p] <= landed.get(p, set()) for p in joined
        )
        verdict_pair = judge_verdict(record, all_rows_landed=baton_landed, now=common["now"])
        sizing_rel = _str(fm.get("sizing_object")) or (
            _str(_fm(worktree_root, joined[0]).get("sizing_object")) if joined else None
        )
        out.append(
            _compose(
                worktree_root,
                slug_source=Path(baton_rel).stem,
                baton_id=_baton_id(baton_rel, fm),
                deliverable_id=deliverable,
                workstream_id=_str(fm.get("workstream")),
                plan_path=joined[0] if joined else None,
                run_kind="mise-en-place",
                verdict_pair=verdict_pair,
                loe=loe_for(sizing_rel),
                **common,
            )
        )
    return out


def _resolve_source_baton(
    worktree_root: Path, inventory_rel: str, source: str
) -> Optional[str]:
    """The ``source_baton`` path (relative to the inventory record) as a repo-relative path."""
    candidate = (worktree_root / inventory_rel).parent / source
    try:
        rel = Path(os.path.normpath(candidate)).resolve().relative_to(worktree_root.resolve())
    except ValueError:
        return None
    rel_posix = rel.as_posix()
    return rel_posix if _read_rel(worktree_root, rel_posix) is not None else None


def write_run_receipts(worktree_root: Path, built: list[tuple[dict, str]]) -> list[str]:
    """Write every built receipt; on the first failure delete those already written and re-raise.

    Returns the repo-relative paths written, in input order.
    """
    worktree_root = Path(worktree_root)
    written: list[str] = []
    try:
        for fm, prose in built:
            written.append(write_receipt(worktree_root, fm, prose))
    except Exception:
        _remove(worktree_root, written)
        raise
    return written


def _remove(worktree_root: Path, rels: list[str]) -> None:
    for rel in rels:
        try:
            (worktree_root / rel).unlink()
        except OSError:
            pass
