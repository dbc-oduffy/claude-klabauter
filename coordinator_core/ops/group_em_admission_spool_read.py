"""JSON-RPC "groupem.admission_spool_read": read-only offset read of this box's admission spool."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.group_em import admission_spool
from coordinator_core.ipc import register_op


@register_op("groupem.admission_spool_read")
def _groupem_admission_spool_read(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.admission_spool_read" handler.

    Params:
        offset (int >= 0) -- byte offset to read the admission spool from.

    Returns:
        {"ok", "lines": [str], "next_offset": int} (whole lines only) or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    offset = p.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return {"error": "offset (int >= 0) is required"}
    return admission_spool.read_since(offset)
