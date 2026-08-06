"""
coordinator_core.ops.ceremony — Ceremony-as-pipeline op package.

Purpose: Home of the ceremony pipeline ops (wsc_resolve, wsc_commit) and shared
ceremony schema/context data models.  Each op sub-module self-registers via
@register_op at import time; this package __init__ is the import trigger that
activates all ceremony ops when coordinator_core.ops is imported.

Wiring: coordinator_core/ops/__init__.py imports this package via a noqa:F401 line
(see dispatch ledger Wave 4, chunk C2.2 registration note).  This __init__ is the
package skeleton — op sub-modules are added in subsequent waves.

Spec backlink:
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § Wave 0
"""

# C2.1 — expose the resolved-state data model at package level for consumers.
from coordinator_core.ops.ceremony.pipeline_context import (  # noqa: F401
    BranchResolution,
    PipelineContext,
)
