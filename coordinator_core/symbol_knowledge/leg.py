"""
coordinator_core.symbol_knowledge.leg -- the ``symbol-knowledge`` close leg.

Purpose: composes ``contract.flag_symbols``, ``record_traps`` and ``launch_batches`` into one
``LegResult`` for ``close_background.BACKGROUND_LEGS``. Runs inside the 400 ms job budget: the flag
check, the trap computation and one detached launch only.

Negative-spec:
  - Writes no file: the detached batch job is the sidecar's only writer.
  - ``not-applicable`` when nothing is flagged (no agent launched), when the run has no terminal
    commit, the repo has not opted into coordinator state, or the SCIP index is absent.
  - Otherwise ``queued`` with the launch detail; the batch job amends it to ``ran``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.dispatch_emit.run_close import LegResult, scrub_detail
from coordinator_core.symbol_knowledge import contract

INDEX_GRAPH_DB = "root/graph.db"


def index_graph_db(root: "str | Path") -> Optional[Path]:
    """The repo's example-retrieval-repo ``graph.db`` under the settings home, or None when absent. The SCIP
    symtab sits beside it."""
    from coordinator_core._settings_home import settings_home

    slug = Path(root).resolve().name.replace("-", "_")
    path = settings_home() / "project-rag" / "indexes" / slug / INDEX_GRAPH_DB
    return path if path.is_file() else None


def symbol_knowledge_leg(ctx: Any) -> LegResult:
    """The leg body for a ``close_background.JobContext``."""
    sha = ctx.terminal_sha
    if not sha:
        return LegResult("not-applicable", "run has no terminal commit")
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    if not process_artifact_policy.hosts_coordinator_state(ctx.root):
        return LegResult("not-applicable", "repo not opted into coordinator state")
    graph_db = index_graph_db(ctx.root)
    if graph_db is None:
        return LegResult("not-applicable", "no symbol index for this repo")
    flagged = contract.flag_symbols(ctx.common_dir, sha, graph_db)
    if not flagged:
        return LegResult("not-applicable", "commit changes no public symbol")
    traps = contract.record_traps(ctx.record, flagged)
    detail = contract.launch_batches(ctx.root, ctx.run_id, flagged, traps, sha=sha)
    return LegResult("queued", scrub_detail(detail, ctx.root))
