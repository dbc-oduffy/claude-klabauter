"""coordinator_core.ops.research_targets — JSON-RPC "research.targets" COMPUTE_ONLY operation.

Purpose: serves ``machine_resolver.research_targets()`` (the registered research clones with
purpose/topic) so a caller can query them over the wire. Optional ``topic`` param filters by
casefolded exact match. Zero spawns, no repo read, no write.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core import machine_resolver
from coordinator_core.ipc import register_op


@register_op("research.targets")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Return ``{"targets": [rows], "registry_readable": bool}``.

    ``topic`` (optional str) keeps only rows whose topic casefolds equal to it. An unreadable
    registry yields ``targets == []`` with ``registry_readable`` False, so the caller can tell
    "none registered" from "could not read".
    """
    rows = machine_resolver.research_targets()
    topic = (params or {}).get("topic")
    if isinstance(topic, str) and topic.strip():
        want = topic.strip().casefold()
        rows = [r for r in rows if str(r.get("topic", "")).casefold() == want]
    return {
        "targets": rows,
        "registry_readable": machine_resolver.external_workspace_registry_readable(),
    }
