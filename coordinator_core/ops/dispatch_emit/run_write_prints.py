"""Per-path post-write blob prints of a run: the bytes a run's chunk left in the tree when it returned.

Stored as `write_prints.json` (`{forward-slash path: blob sha | None}`) in the run record dir. A
later print of a path replaces the earlier one. Zero spawn; never raises on a bad record.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.ops.dispatch_emit import kept_work

PRINTS_FILE = "write_prints.json"
AGENT_RUNS_FILE = "agent_runs.json"


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


def _bound_agents(root: Path, run_id: str) -> frozenset:
    """Agent ids bound to `run_id` in its record dir's `agent_runs.json`; empty when absent or unreadable."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        data = json.loads((record_dir(root, run_id) / AGENT_RUNS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(a for a in data if isinstance(a, str)) if isinstance(data, dict) else frozenset()


def _bind(root: Path, run_id: str, agent_id: str) -> None:
    """Add `agent_id` to the run's `agent_runs.json`; atomic replace."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    target = record_dir(root, run_id) / AGENT_RUNS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(f"{AGENT_RUNS_FILE}.lock")
    held = _take_lock(lock)
    try:
        merged = {a: True for a in _bound_agents(root, run_id)}
        merged[agent_id] = True
        tmp = target.with_name(f"{AGENT_RUNS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(merged, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if held:
            try:
                lock.unlink()
            except OSError:
                pass


def _take_lock(lock: Path) -> bool:
    """Create `lock` exclusively, spinning up to ~1s; a lock older than 2s is a dead holder's and is broken.
    False when not taken (the caller proceeds unlocked rather than drop the binding)."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 2.0:
                    lock.unlink()
                    continue
            except OSError:
                continue
        except OSError:
            return False
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)


def _footprint_holds(root: Path, run_id: str, rel: str) -> bool:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.live_run_writes import manifest_footprint

    try:
        manifest = json.loads((record_dir(root, run_id) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and rel in {_norm(p) for p in manifest_footprint(manifest)}


def resolve_run(root: Path, session_id: str, agent_id: str, rel: str) -> Optional[str]:
    """The live run of `session_id` that owns a subagent write of `rel`, else None (ambiguous or none).

    Order: the run whose `agent_runs.json` binds `agent_id`; else the unique run whose manifest footprint
    holds `rel`; else the session's single live run. The last two bind `agent_id` to the run found.
    Zero spawn; never raises.
    """
    try:
        from coordinator_core.ops.dispatch_emit import run_token

        runs = sorted(
            {r["run_id"] for r in run_token.live_records(Path(root)) if r.get("session_id") == session_id}
        )
        if not runs:
            return None
        bound = [r for r in runs if agent_id in _bound_agents(Path(root), r)]
        if len(bound) == 1:
            return bound[0]
        if bound:
            return None
        owners = [r for r in runs if _footprint_holds(Path(root), r, _norm(rel))]
        found = owners[0] if len(owners) == 1 else (runs[0] if not owners and len(runs) == 1 else None)
        if found is not None:
            _bind(Path(root), found, agent_id)
        return found
    except Exception:  # noqa: BLE001 -- unresolved: the checkpoint withholds
        return None


def record_write(
    root: Path, session_id: str, agent_id: str, path: str, tool_input: object = None
) -> Optional[str]:
    """Print `path` for the live run that owns the subagent's write at the moment its executor wrote it.

    Only a subagent write counts (`agent_id` set): the EM's own hand edits are not the run's bytes.
    Records nothing when `resolve_run` finds no unique run, or the path is outside `root`.
    An `Edit` (`tool_input`) that started from bytes other than the run's last print drops the print,
    so the checkpoint withholds the path rather than absorbing the peer's edit.
    Returns the run id recorded for, else None. Zero spawn; never raises.
    """
    if not (session_id and agent_id):
        return None
    try:
        rel = _repo_relative(Path(root), path)
        if rel is None:
            return None
        run_id = resolve_run(Path(root), session_id, agent_id, rel)
        if run_id is None:
            return None
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
