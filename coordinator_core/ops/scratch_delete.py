"""coordinator_core.ops.scratch_delete -- op "scratch.delete": perform a chunk's declared
delete under the repo's gitignored ``scratch/`` without a shell remove.

A path is admitted only when the run's staged manifest row lists it in ``writes``. Each admitted
path is moved with one ``os.rename`` into ``scratch/.deleted/``; the detached prune pass reclaims
the bytes. No process is spawned and no tree is walked.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Sequence

from coordinator_core.ipc import register_op

TOMBSTONE_DIRNAME = ".deleted"
OP_KEY = "scratch.delete"

_SCRATCH = "scratch"
_RECORD_SEGMENT = "warp"
_INDEX_VERSIONS_SAFE = (2, 3)


def _manifest_writes(repo_root: Path, run_id: str, chunk_id: str) -> tuple[str, ...] | str:
    """The chunk row's `writes`, or a refusal reason string."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    try:
        manifest_path = ask_contract.record_dir(repo_root, run_id) / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = ask_contract.StageManifest.from_json(data)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return f"run manifest unreadable: {exc}"
    for row in manifest.rows:
        if row.id == chunk_id:
            return tuple(row.writes)
    return f"chunk {chunk_id!r} is not a row of run {run_id!r}"


def _static_refusal(rel: str, writes: Sequence[str]) -> str:
    """Refusal reason from the path text alone, before any filesystem call; '' when clean."""
    if not isinstance(rel, str) or not rel:
        return "path is not a non-empty string"
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        return "path is absolute"
    parts = norm.split("/")
    if ".." in parts:
        return "path has a '..' segment"
    if norm not in writes and rel not in writes:
        return "path is not in the chunk's declared writes"
    parts = [p for p in parts if p not in ("", ".")]
    if not parts or parts[0] != _SCRATCH:
        return "path is outside scratch/"
    if len(parts) == 1:
        return "path is scratch/ itself"
    if parts[1] == _RECORD_SEGMENT:
        return "path is under scratch/warp/ (run records)"
    if parts[1] == TOMBSTONE_DIRNAME:
        return f"path is under scratch/{TOMBSTONE_DIRNAME}/ (tombstones)"
    return ""


def _index_refusal(repo_root: Path, rels: Sequence[str]) -> dict[str, str]:
    """Per-path refusal from the git index; the substring test is sound only for index v2/v3."""
    from coordinator_core.ops import prune_claude_temp as _p

    index = _p._read_index(str(repo_root))
    if index is None:
        return {r: "git index is unreadable" for r in rels}
    version = int.from_bytes(index[4:8], "big") if index[:4] == b"DIRC" else -1
    if version not in _INDEX_VERSIONS_SAFE:
        return {r: f"git index version {version} is not 2 or 3" for r in rels}
    return {
        r: "path holds a file in the git index"
        for r in rels
        if r.encode("utf-8", "surrogateescape") in index
    }


def admit(
    repo_root: Path, run_id: str, chunk_id: str, paths: Sequence[str]
) -> tuple[list[str], list[dict]]:
    """Split `paths` into (admitted repo-relative posix paths, refusals `{path, reason}`)."""
    from coordinator_core.ops import prune_claude_temp as _p

    root = Path(repo_root)
    refused: list[dict] = []
    writes = _manifest_writes(root, run_id, chunk_id)
    if isinstance(writes, str):
        return [], [{"path": p, "reason": writes} for p in paths]

    candidates: list[str] = []
    for p in paths:
        reason = _static_refusal(p, writes)
        if reason:
            refused.append({"path": p, "reason": reason})
        else:
            candidates.append("/".join(x for x in p.replace("\\", "/").split("/") if x not in ("", ".")))
    if not candidates:
        return [], refused

    if not _p._gitignored(str(root), _SCRATCH):
        return [], refused + [{"path": c, "reason": "scratch/ is not gitignored"} for c in candidates]

    index_refusals = _index_refusal(root, candidates)
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


def delete_declared(repo_root, run_id: str, chunk_id: str, paths: Sequence[str]) -> dict:
    """Move each admitted path into the tombstone dir. Absent paths are reported, not refused."""
    root = Path(repo_root)
    admitted, refused = admit(root, run_id, chunk_id, paths)
    deleted: list[str] = []
    absent: list[str] = []
    tomb = root / _SCRATCH / TOMBSTONE_DIRNAME
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


def brief_clause(run_id: str, chunk_id: str, writes: Sequence[str]) -> str:
    """The brief line naming the op call for a row's scratch/ writes; '' when it has none."""
    if not isinstance(writes, (list, tuple)):
        return ""
    scratch = [
        w for w in writes
        if isinstance(w, str) and w.replace("\\", "/").split("/", 1)[0] == _SCRATCH
    ]
    if not scratch:
        return ""
    payload = json.dumps({"run_id": run_id, "chunk_id": chunk_id, "paths": scratch})
    return (
        "To delete a declared scratch/ path, a shell remove is denied; run "
        f"`coordinator-invoke {OP_KEY} '{payload}'`."
    )


@register_op(OP_KEY)
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "scratch.delete": params `run_id`, `chunk_id`, `paths` (list of str)."""
    run_id = params.get("run_id")
    chunk_id = params.get("chunk_id")
    paths = params.get("paths")
    if not isinstance(run_id, str) or not run_id:
        return {"error": "scratch.delete: run_id must be a non-empty string"}
    if not isinstance(chunk_id, str) or not chunk_id:
        return {"error": "scratch.delete: chunk_id must be a non-empty string"}
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return {"error": "scratch.delete: paths must be a list of strings"}
    root = _resolve_root(params.get("repo_root"), repo_root, run_id)
    if not root:
        return {"error": "scratch.delete: no repo root"}
    return delete_declared(Path(root), run_id, chunk_id, paths)


def _resolve_root(param_root, dispatcher_root, run_id: str):
    """The first candidate root holding the run's record: the explicit param, the dispatcher's
    root, then the caller's cwd repo. None of them holding it falls back to the first candidate
    so the refusal names that root."""
    from coordinator_core.ops import prune_claude_temp as _p
    from coordinator_core.ops.dispatch_emit import ask_contract

    candidates = [c for c in (param_root, dispatcher_root) if c]
    cwd_root = _p.find_repo_root(os.getcwd())
    if cwd_root:
        candidates.append(cwd_root)
    for cand in candidates:
        try:
            if (ask_contract.record_dir(Path(cand), run_id) / "manifest.json").is_file():
                return cand
        except (ValueError, OSError):
            break
    return candidates[0] if candidates else None
