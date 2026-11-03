"""``dispatch.release_run_token``: the sanctioned peer release of one warp run token.

Params: ``{"run_id": str}``.
Reply: ``{ok, run_id, released, token_removed, index_entry_removed, record: {session_id,
sizing_rel, minted_at} | None}``; ``released`` is true when the unclaimed token record or the
live-run index entry existed and is now gone, and ``record`` is null only when no token record
was held. The token's sha256 is never echoed. Removes the unclaimed record only
(``run_token.discard``), so a terminal commit holding the claim is unaffected. No spawn, no git call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import run_token

OP_NAME = "dispatch.release_run_token"
_ECHOED = ("session_id", "sizing_rel", "minted_at")


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.release_run_token": delete one run's unclaimed token record."""
    if repo_root is None:
        return {"ok": False, "error": f"{OP_NAME} requires repo_root"}
    run_id = params.get("run_id") if isinstance(params, dict) else None
    if not isinstance(run_id, str) or not run_id:
        return {"ok": False, "error": "run_id must be a non-empty string; pass {\"run_id\": \"<id>\"}"}
    root = Path(repo_root)
    indexed = _indexed(root, run_id)
    record = run_token.discard(root, run_id)
    return {
        "ok": True,
        "run_id": run_id,
        "released": record is not None or indexed,
        "token_removed": record is not None,
        "index_entry_removed": indexed and not _indexed(root, run_id),
        "record": None if record is None else {k: record.get(k) for k in _ECHOED},
    }


def _indexed(root: Path, run_id: str) -> bool:
    """True when the live-run index holds an entry for `run_id`; False on any read failure."""
    from coordinator_core.ops.dispatch_emit import live_run_index

    try:
        path = live_run_index.index_path(root)
        return path is not None and run_id in live_run_index._load(root, path)
    except Exception:  # noqa: BLE001
        return False
