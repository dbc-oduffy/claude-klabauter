"""
coordinator_core.ops.cascade_divergence_report — JSON-RPC
"deliverable.cascade_divergence_report" operation (C2,
docs/plans/2026-09-23-cascade-write-provenance.md).

Purpose: a single-pass, read-only report answering "which live records owe an
engine terminal that an implemented plan already decided, and is it absent?"
— the requirement-2 half of the bug row this plan discharges
(`state/bug-backlog/2026-08-29-an-engine-written-terminal-state-is-reverted-by-a-stale-whole-file-write.yaml`).
It reads each corpus exactly once (plan headers, targeted live sizing reads,
the handoff corpus) and joins in memory, evaluating the existing DR-263
per-target predicate (`deliverable_cascade._predicate_refusal`) only for the
records that diverge. See that plan's Shape 3 for the full design; this
docstring restates only what a reader of THIS file needs.

NEGATIVE-SPEC (read before touching this module):
  - NEVER writes, anywhere, under any code path. No `locked_rmw`, no
    `_advance_one*`, no `_handler` call from `deliverable_cascade.py`. A
    divergence is a prompt for a human, never a write this op performs.
  - NEVER re-fires the cascade. DR-263 §4/§6 bar a scan-triggered write; a
    re-fire is a PM-gated policy act this report can only make citable, never
    execute.
  - NEVER spawns a process — no git, no subprocess, of any kind.
  - Plays no commit-time role. It has no hook, cron, or ceremony wiring.
  - `provenance: null` does NOT mean "never cascaded" — it means "not written
    by the cascade in THIS copy". A whole-file revert to pre-cascade bytes
    erases `advanced_by`/`advanced_at` along with the terminal it undid, so a
    present provenance value on a non-terminal record is evidence of a
    field-level revert, and an absent one on a non-terminal record is simply
    silent about cause.
  - A "divergence" here means an OWED terminal whose cause is unknown. On day
    one most divergences are cascades that never fired, not reverts — this
    report cannot and does not tell the two apart (see the plan's Out of
    scope). Neither this op's name nor any result key or refusal reason
    claims "reverted".

Reuses `deliverable_cascade`'s own kind descriptors, terminal-status set, and
predicate — never re-implements any of them, per the plan body.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.deliverable_cascade import (
    _HANDOFF_KIND,
    _SIZING_KIND,
    _SIZING_TERMINAL_STATUS,
    _predicate_refusal,
    _read_sizing_meta,
)
from coordinator_core.ops.fleet._common import main_worktree_root

SCHEMA_VERSION = 1

# Plan-header field extraction (Shape 3, "Plan headers") — a line scan over the
# frontmatter fence's own text, never a full YAML parse. Mirrors the plan's own
# census-script regex shape (docs/plans/2026-09-23-cascade-write-provenance.md
# census row 2/3), reused here rather than re-derived by hand a second time.
_HEADER_FIELD_RE: Dict[str, "re.Pattern[str]"] = {
    "status": re.compile(r"^status:\s*(.*)$", re.M),
    "deliverable_id": re.compile(r"^deliverable_id:\s*(.*)$", re.M),
    "sizing_object": re.compile(r"^sizing_object:\s*(.*)$", re.M),
}


def _fm_value(raw: str) -> str:
    """Strip a trailing YAML comment and surrounding quotes; normalise the
    YAML null spellings to an empty string, mirroring the census script's own
    `f(t, k)` helper exactly."""
    value = raw.split(" #", 1)[0].strip().strip("\"'")
    return "" if value in ("null", "~", "") else value


def _read_plan_header(path: Path) -> Dict[str, str]:
    """Read `path` up to its closing frontmatter fence (never a fixed byte
    count — C1 measured a 32,656-byte frontmatter on this tree) and extract
    `status`/`deliverable_id`/`sizing_object` by line scan. No YAML parse.
    Returns {} for a non-frontmatter document or an unreadable file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    head = text.split("\n---", 1)[0]
    out: Dict[str, str] = {}
    for key, pattern in _HEADER_FIELD_RE.items():
        match = pattern.search(head)
        if match:
            out[key] = _fm_value(match.group(1))
    return out


def _iter_plan_paths(worktree_root: Path) -> List[Path]:
    """`docs/plans/*.md` (live) plus `archive/specs/**/*.md` (archived) — the
    same two sources the plan's own census rows scan (row 2)."""
    paths: List[Path] = []
    plans_dir = worktree_root / "docs" / "plans"
    if plans_dir.is_dir():
        paths.extend(sorted(plans_dir.glob("*.md")))
    archive_specs = worktree_root / "archive" / "specs"
    if archive_specs.is_dir():
        paths.extend(sorted(archive_specs.rglob("*.md")))
    return paths


def _collect_sizing_candidates(
    worktree_root: Path, implemented_plans: List[Tuple[Path, Dict[str, str]]]
) -> Tuple[List[Dict[str, Any]], int, int, bool]:
    """For each implemented plan carrying a `sizing_object` back-pointer, read
    that ONE sizing at its live path (`deliverable_cascade._read_sizing_meta`)
    — never opens anything under `archive/sizings/`. A back-pointer with no
    live file is terminal-by-archival and is counted, not opened (Shape 3,
    "Sizing kind"). A sizing whose `status` is not in
    `_SIZING_TERMINAL_STATUS` is a candidate.

    Returns (candidates, sizings_read, back_pointers_not_live, scan_incomplete)
    — `scan_incomplete` is True when at least one live sizing failed to
    read/parse (a malformed record never aborts the scan)."""
    candidates: List[Dict[str, Any]] = []
    sizings_read = 0
    back_pointers_not_live = 0
    scan_incomplete = False
    for plan_path, header in implemented_plans:
        sizing_rel = header.get("sizing_object")
        if not sizing_rel:
            continue
        sizing_path = worktree_root / sizing_rel
        if not sizing_path.is_file():
            back_pointers_not_live += 1
            continue
        try:
            fm = _read_sizing_meta(str(sizing_path))
        except Exception:  # noqa: BLE001 — quarantine a malformed record, never abort
            scan_incomplete = True
            continue
        sizings_read += 1
        status = fm.get("status")
        if status in _SIZING_TERMINAL_STATUS:
            continue
        candidates.append(
            {
                "plan_path": plan_path,
                "sizing_path": sizing_path,
                "fm": fm,
                "plan_deliverable_id": header.get("deliverable_id", ""),
            }
        )
    return candidates, sizings_read, back_pointers_not_live, scan_incomplete


def _collect_handoff_index(
    worktree_root: Path,
) -> Tuple[Dict[str, dict], Dict[Path, dict], int]:
    """One pass over `state/handoffs/*.md`, read through `dag._read_meta` (the
    same reader `_HANDOFF_KIND` uses). Returns (metas_by_abspath, fm_by_path,
    handoffs_read) — `metas_by_abspath` is the `corpus_metas` index leg (b) of
    `_predicate_refusal` reuses instead of re-scanning."""
    metas: Dict[str, dict] = {}
    by_path: Dict[Path, dict] = {}
    handoffs_dir = worktree_root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return metas, by_path, 0
    count = 0
    for path in sorted(handoffs_dir.glob("*.md")):
        try:
            fm = _read_meta(str(path))
        except Exception:  # noqa: BLE001 — quarantine an unreadable/malformed record
            continue
        if not fm:
            continue
        count += 1
        metas[os.path.abspath(str(path))] = fm
        by_path[path] = fm
    return metas, by_path, count


def _provenance(fm: dict) -> Optional[Dict[str, Any]]:
    """`{"advanced_by": ..., "advanced_at": ...}` when the record carries
    either field, else None — a present value on a non-terminal record is
    what names a field-level revert (module docstring)."""
    advanced_by = fm.get("advanced_by")
    advanced_at = fm.get("advanced_at")
    if advanced_by is None and advanced_at is None:
        return None
    return {"advanced_by": advanced_by, "advanced_at": advanced_at}


@register_op("deliverable.cascade_divergence_report")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deliverable.cascade_divergence_report" handler — read-only,
    single-pass, zero-spawn (see module NEGATIVE-SPEC).

    No required params.

    Returns:
        {
          "exit_code": 0,
          "schema_version": 1,
          "sources_scanned": {
            "plans": <int>, "implemented_plans": <int>, "sizings_read": <int>,
            "back_pointers_not_live": <int>, "handoffs_read": <int>,
          },
          "divergences": [
            {"kind", "path", "deliverable_id", "deliverable_id_matches",
             "terminal_source", "lifecycle_value", "provenance"}, ...
          ],
          "refused": [
            {"kind", "path", "deliverable_id", "terminal_source", "reason"}, ...
          ],
          "scan_incomplete": <bool>,
        }

    Every candidate this scan finds lands in exactly one of `divergences` and
    `refused` — never both, never neither. `exit_code` is always 0; a report
    completes whether or not it finds anything (mirrors
    `cascade_backstop_sweep`'s own "reports, never fails" contract).
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": (
                "deliverable.cascade_divergence_report: repo_root is required "
                "(no founding root available)"
            ),
        }

    worktree_root = main_worktree_root(repo_root)

    plan_paths = _iter_plan_paths(worktree_root)
    implemented_plans: List[Tuple[Path, Dict[str, str]]] = []
    for plan_path in plan_paths:
        header = _read_plan_header(plan_path)
        if header.get("status") == "implemented":
            implemented_plans.append((plan_path, header))

    (
        sizing_candidates,
        sizings_read,
        back_pointers_not_live,
        sizing_scan_incomplete,
    ) = _collect_sizing_candidates(worktree_root, implemented_plans)

    handoff_metas, handoff_fm_by_path, handoffs_read = _collect_handoff_index(worktree_root)

    divergences: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []

    # Sizing candidates — leg (b) is EXEMPT for _SIZING_KIND (no successor-edge
    # vocabulary reaches a sizing-object), so corpus_metas is None here: the
    # handoff-corpus index above is never threaded into a sizing candidate's
    # predicate call.
    for candidate in sizing_candidates:
        sizing_path = candidate["sizing_path"]
        fm = candidate["fm"]
        plan_deliverable_id = (candidate["plan_deliverable_id"] or "").strip()
        sizing_deliverable_id_raw = fm.get("deliverable_id")
        sizing_deliverable_id = (
            sizing_deliverable_id_raw.strip()
            if isinstance(sizing_deliverable_id_raw, str)
            else ""
        )
        reason = await _predicate_refusal(
            sizing_path, fm, repo_root, kind=_SIZING_KIND, corpus_metas=None
        )
        entry = {
            "kind": "sizing",
            "path": str(sizing_path),
            "deliverable_id": sizing_deliverable_id,
            "terminal_source": str(candidate["plan_path"]),
        }
        if reason is not None:
            refused.append({**entry, "reason": reason})
            continue
        divergences.append(
            {
                **entry,
                "deliverable_id_matches": bool(plan_deliverable_id)
                and sizing_deliverable_id == plan_deliverable_id,
                "lifecycle_value": fm.get("status"),
                "provenance": _provenance(fm),
            }
        )

    # Handoff candidates — sourced from the plan-trigger cascade only (this
    # scan's own read of docs/plans/*.md / archive/specs/**/*.md), never a
    # separate scan of archive/handoffs/** (that is the handoff-conclusion
    # trigger, out of scope here).
    implemented_plan_ids: Dict[str, List[Path]] = {}
    for plan_path, header in implemented_plans:
        did = (header.get("deliverable_id") or "").strip()
        if did:
            implemented_plan_ids.setdefault(did, []).append(plan_path)

    for handoff_path, fm in handoff_fm_by_path.items():
        did_raw = fm.get("deliverable_id")
        did = did_raw.strip() if isinstance(did_raw, str) else ""
        if not did or did not in implemented_plan_ids:
            continue
        if fm.get("deployment_state") in HANDOFF_TERMINAL_DEPLOYMENT:
            continue
        reason = await _predicate_refusal(
            handoff_path,
            fm,
            repo_root,
            kind=_HANDOFF_KIND,
            corpus_metas=handoff_metas,
        )
        terminal_source = str(sorted(implemented_plan_ids[did])[0])
        entry = {
            "kind": "handoff",
            "path": str(handoff_path),
            "deliverable_id": did,
            "terminal_source": terminal_source,
        }
        if reason is not None:
            refused.append({**entry, "reason": reason})
            continue
        divergences.append(
            {
                **entry,
                "deliverable_id_matches": True,
                "lifecycle_value": fm.get("deployment_state"),
                "provenance": _provenance(fm),
            }
        )

    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "sources_scanned": {
            "plans": len(plan_paths),
            "implemented_plans": len(implemented_plans),
            "sizings_read": sizings_read,
            "back_pointers_not_live": back_pointers_not_live,
            "handoffs_read": handoffs_read,
        },
        "divergences": divergences,
        "refused": refused,
        "scan_incomplete": sizing_scan_incomplete,
    }
