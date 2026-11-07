"""Per-path post-write blob prints of a run: the bytes a run's chunk left in the tree when it returned.

Stored as `write_prints.json` (`{forward-slash path: blob sha | None}`) in the run record dir. A
later print of a path replaces the earlier one. Zero spawn; never raises on a bad record.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.ops.dispatch_emit import kept_work

PRINTS_FILE = "write_prints.json"


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _prints_path(root: Path, run_id: str) -> Path:
    """`write_prints.json` beside the run's manifest (record dir). Raises ValueError on an unsafe run_id."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    return record_dir(Path(root), run_id) / PRINTS_FILE


def read(root: Path, run_id: str) -> Optional[dict[str, Optional[str]]]:
    """`{path: blob sha | None}` recorded for `run_id`, or None when absent or unreadable (unknown)."""
    try:
        data = json.loads(_prints_path(root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {_norm(p): sha for p, sha in data.items() if isinstance(p, str) and (sha is None or isinstance(sha, str))}


def _print_and_store(root: Path, rel: str) -> Optional[str]:
    """Blob sha of `root/rel`, with the bytes written to the object store so a reader can resolve the
    print; a store that cannot take the write leaves the print unreadable (readers hold)."""
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    try:
        from coordinator_core.git import git_objects
        from coordinator_core.git.git_dir import resolve_git_common_dir

        return git_objects.write_object(resolve_git_common_dir(root), b"blob", data)
    except Exception:  # noqa: BLE001 -- sha still recorded; unreadable object holds the hunks
        return kept_work.blob_sha(data)


def _repo_relative(root: Path, path: str) -> Optional[str]:
    """`path` as a forward-slash path under `root`; None when blank or outside the tree."""
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        return _norm(path)
    try:
        return candidate.resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _peer_edit_since_last_print(root: Path, run_id: str, rel: str, tool_input: object) -> bool:
    """True when an `Edit` of `rel` provably started from bytes other than the run's last print of it,
    i.e. a peer edited the file between two run writes. Only a unique, single-replace edit is
    checkable; anything else is not a proof."""
    if not isinstance(tool_input, dict):
        return False
    old, new = tool_input.get("old_string"), tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or not new or tool_input.get("replace_all"):
        return False
    prior = (read(root, run_id) or {}).get(rel)
    if prior is None:
        return False
    try:
        text = (root / rel).read_bytes().decode("utf-8")
    except (OSError, ValueError):
        return False
    if text.count(new) != 1:
        return False
    return kept_work.blob_sha(text.replace(new, old, 1).encode("utf-8")) != prior


def record_write(
    root: Path, session_id: str, agent_id: str, path: str, tool_input: object = None
) -> Optional[str]:
    """Print `path` for the one live run of `session_id` at the moment its executor wrote it.

    Only a subagent write counts (`agent_id` set): the EM's own hand edits are not the run's bytes.
    Records nothing when zero or several live runs hold the session, or the path is outside `root`.
    An `Edit` (`tool_input`) that started from bytes other than the run's last print drops the print,
    so the checkpoint withholds the path rather than absorbing the peer's edit.
    Returns the run id recorded for, else None. Zero spawn; never raises.
    """
    if not (session_id and agent_id):
        return None
    try:
        from coordinator_core.ops.dispatch_emit import run_token

        rel = _repo_relative(Path(root), path)
        if rel is None:
            return None
        runs = {r["run_id"] for r in run_token.live_records(Path(root)) if r.get("session_id") == session_id}
        if len(runs) != 1:
            return None
        (run_id,) = runs
        if _peer_edit_since_last_print(Path(root), run_id, rel, tool_input):
            drop(Path(root), run_id, rel)
            return run_id
        record(Path(root), run_id, [rel])
        return run_id
    except Exception:  # noqa: BLE001 -- no print: the checkpoint withholds the file
        return None


def drop(root: Path, run_id: str, rel: str) -> None:
    """Remove the run's print of `rel`, so a checkpoint withholds and names it."""
    merged = read(root, run_id) or {}
    if merged.pop(_norm(rel), 0) == 0:
        return
    target = _prints_path(root, run_id)
    tmp = target.with_name(f"{PRINTS_FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(merged, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def record(root: Path, run_id: str, paths: Iterable[str]) -> None:
    """Merge the current blob print of each of `paths` (None when absent) into the run's prints,
    latest write wins; atomic replace. An unreadable existing record is replaced, not extended."""
    target = _prints_path(root, run_id)
    merged = read(root, run_id) or {}
    for rel in paths:
        key = _norm(rel)
        merged[key] = _print_and_store(Path(root), key)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{PRINTS_FILE}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(merged, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
