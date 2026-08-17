"""
coordinator_core.ops.deliverable_fork_detect — JSON-RPC "deliverable.fork_detect"
operation (C7).

Purpose: `deliverable_equivalence.seed_deliverable_ledger_rows` has zero production
callers — every fork minted after the frozen 2026-08-01 audit
(`state/deliverable-equivalence.yaml`, 19 hand-adjudicated rows) is invisible to
`canonicalize()` permanently, including this incident's third id (the 40/42/45
triple — see this module's plan's own § Problem). This op gives the seeder its first
live caller: it reads the seeded rows' canonical ids and clusters THOSE ids by
slug-prefix family (the `…fence-inve-fc3678` / `…fence-invent-903224` /
`…fence-inventory-df74c5` shape — one shared source string, truncated at three
different lengths before the mint-time hash suffix), surfacing a candidate fork
family for a human to adjudicate.

STRUCTURAL BOUNDARY (staff-eng-063f0261 finding 6): this module is deliberately
separate from `deliverable_equivalence.py`, the module that owns
`load_equivalence_map`, the ledger loader, and the seeder — the only module in the
repo already carrying the equivalence map's write-shape knowledge in scope. This
module:

  - imports `seed_deliverable_ledger_rows` and `canonicalize` from
    `deliverable_equivalence.py` READ-SIDE ONLY, and never references
    `state/deliverable-equivalence.yaml`'s path or any writer of it;
  - imports the slug-prefix-family clustering predicate from
    `coordinator_core.ops.slug_prefix_family` (C4's module) rather than
    re-deriving the string-prefix/truncation-length/hash logic a second time
    (staff-eng-063f0261 finding 7);
  - emits its own report shape, `{family: [ids], evidence_paths: [...]}`, with NO
    `winner` field and no `superseded_by`/`status`/`closed_at`/`adjudicator` slot —
    a shape with nowhere to put a winner cannot silently grow one, unlike passing
    seed rows (which already carry all four fields) through unmodified.

OUT OF SCOPE, deliberately (Anti-scope / § Out of scope): no automatic winner/loser
adjudication, no automatic row write into the equivalence map. Winner selection
stays human. This op never opens `state/deliverable-equivalence.yaml` in any write
mode, and never imports a writer of it.

Cadence posture: mirrors `coordinator_core.ops.cascade_backstop_sweep` — registered
as an op (`coordinator_core/ops/_registry_map.py`), invocable by name, reachable
from no boot/commit-path trigger. Not wired to any `coordinator/bin/` CLI wrapper
(one seam, not two half-built ones) and not on the boot path (DR-271's four hard
constraints for boot-path swathe work — performant, idempotent, Windows/macOS
first-class, lazy-running — are unmet here for the same reason cited by C4/C6c).

Spec backlink: docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md § C7
(AC12; staff-eng-063f0261 finding 6, finding 7, finding 9)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.deliverable_equivalence import (
    canonicalize,
    seed_deliverable_ledger_rows,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.slug_prefix_family import cluster_slug_prefix_families

SCHEMA_VERSION = 1


def detect_slug_prefix_fork_families(worktree_root: Path) -> List[Dict[str, Any]]:
    """Detect slug-prefix fork families among `seed_deliverable_ledger_rows`'s
    OUTPUT ids.

    Two of this incident's 40/42/45 triple are already declared equivalent in
    `state/deliverable-equivalence.yaml`, so `canonicalize()` already collapses
    them into one seeded row; that collapse is NOT what this function detects
    (it is the map's own, already-correct behaviour). What `canonicalize()`
    cannot see is a NEW, undeclared prefix collision among the seeded rows'
    canonical ids themselves — this function clusters those canonical ids by
    `cluster_slug_prefix_families` (the shared C4/C7 predicate) to surface
    exactly that shape.

    Returns a list of `{"family": [<ids>], "evidence_paths": [<repo-relative
    paths>]}` dicts, one per detected family (2+ members), sorted by family.
    `evidence_paths` unions every seeded row's own `evidence_source` entries
    for every id in the family, deduplicated and sorted — the corpus paths a
    human adjudicator would start from. No `winner`, `superseded_by`,
    `status`, `closed_at`, or `adjudicator` field is present anywhere in this
    shape (STRUCTURAL BOUNDARY, module docstring).

    Zero writes: this function only calls `seed_deliverable_ledger_rows`
    (itself read-only) and a pure clustering function — it never opens
    `state/deliverable-equivalence.yaml` in any write mode.
    """
    rows = seed_deliverable_ledger_rows(worktree_root)
    evidence_by_id: Dict[str, List[str]] = {}
    for row in rows:
        # seed_deliverable_ledger_rows already routed every raw id through
        # canonicalize() against the full equivalence map — re-canonicalizing
        # against an EMPTY map here is a no-op by construction (identity),
        # and asserts the row's id is genuinely the seeder's stable output
        # rather than a raw id that slipped through unrouted.
        canonical_id = canonicalize(row["deliverable_id"], {})
        raw_evidence = str(row.get("evidence_source") or "")
        paths = [p.strip() for p in raw_evidence.split(";") if p.strip()]
        evidence_by_id.setdefault(canonical_id, []).extend(paths)

    families = cluster_slug_prefix_families(evidence_by_id.keys())

    report: List[Dict[str, Any]] = []
    for family in families:
        evidence_paths: List[str] = []
        for family_id in family:
            evidence_paths.extend(evidence_by_id.get(family_id, []))
        report.append(
            {
                "family": family,
                "evidence_paths": sorted(set(evidence_paths)),
            }
        )

    return report


@register_op("deliverable.fork_detect")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "deliverable.fork_detect" handler — read-only fork-family detector.

    Deliberately a plain `def`, not `async def`: it does no awaiting, so it
    routes through `ipc.dispatch_message`'s SYNC branch where its dispatch
    timeout is actually enforceable (`test_async_handler_discipline.py`'s
    zero-await rule — see that gate's module docstring for the incident this
    prevents, and this module's own comment history for the prior async
    shape).

    No required params.

    Returns:
        {
          "schema_version": 1,
          "families_checked": <int>,   # count of seeded canonical ids clustered
          "families": [
            {"family": [<ids>], "evidence_paths": [<relpath>, ...]}, ...
          ],
        }

    `families` is the detection/reporting result of
    `detect_slug_prefix_fork_families` — see that function's own docstring and
    this module's STRUCTURAL BOUNDARY section. `exit_code` is always 0; this op
    reports, it never writes and never fails on an empty result (an empty
    `families` list IS the clean-scan result, matching
    `cascade_backstop_sweep`'s identical "reports, never flips" posture).
    """
    if repo_root is None:
        return {
            "exit_code": 1,
            "error": "deliverable.fork_detect: repo_root is required (no founding root available)",
        }

    worktree_root = main_worktree_root(repo_root)
    seeded_ids = {row["deliverable_id"] for row in seed_deliverable_ledger_rows(worktree_root)}
    families = detect_slug_prefix_fork_families(worktree_root)

    return {
        "exit_code": 0,
        "schema_version": SCHEMA_VERSION,
        "families_checked": len(seeded_ids),
        "families": families,
    }
