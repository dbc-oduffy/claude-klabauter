"""Landing gates a halt commit applies to declared product, mirroring ``terminal_commit``'s.

A halt commit carries run artifacts; product rides along only when it clears the same gates a
terminal commit applies. ``gate_halt_product`` answers which dirty declared product paths fail one,
``is_scratch`` answers whether a rel is a scratch export copy that never lands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional, Sequence

UNOWNED_PRODUCT = "halt-unowned-product"
SCRATCH_EXPORT = "scratch-export"


def _norm(rel: str) -> str:
    out = rel.replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out


def is_scratch(rel: str) -> bool:
    """True when ``rel`` is ``scratch`` or under ``scratch/``, after separator and ``./`` normalisation."""
    out = _norm(rel)
    return out == "scratch" or out.startswith("scratch/")


def gate_halt_product(
    worktree_root: Path,
    run_id: str,
    product: Sequence[str],
    gone: Sequence[str],
    owner: Mapping[str, str],
    own_declared: Sequence[str],
    *,
    plan: Optional[str],
    provenance: Optional[list] = None,
) -> dict[str, str]:
    """``{path: reason}`` for each of ``product`` and ``gone`` that fails a landing gate: a declared
    path no chunk owns (``halt-unowned-product``), a scratch path (``scratch-export``) or a path a
    co-declaring unlanded sibling run claims and ``co_declared_settle.settle`` keeps held
    (``peer_hunk_gate.PEER_HUNKS_CO_DECLARED``): a provenance kind, appended to ``provenance`` as
    ``(path, reason, sibling)`` and not returned; settled paths clear. Paths
    that clear every gate are absent from the result."""
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    paths = [*product, *gone]
    out: dict[str, str] = {}
    for p in paths:
        if is_scratch(p):
            out[p] = SCRATCH_EXPORT
        elif p not in owner:
            out[p] = UNOWNED_PRODUCT
    rest = [p for p in paths if p not in out]
    if rest:
        from coordinator_core.ops.dispatch_emit import co_declared_settle

        holds = peer_hunk_gate.co_declared_peer_holds(
            worktree_root, run_id, rest, own_declared, own_plan=plan,
        )
        _stage, still_held = co_declared_settle.settle(worktree_root, run_id, holds, own_plan=plan)
        from coordinator_core.ops.dispatch_emit.landing_refusal_kinds import is_refusal

        for p, sibling in still_held:
            if is_refusal(peer_hunk_gate.PEER_HUNKS_CO_DECLARED):
                out.setdefault(p, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)
            elif provenance is not None:
                provenance.append((p, peer_hunk_gate.PEER_HUNKS_CO_DECLARED, sibling))
    return out
