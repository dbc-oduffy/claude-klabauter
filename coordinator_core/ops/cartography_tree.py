"""
coordinator_core.ops.cartography_tree — JSON-RPC "cartography.tree" operation.

Purpose: Thin RPC wrapper over coordinator_core.cartography.tree.build_tree.
Accepts a caller-supplied `target_root`, guards it via
coordinator_core.cartography._guard.path_guard, and returns the deterministic
tracked-file tree ({path: {lang, loc}}) — the RAG-absent-fallback substrate
primitive that replaces an agentic full-inventory read pass (§
docs/problems/2026-07-12-architecture-survey-dogfood-observations.md § 6.1).

Self-registration: importing this module calls register_op("cartography.tree", ...)
as a side-effect — same pattern as ops/ping.py. Wired into
coordinator_core.ops.__init__, which imports this module — this op is LIVE
(reachable via dispatch). The rest of the 4-file seam
(authz/classification.py, ipc.py _OP_KEY_SCOPE, benchmarks
budget-manifest.json) carries a "cartography.tree" entry too.

Wire params:
    target_root (str, required) — root of the tracked-file tree to walk. Must
                                   resolve inside a git worktree.
    scope (list[str], optional) — git pathspecs (repo-relative paths or
                                   directory prefixes) narrowing the walk.
                                   Applied at `git ls-files` enumeration time
                                   (coordinator_core.cartography.tree), not as
                                   a post-hoc filter over the full result.
                                   Absent, behaviour is byte-identical to
                                   before this param existed. Same param
                                   shape as "cartography.file_index"'s
                                   `scope` — a caller narrows both ops
                                   identically.

Consumption status: registered and reachable via dispatch (see
"Self-registration" below), but UNCONSUMED — no call site exists today.
Example-doctrine-repo's frozen contract (`docs/contracts/arch-engine-scripts.md`) names
this op under its `arch-census` lane, but the survey's Workflow script does
not call it; only `cartography.chunk_table` and `cartography.churn` have
call sites (docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The
survey calls two of nine cartography op names").

Reply fields (result object in JSON-RPC response):
    target_root  (str)  — resolved target_root, echoed.
    files        (dict) — {"<repo-relative path>": {"lang": str, "loc": int|None}}.
    file_count   (int)  — len(files).

COMPUTE_ONLY / scope "none" classification (DR-208 § 5 mutating-op-author
checklist — carried into authz/classification.py, which now holds the
"cartography.tree" entry; this module does not itself edit that seam file):
    1. Does the handler open any file for write (including append)?             No.
       `_cartography_tree` only reads: `build_tree` -> `list_tracked_files`
       shells to `git ls-files` (read query) and `_loc_for` calls
       `Path.read_bytes()` (read-only) per tracked file. No `open(..., "w")` /
       `write_text` / `write_bytes` call anywhere in this module or
       `cartography/tree.py`.
    2. Does the handler call any git write command (`git commit`, `git add`,
       `git update-ref`, etc.)?                                                 No.
       The only git invocation is `git ls-files` (cartography/tree.py
       `list_tracked_files`) — a read-only query, the same precedent DR-208's
       own table cites for `coverage.gate` ("all subprocess calls are read-only
       git queries").
    3. Does the handler enqueue any state mutation (write to a queue, backlog,
       or similar)?                                                             No.
       No queue/backlog/state-file append anywhere in the call path.
    4. Does the handler invoke any subprocess that may do any of the above?      No.
       The sole subprocess call is `git ls-files` (read-only; see #2).
    5. Is the handler's I/O behavior conditional (reads under some paths, writes
       under others)?                                                           No.
       Every code path through `build_tree`/`list_tracked_files`/`_loc_for` is a
       read; there is no branch that opens a file for write.
    -> All five verified "no": COMPUTE_ONLY. Scope "none" — this op takes an
       explicit `target_root` wire param (any repo), not a resolved-from-session
       common_dir; it accesses no repo-specific *implicit* state (mirrors
       "ping" / "percolate.run" scope="none" precedent, ipc.py OP_KEY_SCOPE table).

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C2
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.tree import build_tree
from coordinator_core.ipc import register_op


@register_op("cartography.tree")
async def _cartography_tree(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.tree" handler.

    Required params: target_root. Optional: scope (see module docstring
    "Wire params").

    Returns: {target_root, files, file_count} (see module docstring "Reply fields").

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root cannot be
    resolved, and RuntimeError if the underlying `git ls-files` invocation fails
    (target_root not inside a git worktree, or git itself errors).
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.tree requires param: target_root")

    scope = params.get("scope") or None
    guarded_root = path_guard(target_root, ".")
    return build_tree(guarded_root, scope=scope)
