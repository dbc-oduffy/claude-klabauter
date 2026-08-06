"""
coordinator_core.ops.cascade_backstop_sweep — JSON-RPC "deliverable.cascade_backstop_sweep"
operation (C6c; AC6d).

Purpose: `/update-docs` and `/distill` already walk this material as a periodic
maintenance pass — this op gives them a SHARED, read-only re-derivation of what
`deliverable.cascade_terminal` (C6) would have produced for every terminal
deliverable this repo already knows about, so a missed event (a stamp that
landed while the cascade was broken, or a pre-existing strand like
Example-cockpit-repo's four instances) is caught rather than silently persisting.

HARD BOUNDARY (Anti-scope, second bullet): this sweep REPORTS. It does not
flip, and it never will. By construction a sweep is SCANNING rather than
observing an event, so anything it concludes is INFERENCE — exactly what R1's
event-driven cascade design deliberately keeps off the write path (see
`docs/decisions/DR-217-terminal-transition-archival-event-model.md`, and this
plan's own Anti-scope bullet drawing the identical discriminator for C6's
fan-out). A divergence this op finds is a prompt for a human, or for a re-run
of the REAL trigger (`plan-status-transition stamp-implemented`, or the
handoff-conclusion leg C6b wires into `post_commit_tail`) — never a write this
op performs itself. "Reports but never flips" is a POLICY choice once C6's
per-target predicate exists (the only difference between this sweep and the
cascade is the trigger) — `docs/decisions/DR-263-terminal-cascade-join-closure-fanout-and-retraction-provenance.md`
§6 records that choice, and §4 names the revisit trigger it may only be reversed
through (the per-target predicate AND a cited decision event, neither optional
cover for the other); this module is required to stay inside it, not merely cite
it.

Composes C6's own candidate-collection and per-target predicate in-process
(never re-derives either) — the ONLY difference from `deliverable.cascade_terminal`
is that this op never reaches `_advance_one`/`stamp_shipped_in`/`locked_rmw` for
any candidate, under any outcome. See NEGATIVE-SPEC below.

Terminal-node sources re-scanned (mirrors the two triggers C6/C6b actually
fire from — see `deliverable_cascade.py`'s own module docstring "source_kind"
contract):
  - `docs/plans/*.md` carrying `status: implemented` and a `deliverable_id` —
    the plan trigger's condition (`plan_status_transition._run_cascade`).
  - `state/handoffs/*.md` AND `archive/handoffs/**/*.md` whose
    `deployment_state` is a member of `HANDOFF_TERMINAL_DEPLOYMENT` and which
    carry a `deliverable_id` — the handoff-conclusion trigger's condition
    (`post_commit_tail._run_deliverable_cascade`'s "stamped" set, generalized
    from "just stamped this run" to "terminal on disk, whenever that happened"
    — exactly the generalization a backstop sweep needs to catch a MISSED
    event, not only a live one).

For each DISTINCT `deliverable_id` collected from those sources, this op
re-runs C6's own `_collect_live_candidates` + `_predicate_refusal` against
`state/handoffs/` (dry-run — no write attempted for any outcome) and reports
every candidate that CLEARS the predicate as a divergence: a live handoff that
the real cascade would advance today, had it fired. A candidate the predicate
REFUSES is not a divergence — refusal is the cascade's own correct, named
"not safe to advance" outcome (AC6h), identical whether reached by an event or
this sweep.

Self-registration: importing this module calls
register_op("deliverable.cascade_backstop_sweep") as a side-effect. Added to
coordinator_core/ops/__init__.py's eager-import table so registration fires
at start_server() time.

Spec backlink: docs/plans/2026-08-04-terminal-state-propagation-join-keys.md § C6c (AC6d)

NEGATIVE-SPEC:
  - Does NOT write, anywhere, under any code path. No `locked_rmw`, no
    `stamp_shipped_in`, no `build_ship_mutate`, no frontmatter mutation of
    any kind. A caller wanting the real cascade to run calls
    `deliverable.cascade_terminal` (or the real trigger) directly — this op
    is never a substitute entrypoint for it.
  - Does NOT re-implement `_collect_live_candidates` or `_predicate_refusal`
    — both are imported from `deliverable_cascade` and called exactly as C6
    itself calls them, so a future change to the predicate is automatically
    picked up here with no parallel edit.
  - Does NOT build C11's cockpit ground-truth fixture (that chunk's own
    surface) and does NOT write `docs/decisions/*` (C-DR's own surface).
  - Does NOT wire itself into any auto-firing hook, cron, or commit-path
    trigger — it is read-only compute a slash command composes on demand,
    exactly like `ceremony.update_docs_scan`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.deliverable_cascade import (
    _collect_live_candidates,
    _predicate_refusal,
)
from coordinator_core.ops.fleet._common import main_worktree_root

SCHEMA_VERSION = 1
"""First key of the returned manifest (C9 schema_version convention)."""


def _read_fm(path: Path) -> dict:
    """Best-effort frontmatter read. An unreadable/malformed file is quarantined
    (empty dict) rather than raising — a backstop sweep must survive one bad
    file on disk, not abort the whole scan over it."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    split = split_frontmatter(text)
    if split is None:
        return {}
    import yaml

    try:
        return yaml.safe_load(split.fm_text) or {}
    except Exception:  # noqa: BLE001
        return {}


def _terminal_plan_deliverable_ids(worktree_root: Path) -> dict[str, list[str]]:
    """{deliverable_id: [plan relpath, ...]} for every docs/plans/*.md carrying
    `status: implemented` — the exact condition `plan_status_transition._run_cascade`
    fires the real cascade on (see that module's `_stamp_implemented` non-no-op
    branch)."""
    out: dict[str, list[str]] = {}
    plans_dir = worktree_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return out
    for plan_path in sorted(plans_dir.glob("*.md")):
        fm = _read_fm(plan_path)
        if fm.get("status") != "implemented":
            continue
        did = fm.get("deliverable_id")
        if not isinstance(did, str) or not did.strip():
            continue
        rel = plan_path.relative_to(worktree_root).as_posix()
        out.setdefault(did.strip(), []).append(rel)
    return out


def _terminal_handoff_deliverable_ids(worktree_root: Path) -> dict[str, list[str]]:
    """{deliverable_id: [handoff relpath, ...]} for every handoff (live-tree
    `state/handoffs/*.md` OR already-archived `archive/handoffs/**/*.md`) whose
    `deployment_state` is a member of HANDOFF_TERMINAL_DEPLOYMENT — the
    generalized form of `post_commit_tail._run_deliverable_cascade`'s "just
    stamped" condition, widened to "terminal on disk, whenever that happened"
    (a backstop sweep must catch a MISSED event, not only a live one)."""
    out: dict[str, list[str]] = {}
    for base in (worktree_root / "state" / "handoffs", worktree_root / "archive" / "handoffs"):
        if not base.is_dir():
            continue
        for handoff_path in sorted(base.rglob("*.md")):
            try:
                fm = _read_meta(str(handoff_path))
            except Exception:  # noqa: BLE001 — quarantine an unreadable/malformed record
                continue
            if not fm:
                continue
            if fm.get("deployment_state") not in HANDOFF_TERMINAL_DEPLOYMENT:
                continue
            did = fm.get("deliverable_id")
            if not isinstance(did, str) or not did.strip():
                continue
            rel = handoff_path.relative_to(worktree_root).as_posix()
            out.setdefault(did.strip(), []).append(rel)
    return out


@register_op("deliverable.cascade_backstop_sweep")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deliverable.cascade_backstop_sweep" handler — read-only backstop.

    No required params. Optional:
        deliverable_id (str) — restrict the sweep to one join key instead of
                                every terminal source found on disk.

    Returns:
        {
          "schema_version": 1,
          "sources_scanned": {"plans": <int>, "handoffs": <int>},
          "deliverable_ids_checked": <int>,
          "divergences": [
            {
              "deliverable_id": ..., "handoff_path": ...,
              "terminal_sources": [<relpath>, ...],
            }, ...
          ],
          "scan_incomplete": <bool>,
        }

    A `divergences` entry names a LIVE handoff that C6's own predicate would
    clear (i.e. the real cascade would advance it today) for a deliverable_id
    this sweep already found a terminal source for on disk. Nothing is ever
    written here — see module NEGATIVE-SPEC. `exit_code` is always 0; this op
    reports, it does not fail — an empty `divergences` list IS the clean-sweep
    result, not a degenerate one, so exit_code carries no separate signal
    (unlike deliverable_cascade's own "advanced empty is failure" contract,
    which does not apply to a read-only report).
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": "deliverable.cascade_backstop_sweep: repo_root is required (no founding root available)",
        }

    worktree_root = main_worktree_root(repo_root)
    only_did = (params.get("deliverable_id") or "").strip() or None

    plan_ids = _terminal_plan_deliverable_ids(worktree_root)
    handoff_ids = _terminal_handoff_deliverable_ids(worktree_root)

    all_ids: dict[str, list[str]] = {}
    for did, sources in plan_ids.items():
        all_ids.setdefault(did, []).extend(sources)
    for did, sources in handoff_ids.items():
        all_ids.setdefault(did, []).extend(sources)

    if only_did is not None:
        all_ids = {did: sources for did, sources in all_ids.items() if did == only_did}

    divergences: list[dict[str, Any]] = []
    scan_incomplete = False

    for did in sorted(all_ids):
        candidates, incomplete = _collect_live_candidates(worktree_root, did)
        if incomplete:
            scan_incomplete = True
        for candidate in candidates:
            refusal = await _predicate_refusal(candidate["path"], candidate["fm"], repo_root)
            if refusal is None:
                divergences.append(
                    {
                        "deliverable_id": did,
                        "handoff_path": str(candidate["path"]),
                        "terminal_sources": sorted(all_ids[did]),
                    }
                )

    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "sources_scanned": {"plans": len(plan_ids), "handoffs": len(handoff_ids)},
        "deliverable_ids_checked": len(all_ids),
        "divergences": divergences,
        "scan_incomplete": scan_incomplete,
    }
