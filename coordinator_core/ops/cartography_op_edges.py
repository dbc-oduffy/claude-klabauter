"""
coordinator_core.ops.cartography_op_edges — JSON-RPC "cartography.op_edges"
operation.

Purpose: thin RPC wrapper over
``coordinator_core.cartography.op_edges.build_op_edges`` — the
registry-dispatch producer(``@register_op`` site) -> consumer
(``get_op_handler``/``dispatch_message`` literal site) edge graph that
``cartography.edges`` self-describes as categorically absent from its own
static import/call graph (that op's ``excludes: ["register_op_dynamic_
dispatch"]`` marker). See ``coordinator_core.cartography.op_edges`` module
docstring for the full spec, the three named-but-unmodelled sibling edge
classes, and the declared class's exact boundaries. Scope "none" /
COMPUTE_ONLY, mirrors ``ops/cartography_edges.py``'s registration pattern
(and this repo's freshest sibling, ``ops/cartography_chunk_table.py``, for
current module-docstring/DR-208-affirmation conventions).

Self-registration: importing this module calls
register_op("cartography.op_edges", _cartography_op_edges) as a side-effect.
Wired into coordinator_core.ops.__init__'s ``_EAGER_OP_MODULES``, so this op
is LIVE on the dispatch path independent of import order.

Consumption status: UNCONSUMED — no call site exists today; not named in
Example-doctrine-repo's frozen contract (`docs/contracts/arch-engine-scripts.md`) at
all, and not among the two op names the survey's Workflow script does call
(``cartography.chunk_table`` and ``cartography.churn`` —
docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The survey calls
two of nine cartography op names").

Wire params:
    target_root (str, required) — containment root; every path in `files` is
                                   validated (post-resolve()) to be contained
                                   under this root before being read.
    files       (list[str], required) — absolute or target_root-relative
                                   paths to *.py files to build the
                                   registry-dispatch edge graph over.

Reply fields:
    {"edges": [...], "op_names": [...], "registration_site_count": int,
     "unmodelled": [...], "static_only": true}
    — see coordinator_core.cartography.op_edges.build_op_edges for the
    per-entry shape.

DR-208 five-question affirmation (COMPUTE_ONLY; citing this handler):
  1. Writes, deletes, or reorders any state file, queue, or git object?  No.
     The handler only reads *.py source text (Path.read_text, mode 'r') and
     returns a computed dict; no file is opened for write, no git object is
     touched.
  2. Writes into rag's relational store?                                 No.
     Returns a structured dict to the caller; no rag interaction of any kind.
  3. Opens any file for write (including sentinel creation)?             No.
     Every file touched is opened read-only via Path.read_text(); no
     tempfile, no sentinel, no os.replace.
  4. Mutates shared mutable state outside its own module?                No.
     ast.parse/ast.walk operate on an in-memory string/tree; no shared/
     global state is written by this handler or by
     coordinator_core.cartography.op_edges.
  5. Persistent state changes observable across process boundaries?      No.
     Nothing is written to disk; the only observable effect is the return
     value handed back to the caller.
  Git-shelling-is-read-only precedent: this handler shells out to nothing —
  it is pure-Python ast.parse/ast.walk over already-read source text, the
  same profile as cartography.edges' own affirmed-COMPUTE_ONLY posture.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: cross-repo memo, 2026-08-06 architecture survey
(cartography.edges' own "excludes": ["register_op_dynamic_dispatch"] marker
is what six of thirteen analysts independently paid the cost of).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.op_edges import build_op_edges
from coordinator_core.ipc import register_op


@register_op("cartography.op_edges")
async def _cartography_op_edges(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.op_edges" handler.

    Args (via params):
        target_root (str): containment root for every path in `files`.
        files (list[str]): *.py file paths to build the registry-dispatch
            edge graph over.

    Returns:
        {"edges": [...], "op_names": [...], "registration_site_count": int,
         "unmodelled": [...], "static_only": true}
        (see coordinator_core.cartography.op_edges.build_op_edges)

    Raises:
        ValueError — if `target_root` or `files` is missing (descriptive
        message naming the required param), matching the cartography.edges
        error contract.
        coordinator_core.cartography._guard.PathEscapeError — propagated,
        uncaught, if any entry in `files` resolves outside `target_root`.
        This is a containment violation, not a per-file data condition.
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.op_edges requires param: target_root")
    files = params.get("files")
    if not files:
        raise ValueError("cartography.op_edges requires param: files")
    guarded_root = path_guard(target_root, ".")
    return build_op_edges(guarded_root, files)
