"""
coordinator_core.ops.init_anchor_injection_state — JSON-RPC
"ceremony.init_anchor_injection_state" operation.

Purpose: ports the Phase A0 state-init preamble of the workday-complete anchor-
injection ceremony (`commands/workday-complete.md:437` in the example-doctrine-repo tree —
distinct-ops-new.tsv row "init-anchor-injection-state"). The bash fence this
replaces resolved the coordinator/example-doctrine-repo root, computed today's date, and declared
two empty accumulator shell vars ahead of a multi-step anchor-injection loop
that appends to them across later phases. This op reproduces exactly that: a
pure resolve-and-seed step with NO writes and NO mutation of any accumulator —
the caller (the next ceremony phase) owns appending to the returned lists.

op-key: ceremony.init_anchor_injection_state
Contract (op-classification.tsv): params: {} -> {doe_root: str, today: str,
    injected_dates: list[str], content_gap_dates: list[str]}

Idempotency (AC7, manifest-rated idempotency-hazard: none): every field is
either a pure resolution (doe_root via coordinator_doe_root()), a pure
computation of the current date (today), or a fixed empty-list literal
(injected_dates / content_gap_dates). Two invocations on the same calendar day
with the same params ({}) are byte-identical; no disk state is read beyond the
Example-doctrine-repo-root resolution chain and nothing is written at all, so there is no state
to clobber on re-invocation.

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-
inventory.md § Wave 2 (unclustered — workday-complete Phase A0 state init)

Negative-spec:
    - Does NOT write any file — this is a pure in-memory resolve/init step;
      the accumulator lists start and stay empty here.
    - Does NOT derive doe_root via __file__/parents[n] traversal — resolves
      exclusively through coordinator_doe_root() (plan § Mandated resolvers),
      never a literal or dynamically-joined path.
    - Does NOT accept or require any params — the manifest contract is
      params: {}.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root


@register_op("ceremony.init_anchor_injection_state")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.init_anchor_injection_state" handler.

    Params: none (params: {} per manifest contract; the argument is accepted
        for handler-signature parity with every other registered op but its
        contents are ignored).

    Returns:
        {
            "doe_root": <str — resolved example-doctrine-repo / coordinator-claude root>,
            "today": <str — today's date, ISO 8601 "YYYY-MM-DD">,
            "injected_dates": [],
            "content_gap_dates": [],
        }

    Raises:
        RuntimeError — coordinator_doe_root() could not resolve the example-doctrine-repo
        root. Fails loud rather than degrading to an empty/placeholder
        doe_root: the manifest contract types doe_root as `str`, and a
        ceremony phase silently anchoring against "" would misbehave far
        downstream of this op rather than at the point of failure.
    """
    doe_root = coordinator_doe_root()
    if doe_root is None:
        raise RuntimeError(
            "ceremony.init_anchor_injection_state: cannot resolve the example-doctrine-repo "
            "repo root — coordinator_doe_root() returned no result. Set "
            "repos.example_doctrine_repo in the machine-local registry, or set the "
            "DOE_ROOT/REPO_EXAMPLE_DOCTRINE_REPO env var."
        )
    return {
        "doe_root": doe_root,
        "today": datetime.date.today().isoformat(),
        "injected_dates": [],
        "content_gap_dates": [],
    }
