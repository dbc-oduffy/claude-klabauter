"""coordinator_core.ops.remote_engine_handle: JSON-RPC "remote_engine.handle" read op.

Reads the outcome of a remote-engine failover handle (the `remote_handle` key a door reply
carries). Pure read of one file under the remote-engine requests directory; sweeping and
reaping stay in `background.launch`.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ipc import register_op


@register_op("remote_engine.handle")
def _remote_engine_handle(params: dict, repo_root: Path | None = None) -> dict:
    """Return `{"found": bool, "handle": dict | None}` for `params["key"]`.

    `handle["status"]` is pending, done, indeterminate, no_reply or error; a missing,
    swept or malformed key answers `found: false`.
    """
    key = params.get("key")
    if not key or not isinstance(key, str):
        raise ValueError("remote_engine.handle requires 'key' (non-empty string)")
    from coordinator_core.remote_engine import background

    handle = background.read_handle(key)
    return {"found": handle is not None, "handle": handle}
