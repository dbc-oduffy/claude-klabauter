"""
coordinator_core.ops.cartography_file_index — JSON-RPC "cartography.file_index" operation.

Purpose: Thin RPC wrapper over
coordinator_core.cartography.file_index.build_file_index. Accepts a caller-supplied
`target_root`, guards it via coordinator_core.cartography._guard.path_guard, and
returns the deterministic path->system index — the proven primitive from the
2026-07-12-11h57 architecture-survey dogfood run (149 files, 0 unmapped; §
docs/problems/2026-07-12-architecture-survey-dogfood-observations.md § 5).

Self-registration: importing this module calls
register_op("cartography.file_index", ...) as a side-effect — same pattern as
ops/ping.py. Wired into coordinator_core.ops.__init__, which imports this module
— this op is LIVE (reachable via dispatch). The rest of the 4-file seam
(authz/classification.py, ipc.py _OP_KEY_SCOPE, benchmarks budget-manifest.json)
carries a "cartography.file_index" entry too.

Wire params:
    target_root (str, required) — root of the tracked-file tree to index. Must
                                   resolve inside a git worktree.
    scope (list[str], optional) — git pathspecs (repo-relative paths or
                                   directory prefixes) narrowing the walk.
                                   Applied at `git ls-files` enumeration time
                                   (via coordinator_core.cartography.tree),
                                   not as a post-hoc filter over the full
                                   result. Absent, behaviour is
                                   byte-identical to before this param
                                   existed. Same param shape as
                                   "cartography.tree"'s `scope` — a caller
                                   narrows both ops identically.

Consumption status: registered and reachable via dispatch (see
"Self-registration" below), but UNCONSUMED — no call site exists today.
Example-doctrine-repo's frozen contract (`docs/contracts/arch-engine-scripts.md`) names
this op under its `arch-callgraph` lane, but the survey's Workflow script
does not call it; only `cartography.chunk_table` and `cartography.churn`
have call sites (docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md §
"The survey calls two of nine cartography op names"). The 149-file, 0-unmapped
dogfood run cited above validated correctness, not live consumption.

Reply fields (result object in JSON-RPC response):
    target_root     (str)  — resolved target_root, echoed.
    index           (dict) — {"<repo-relative path>": "<system>"}.
    systems         (dict) — {"<system>": <file_count>} per-system rollup.
    file_count      (int)  — total tracked files seen.
    unmapped_count  (int)  — always 0 for this rule (every tracked path maps by
                             its top-level directory component, or the reserved
                             "repo-root" bucket for a root-level file).

COMPUTE_ONLY / scope "none" classification (DR-208 § 5 mutating-op-author
checklist — carried into authz/classification.py, which now holds the
"cartography.file_index" entry; this module does not itself edit that seam file):
    1. Does the handler open any file for write (including append)?             No.
       `_cartography_file_index` only reads: `build_file_index` calls
       `list_tracked_files` (git ls-files, read query). No write/append call
       anywhere in this module or `cartography/file_index.py`.
    2. Does the handler call any git write command (`git commit`, `git add`,
       `git update-ref`, etc.)?                                                 No.
       The only git invocation, transitively via `cartography.tree.list_tracked_files`,
       is `git ls-files` — a read-only query (same DR-208 `coverage.gate`
       precedent cited by cartography_tree.py).
    3. Does the handler enqueue any state mutation (write to a queue, backlog,
       or similar)?                                                             No.
       No queue/backlog/state-file append anywhere in the call path.
    4. Does the handler invoke any subprocess that may do any of the above?      No.
       The sole subprocess call (via `list_tracked_files`) is `git ls-files`
       (read-only; see #2).
    5. Is the handler's I/O behavior conditional (reads under some paths, writes
       under others)?                                                           No.
       `build_file_index`/`system_for_path` are pure string/dict operations over
       the `list_tracked_files` result; no branch opens a file for write.
    -> All five verified "no": COMPUTE_ONLY. Scope "none" — explicit `target_root`
       wire param (any repo), no implicit repo-specific state (mirrors "ping" /
       "percolate.run" / "cartography.tree" scope="none" precedent).

Spec backlink: docs/plans/2026-07-12-claude-klabauter-cartography-substrate-strand-a.md § C2
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.file_index import build_file_index
from coordinator_core.ipc import register_op


@register_op("cartography.file_index")
async def _cartography_file_index(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.file_index" handler.

    Required params: target_root. Optional: scope (see module docstring
    "Wire params").

    Returns: {target_root, index, systems, file_count, unmapped_count} (see
    module docstring "Reply fields").

    Raises ValueError if target_root is missing; propagates
    coordinator_core.cartography._guard.PathEscapeError if target_root cannot be
    resolved, and RuntimeError if the underlying `git ls-files` invocation fails
    (target_root not inside a git worktree, or git itself errors).
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.file_index requires param: target_root")

    scope = params.get("scope") or None
    guarded_root = path_guard(target_root, ".")
    return build_file_index(guarded_root, scope=scope)
