"""`dispatch.measured_rev`: a candidate commit of HEAD plus the run's write set, and the
drift check that tells a landing gate whether the recorded measurement still holds."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Sequence

from coordinator_core.git import commit
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_objects import flush_object_batch, object_batch, write_object
from coordinator_core.git.git_state import head_sha, read_tree_spine
from coordinator_core.git.tree_spine import (
    _ABSENT,
    _rewrite_head_spine,
    _synthesize_absent_spine_dirs,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit.ask_contract import (
    MEASURED_REV_OP,
    MEASURED_REV_RECORD,
    measured_rev_record_path,
)

_GLOB_CHARS = frozenset("*?[")
_TREE_MODE = 0o40000
_GITLINK_MODE = 0o160000


class MeasuredRevRefused(ValueError):
    """Typed refusal: the candidate commit cannot be built faithfully. Nothing was written
    to a ref or the index."""


def _normalise(raw: str) -> str:
    rel = str(raw).replace("\\", "/")
    parts = rel.split("/")
    if not rel or rel.startswith("/") or (len(rel) > 1 and rel[1] == ":") or ".." in parts or "" in parts or "." in parts:
        raise MeasuredRevRefused(f"{raw!r} is not a repo-relative file path")
    if ".git" in (p.lower() for p in parts):
        raise MeasuredRevRefused(f"{raw!r} is inside .git")
    return rel


def measured_rev(run_id: str, paths: Sequence[str], repo_root: Path | str | None = None) -> Dict[str, Any]:
    """Params `run_id`, `paths`; returns a dict keyed by `MEASURED_REV_KEYS`
    (`sha`, `base_sha`, `paths`, `removed`, `blobs`). Moves no ref, writes no index,
    spawns nothing. The record is replaced per run_id: pass the run's whole write set in one
    call. Raises `MeasuredRevRefused` for no HEAD, a path outside the repo, a
    directory, glob or submodule path, or an unsupported checkin filter. `repo_root`
    defaults to the current directory."""
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    record = measured_rev_record_path(root, run_id)
    base = head_sha(root, fresh=True)
    if base is None:
        raise MeasuredRevRefused("no HEAD commit to measure against")
    rels = sorted({_normalise(p) for p in paths})
    from coordinator_core.ops.dispatch_emit.landing_integrity import unparseable_py

    broken = unparseable_py(root, rels)
    if broken:
        raise MeasuredRevRefused(
            "measured-rev refused: py-unparseable: " + "; ".join(f"{f.path} {f.detail}" for f in broken)
        )
    spine = read_tree_spine(root, rels)
    if spine is None:
        raise MeasuredRevRefused("HEAD's tree spine could not be read")

    gitdir = resolve_git_dir(root)
    assembled: Dict[str, object] = {}
    written: list[str] = []
    removed: list[str] = []
    blobs: Dict[str, str] = {}
    with object_batch():
        for rel in rels:
            target = root / rel
            parent, _, leaf = rel.rpartition("/")
            head_entry = spine.get(parent, {}).get(leaf)
            if head_entry is not None and head_entry[0] in (_TREE_MODE, _GITLINK_MODE):
                raise MeasuredRevRefused(f"{rel} is a directory or submodule in HEAD")
            if target.is_symlink():
                raise MeasuredRevRefused(f"{rel} is a symlink")
            if target.is_dir():
                raise MeasuredRevRefused(f"{rel} is a directory")
            if not target.exists():
                if _GLOB_CHARS & set(rel):
                    raise MeasuredRevRefused(f"{rel} looks like a glob, not one file path")
                removed.append(rel)
                if head_entry is not None:
                    assembled[rel] = _ABSENT
                continue
            try:
                data = commit._checkin_bytes(root, rel, target.read_bytes())
            except commit.FilterUnsupported as exc:
                raise MeasuredRevRefused(str(exc)) from exc
            except OSError as exc:
                raise MeasuredRevRefused(f"{rel} is unreadable: {exc}") from exc
            sha = write_object(gitdir, b"blob", data)
            mode = head_entry[0] if head_entry is not None else commit._mode_for(target)
            assembled[rel] = (mode, sha)
            written.append(rel)
            blobs[rel] = sha

        filled = _synthesize_absent_spine_dirs(spine, assembled)
        if filled is None:
            raise MeasuredRevRefused("a written path's parent collides with a non-directory in HEAD")
        root_tree = _rewrite_head_spine(gitdir, filled, assembled)
        if root_tree is None:
            raise MeasuredRevRefused("the tree spine could not be rewritten for these paths")

        name, email = commit._identity(root)
        who = f"{name} <{email}> {commit._stamp()}"
        body = f"tree {root_tree}\nparent {base}\nauthor {who}\ncommitter {who}\n\nmeasured-rev {run_id}\n"
        cand = write_object(gitdir, b"commit", body.encode("utf-8", "surrogateescape"))
        flush_object_batch()

    result = {"sha": cand, "base_sha": base, "paths": written, "removed": removed, "blobs": blobs}
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return result


@register_op(MEASURED_REV_OP)
def _handler(params: dict, repo_root: Path | None = None) -> Dict[str, Any]:
    """JSON-RPC "dispatch.measured_rev": params `run_id` (str) and `paths` (list[str]);
    replies the `MEASURED_REV_KEYS` dict, or `{error, refused: "measured-rev"}` on a refusal."""
    run_id, paths = params.get("run_id"), params.get("paths")
    if not isinstance(run_id, str) or not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return {"error": "params.run_id (str) and params.paths (list of str) are required", "refused": "measured-rev"}
    try:
        return measured_rev(run_id, paths, repo_root)
    except (OSError, ValueError) as exc:
        return {"error": str(exc), "refused": "measured-rev"}


def measured_rev_drift(run_dir: Path, repo_root: Path) -> list[str]:
    """Recorded paths whose worktree blob no longer matches the record (None included), plus
    each `removed` path that exists again. `[]` when `run_dir` holds no record. Hash-only:
    spawns nothing and reads no object. Raises OSError/ValueError on an unreadable or malformed
    record; callers fail closed."""
    record = Path(run_dir) / MEASURED_REV_RECORD
    if not record.is_file():
        return []
    data = json.loads(record.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{record} does not hold a measured-rev record object")
    root = Path(repo_root)
    blobs = data.get("blobs") or {}
    drifted = [p for p, sha in blobs.items() if commit.checkin_blob_sha(root, p) != sha]
    drifted.extend(p for p in data.get("removed") or [] if (root / p).exists())
    return drifted
