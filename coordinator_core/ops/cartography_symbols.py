"""
coordinator_core.ops.cartography_symbols — JSON-RPC "cartography.symbols" operation.

Purpose: Thin RPC wrapper over coordinator_core.cartography.symbols.build_symbols
— per-Python-file AST symbol-table extraction (classes/functions/signatures/
module-constants-with-values/docstrings). Scope "none" / COMPUTE_ONLY, mirrors
ops/ping.py's registration pattern.

Self-registration: importing this module calls register_op("cartography.symbols",
_cartography_symbols) as a side-effect. Wired into
coordinator_core.ops.__init__, which imports this module — this op is LIVE on
the dispatch path.

Consumption status: UNCONSUMED — no call site exists today. Example-doctrine-repo's
frozen contract (`docs/contracts/arch-engine-scripts.md`) names this op
under its `arch-census` lane, but the survey's Workflow script does not call
it; only `cartography.chunk_table` and `cartography.churn` have call sites
(docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The survey calls
two of nine cartography op names"). Would be consumed by the
`arch-census`-lane annotation step this contract describes, if and when that
lane's Workflow script is wired.

Wire params:
    target_root (str, required) — containment root; every path in `files` is
                                   validated (post-resolve()) to be contained
                                   under this root before being read.
    files       (list[str], required) — absolute or target_root-relative
                                   paths to *.py files to extract symbols for.

Reply fields:
    {"files": [<per-file symbol table>, ...]}  — see
    coordinator_core.cartography.symbols.symbol_table_for_file for the
    per-entry shape (including the "error" field on a per-file parse failure).

DR-208 five-question affirmation (COMPUTE_ONLY; citing this handler):
  1. Writes, deletes, or reorders any state file, queue, or git object?  No.
     The handler only reads *.py source text (open mode 'r') and returns a
     computed dict; no file is opened for write, no git object is touched.
  2. Writes into rag's relational store?                                 No.
     Returns a structured dict to the caller; no rag interaction of any kind.
  3. Opens any file for write (including sentinel creation)?             No.
     Every file touched is opened read-only via Path.read_text(); no tempfile,
     no sentinel, no os.replace.
  4. Mutates shared mutable state outside its own module?                No.
     ast.parse operates on an in-memory string; no shared/global state is
     written by this handler or by coordinator_core.cartography.symbols.
  5. Persistent state changes observable across process boundaries?     No.
     Nothing is written to disk; the only observable effect is the return
     value handed back to the caller.
  Git-shelling-is-read-only precedent: this handler shells out to nothing —
  it is pure-Python ast.parse over already-read source text, a strictly
  narrower profile than coverage.gate's affirmed-COMPUTE_ONLY git subprocess
  reads (DR-208's own table: "all subprocess calls are read-only git
  queries"). No subprocess call is made here at all.
Authority: docs/decisions/DR-208-invoke-op-authz-model.md § 5

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md
§ chunk C4 (cartography.symbols).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.symbols import build_symbols


@register_op("cartography.symbols")
def _cartography_symbols(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.symbols" handler.

    Args (via params):
        target_root (str): containment root for every path in `files`.
        files (list[str]): *.py file paths to extract symbol tables for.

    Returns:
        {"files": [<symbol table dict per file>, ...]}
        (see coordinator_core.cartography.symbols.symbol_table_for_file)

    Raises:
        ValueError — if `target_root` or `files` is missing (descriptive
        message naming the required param), matching the cartography.tree /
        cartography.file_index error contract.
        coordinator_core.cartography._guard.PathEscapeError — propagated,
        uncaught, if any entry in `files` resolves outside `target_root`.
        This is a containment violation, not a per-file data condition.
    """
    # Review: code-reviewer (P2, 2026-07-12-workflow-review-cartography.md) —
    # bare params[...] raised an uncaught, un-annotated KeyError on a missing
    # param, inconsistent with the descriptive-ValueError contract tree/
    # file_index already use in this same op family.
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.symbols requires param: target_root")
    files = params.get("files")
    if not files:
        raise ValueError("cartography.symbols requires param: files")
    # Review: code-reviewer (P2, Finding 2, 2026-07-12-codereview-slicecartography-
    # substrate-b-wave) — guard target_root at the handler boundary, mirroring
    # cartography.tree/file_index, so a malformed root is rejected up front
    # (descriptive PathEscapeError) rather than surfacing incidentally, deep
    # inside the first file's per-file path_guard call.
    guarded_root = path_guard(target_root, ".")
    return build_symbols(guarded_root, files)
