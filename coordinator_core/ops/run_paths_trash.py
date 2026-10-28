"""coordinator_core.ops.run_paths_trash -- op "run.paths_trash": delete a chunk's declared paths
anywhere in the repo by trash-then-prune, without a shell remove.

Reuses scratch.delete's manifest lookup, index check, link check and tombstone dir
(``scratch/.deleted/``); only the path-scope rule differs. Each admitted path is moved with one
``os.rename``; the detached prune pass reclaims the bytes. No process is spawned and no tree is
walked.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Sequence

from coordinator_core.ipc import register_op
from coordinator_core.ops import scratch_delete as _sd

OP_KEY = "run.paths_trash"

_PROTECTED_FIRST = (".git", "state", "docs")


def _static_refusal(rel: str, writes: Sequence[str]) -> str:
    """Refusal reason from the path text alone; '' when clean."""
    if not isinstance(rel, str) or not rel:
        return "path is not a non-empty string"
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        return "path is absolute"
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if ".." in parts:
        return "path has a '..' segment"
    if norm not in writes and rel not in writes:
        return "path is not in the chunk's declared writes"
    if not parts:
        return "path is the repo root"
    if parts[0] == ".git":
        return "path is under .git/"
    if len(parts) == 1 and parts[0] in _PROTECTED_FIRST:
        return f"path is the top-level {parts[0]}/ itself"
    if parts[0] == _sd._SCRATCH:
        if len(parts) == 1:
            return "path is scratch/ itself"
        if parts[1] == _sd._RECORD_SEGMENT:
            return "path is under scratch/warp/ (run records)"
        if parts[1] == _sd.TOMBSTONE_DIRNAME:
            return f"path is under scratch/{_sd.TOMBSTONE_DIRNAME}/ (tombstones)"
    return ""


def admit(
    repo_root: Path, run_id: str, chunk_id: str, paths: Sequence[str]
) -> tuple[list[str], list[dict]]:
    """Split `paths` into (admitted repo-relative posix paths, refusals `{path, reason}`)."""
    from coordinator_core.ops import prune_claude_temp as _p

    root = Path(repo_root)
    writes = _sd._manifest_writes(root, run_id, chunk_id)
    if isinstance(writes, str):
        return [], [{"path": p, "reason": writes} for p in paths]

    refused: list[dict] = []
    candidates: list[str] = []
    for p in paths:
        reason = _static_refusal(p, writes)
        if reason:
            refused.append({"path": p, "reason": reason})
        else:
            candidates.append("/".join(x for x in p.replace("\\", "/").split("/") if x not in ("", ".")))
    if not candidates:
        return [], refused

    if not _p._gitignored(str(root), _sd._SCRATCH):
        return [], refused + [
            {"path": c, "reason": "scratch/ is not gitignored (tombstone dir)"} for c in candidates
        ]

    index_refusals = _sd._index_refusal(root, candidates)
    admitted: list[str] = []
    for c in candidates:
        if c in index_refusals:
            refused.append({"path": c, "reason": index_refusals[c]})
            continue
        cur = root
        linked = False
        for seg in c.split("/"):
            cur = cur / seg
            if os.path.lexists(cur) and _p._is_link(str(cur)):
                linked = True
                break
        if linked:
            refused.append({"path": c, "reason": "path has a link or junction component"})
        else:
            admitted.append(c)
    return admitted, refused


def trash_declared(repo_root, run_id: str, chunk_id: str, paths: Sequence[str]) -> dict:
    """Move each admitted path into the tombstone dir. Absent paths are reported, not refused."""
    root = Path(repo_root)
    admitted, refused = admit(root, run_id, chunk_id, paths)
    deleted: list[str] = []
    absent: list[str] = []
    tomb = root / _sd._SCRATCH / _sd.TOMBSTONE_DIRNAME
    for rel in admitted:
        target = root / rel
        if not os.path.lexists(target):
            absent.append(rel)
            continue
        try:
            os.makedirs(tomb, exist_ok=True)
            os.rename(target, tomb / f"{target.name}-{time.time_ns()}")
        except OSError as exc:
            refused.append({"path": rel, "reason": f"rename failed: {exc}"})
            continue
        deleted.append(rel)
    return {"deleted": deleted, "absent": absent, "refused": refused}


@register_op(OP_KEY)
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "run.paths_trash": params `run_id`, `chunk_id`, `paths` (list of str)."""
    run_id = params.get("run_id")
    chunk_id = params.get("chunk_id")
    paths = params.get("paths")
    if not isinstance(run_id, str) or not run_id:
        return {"error": f"{OP_KEY}: run_id must be a non-empty string"}
    if not isinstance(chunk_id, str) or not chunk_id:
        return {"error": f"{OP_KEY}: chunk_id must be a non-empty string"}
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return {"error": f"{OP_KEY}: paths must be a list of strings"}
    root = _sd._resolve_root(params.get("repo_root"), repo_root, run_id)
    if not root:
        return {"error": f"{OP_KEY}: no repo root"}
    return trash_declared(Path(root), run_id, chunk_id, paths)
