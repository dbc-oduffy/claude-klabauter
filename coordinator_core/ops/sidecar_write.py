"""coordinator_core.ops.sidecar_write -- op "sidecar.write": one atomic file write into a workflow sidecar dir.

Lands ``content`` at ``<dir>/<name>`` when ``dir`` sits under ``agent_sidecar.SIDECAR_ROOT``; any other
dir or name returns ``{"error": ...}`` and writes nothing. Lists no directory and spawns no process.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import agent_sidecar

OP_KEY = "sidecar.write"


def _refuse(why: str) -> dict:
    return {"error": f"{OP_KEY}: {why}"}


def _root(param_root, dispatcher_root) -> Path | None:
    for cand in (dispatcher_root, param_root):
        if cand:
            return Path(cand)
    from coordinator_core.ops import prune_claude_temp

    found = prune_claude_temp.find_repo_root(os.getcwd())
    return Path(found) if found else None


@register_op(OP_KEY)
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "sidecar.write": params `dir` (repo-relative, under the workflow-sidecars root), `name` (one segment), `content` (str)."""
    dir_ = params.get("dir")
    name = params.get("name")
    content = params.get("content")
    if not isinstance(dir_, str) or not dir_.startswith(agent_sidecar.SIDECAR_ROOT + "/"):
        return _refuse(f"dir must start with {agent_sidecar.SIDECAR_ROOT}/")
    if not isinstance(name, str):
        return _refuse("name must be a string")
    dir_segments = [s for s in dir_[len(agent_sidecar.SIDECAR_ROOT) + 1 :].split("/") if s]
    if not dir_segments:
        return _refuse(f"dir must name a subdirectory of {agent_sidecar.SIDECAR_ROOT}/")
    segments = [name] + dir_segments
    for seg in segments:
        try:
            checked = agent_sidecar._checked_segment("segment", seg)
        except ValueError as exc:
            return _refuse(str(exc))
        if checked != seg:
            return _refuse(f"segment {seg!r} must be one segment of [A-Za-z0-9._-]")
    if not isinstance(content, str):
        return _refuse("content must be a string")
    root = _root(params.get("repo_root"), repo_root)
    if root is None:
        return _refuse("no repo root")

    try:
        root = root.resolve()
        allowed = (root / agent_sidecar.SIDECAR_ROOT).resolve()
        target = (root / dir_ / name).resolve()
        if allowed not in target.parents:
            return _refuse(f"{dir_} resolves outside {agent_sidecar.SIDECAR_ROOT}")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        return _refuse(str(exc))
    return {"path": target.relative_to(root).as_posix(), "bytes": len(data)}
