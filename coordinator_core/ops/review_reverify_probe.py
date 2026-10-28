"""
coordinator_core.ops.review_reverify_probe -- a pre-review probe beside the review.prep snapshot.

``open_probe`` stashes each path's current (post-review) bytes under ``<snapshot_dir>/probe/<sha256>``,
records them in ``probe.json``, then writes the caller's probe bodies into the tree so the
test-runner can re-run failing ids against a pre-review tree. ``close_probe`` puts the post-review
bytes back, writes ``probe-closed.json`` and removes ``probe.json``. ``closed_rerun`` is the set of
ids a closed probe re-ran: the only ids whose pre-review status is vouched for.

Trap: ``probe.json`` is written before any tree write, so a crash always leaves a closable record.
``open_probe`` deletes a prior ``probe-closed.json`` first so an earlier fire's record never vouches
for this fire.

Negative-spec: zero process spawns, no git verb, no test execution. Every write goes through
``atomic_write_bytes`` and ``declare_write``. A refusal is returned as ``error`` with nothing
written, never raised.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Set

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops.dispatch_emit.run_ownership import LiveRun, blocks, owns_path
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.session.declared_writes import declare_write

_PROBE = "probe.json"
_CLOSED = "probe-closed.json"
_STASH = "probe"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _put(path: Path, data: bytes) -> None:
    atomic_write_bytes(path, data)
    declare_write(path)


def _put_json(path: Path, obj: dict) -> None:
    _put(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _read_json(path: Path) -> Optional[dict]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def is_open(snapshot_dir: Path) -> bool:
    """True while ``probe.json`` exists."""
    return (Path(snapshot_dir) / _PROBE).is_file()


def open_probe(
    repo_root: Path,
    snapshot_dir: Path,
    writes: Dict[str, bytes],
    rerun: List[str],
    run_id: str = "",
    live_runs: Optional[Mapping[str, LiveRun]] = None,
) -> dict:
    """Stash current bytes, record them, then write ``writes`` into the tree.

    Refuses (``error``, nothing written) when a probe is already open, a path escapes the root, a
    path has no current file to stash, or (with ``live_runs``) a path is another live run's withheld
    or claimed path. A write that fails midway restores every path already probed.
    """
    repo_root, snapshot_dir = Path(repo_root), Path(snapshot_dir)
    out = {"probed": [], "rerun": list(rerun), "record": str(snapshot_dir / _PROBE), "error": None}
    if is_open(snapshot_dir):
        out["error"] = "a probe is already open; close it first"
        return out
    if live_runs:
        foreign = sorted(r for r in writes if blocks(owns_path(run_id, r, live_runs)))
        if foreign:
            out["error"] = "path outside this run (another live run's): " + ", ".join(foreign)
            return out
    current: Dict[str, bytes] = {}
    for rel in writes:
        target = repo_root / rel
        if contained_path(target, [repo_root]) is None:
            out["error"] = f"path escapes the worktree: {rel}"
            return out
        try:
            current[rel] = target.read_bytes()
        except OSError:
            out["error"] = f"cannot read current bytes of {rel}"
            return out
    (snapshot_dir / _CLOSED).unlink(missing_ok=True)
    files: Dict[str, dict] = {}
    for rel in sorted(writes):
        post = _sha(current[rel])
        _put(snapshot_dir / _STASH / post, current[rel])
        files[rel] = {"post": post, "probe": _sha(writes[rel])}
    _put_json(snapshot_dir / _PROBE, {"opened_at": _now(), "rerun": list(rerun), "files": files})
    done: List[str] = []
    try:
        for rel in sorted(writes):
            _put(repo_root / rel, writes[rel])
            done.append(rel)
    except Exception as exc:
        stuck: List[str] = []
        for rel in done:
            try:
                _put(repo_root / rel, current[rel])
            except Exception:  # keep probe.json: close_probe restores from the stash
                stuck.append(rel)
        if stuck:
            out["error"] = f"probe write failed: {exc}; restore failed for {', '.join(stuck)}, probe left open"
            return out
        (snapshot_dir / _PROBE).unlink(missing_ok=True)
        out["error"] = f"probe write failed, {len(done)} path(s) restored: {exc}"
        return out
    out["probed"] = sorted(writes)
    return out


def close_probe(repo_root: Path, snapshot_dir: Path) -> dict:
    """Restore post-review bytes, write ``probe-closed.json``, remove ``probe.json``.

    With no open probe: empty lists, no error. Refuses, writing nothing, when any file's current
    bytes match neither its probe nor its post sha.
    """
    repo_root, snapshot_dir = Path(repo_root), Path(snapshot_dir)
    out = {"restored": [], "rerun": [], "error": None}
    record_path = snapshot_dir / _PROBE
    if not record_path.is_file():
        return out
    record = _read_json(record_path)
    if record is None or not isinstance(record.get("files"), dict):
        out["error"] = f"unreadable probe record: {record_path}"
        return out
    rerun = [str(i) for i in record.get("rerun") or []]
    out["rerun"] = rerun
    todo: List[str] = []
    bad: List[str] = []
    bodies: Dict[str, bytes] = {}
    for rel, shas in sorted(record["files"].items()):
        target = repo_root / rel
        if contained_path(target, [repo_root]) is None:
            bad.append(rel)
            continue
        try:
            now = _sha(target.read_bytes())
        except OSError:
            bad.append(rel)
            continue
        if now == shas.get("post"):
            continue
        if now != shas.get("probe"):
            bad.append(rel)
            continue
        try:
            body = (snapshot_dir / _STASH / shas["post"]).read_bytes()
        except (OSError, KeyError):
            bad.append(rel)
            continue
        if _sha(body) != shas["post"]:
            bad.append(rel)
            continue
        todo.append(rel)
        bodies[rel] = body
    if bad:
        out["error"] = "probe files changed or unrecoverable: " + ", ".join(bad)
        return out
    for rel in todo:
        _put(repo_root / rel, bodies[rel])
    _put_json(snapshot_dir / _CLOSED, {"closed_at": _now(), "rerun": rerun})
    record_path.unlink()
    out["restored"] = todo
    return out


def closed_rerun(snapshot_dir: Path) -> Set[str]:
    """The ``rerun`` ids of ``probe-closed.json``, or an empty set."""
    record = _read_json(Path(snapshot_dir) / _CLOSED)
    if record is None:
        return set()
    return {str(i) for i in record.get("rerun") or []}
