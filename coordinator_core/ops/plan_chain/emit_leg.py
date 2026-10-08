"""plan_chain emit leg: ``dispatch.emit`` in-process for the ready plan, never fired here.

The emission receipt is stamped with the execute child's pre-minted session id so the child's
firing gate reads the emission as its own. The driver fires; this leg only emits.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Mapping

from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import CrossRepoWriteError
from coordinator_core.ops.dispatch_emit.dirty_write_set import DirtyWriteSetError
from coordinator_core.ops.plan_chain.contract import Halt, halt

Dispatch = Callable[[dict], Mapping[str, Any]]

_DIRTY_MARKERS = ("DirtyWriteSetError", "write set are already modified", "dirtiness check could not run")
_CROSS_REPO_MARKERS = ("CrossRepoWriteError", "resolve outside repoRoot")


def _engine_dispatch(msg: dict) -> Mapping[str, Any]:
    from coordinator_core.ipc import dispatch_message

    return asyncio.run(dispatch_message(msg, caller="plan-chain-run"))


def _classify(text: str) -> Halt:
    if any(m in text for m in _DIRTY_MARKERS):
        return halt("dirty-write-set", text)
    if any(m in text for m in _CROSS_REPO_MARKERS):
        return halt("cross-repo-write-approval", text)
    return Halt("execute", text)


def run(
    plan_path: str | Path,
    *,
    repo_root: str | Path,
    child_session_id: str,
    trail_dir: str | Path,
    chain_id: str,
    dispatch: Dispatch | None = None,
) -> tuple[Path, str] | Halt:
    """Emit ``<trail_dir>/chain-<chain_id>.execute.mjs``; return ``(script_path, sha256)`` or a Halt.

    ``trail_dir`` is the manifest's ``trail_dir`` (repo-relative or absolute). Never retries.
    """
    root = Path(repo_root)
    trail = Path(trail_dir)
    if not trail.is_absolute():
        trail = root / trail
    out = trail / f"chain-{chain_id}.execute.mjs"
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "dispatch.emit",
        "params": {
            "plan_path": str(plan_path),
            "output_path": str(out),
            "repo_root": str(root),
            "session_id": child_session_id,
        },
        "_origin_worktree": str(root),
    }
    try:
        reply = (dispatch or _engine_dispatch)(msg)
    except DirtyWriteSetError as exc:
        return halt("dirty-write-set", str(exc))
    except CrossRepoWriteError as exc:
        return halt("cross-repo-write-approval", str(exc))
    except Exception as exc:  # noqa: BLE001 -- any raise is an execute-stage halt
        return _classify(f"{type(exc).__name__}: {exc}")
    if "error" in reply:
        err = reply["error"]
        return _classify(str((err or {}).get("message") or err) if isinstance(err, Mapping) else str(err))
    result = reply.get("result") or {}
    sha = result.get("sha256")
    path = result.get("path")
    if not isinstance(sha, str) or not sha or not path:
        return Halt("execute", f"dispatch.emit reply carried no path/sha256: keys {sorted(result)!r}")
    return Path(path), sha
