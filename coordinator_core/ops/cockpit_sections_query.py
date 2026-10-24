"""
coordinator_core.ops.cockpit_sections_query -- "sizings.summary" and "workflow_runs.list".

Purpose: the production caller of the sizings and workflow_runs section porters. The
artifact.emit aggregate is gone (DR-351), so a section reaches a consumer only through a
registered query op; this is the handoff.columns shape. Each handler builds the EmitContext
via resolvers.resolve_context and returns the rows of the section's own collect() -- no
projection lives here.

Row shapes are the SizingSummary and WorkflowRun contract entities. Malformed records are
omitted from ``records`` and counted in ``malformed`` (path + reason only).

Absent ``repo_root`` -> well-formed empty payload, no raise.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.emit import resolvers
from coordinator_core.ops.emit.sections import sizings, workflow_runs
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)


def _serve(
    op: str,
    collect: Callable,
    repo_root: Optional[Path],
) -> dict:
    if repo_root is None:
        _LOG.warning("%s: repo_root absent -- returning empty payload", op)
        return {"records": [], "malformed": []}
    ctx = resolvers.resolve_context(main_worktree_root(repo_root))
    records, malformed = collect(ctx)
    return {
        "records": records,
        "malformed": [{"path": m["path"], "reason": m["reason"]} for m in malformed],
    }


@register_op("sizings.summary")
def _sizings_summary(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Return ``{"records": [SizingSummary rows], "malformed": [{path, reason}]}``."""
    return _serve("sizings.summary", sizings.collect, repo_root)


@register_op("workflow_runs.list")
def _workflow_runs_list(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Return ``{"records": [WorkflowRun rows], "malformed": [{path, reason}]}``."""
    return _serve("workflow_runs.list", workflow_runs.collect, repo_root)
