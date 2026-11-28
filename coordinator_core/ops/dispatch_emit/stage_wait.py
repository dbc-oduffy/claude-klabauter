"""Bounded stage wait records: a stage whose writes overlap only live engine runs parks here instead of ending.

One record per parked run, `<run-token home>/coordinator-warp-stage-waits/<run_id>.json`, beside the
run-token records (home from `run_token._record_home`). The blocker's own `run_token.consume` (landed)
or `run_token.discard` (released or abandoned) calls `wake_on_end`; no poll, sleep or watcher exists.
`release` restores a claimed record and is not an end.

Resume route (the C0 spike; C2's premise). A woken stage is resumed by the ask spool, not inside the
blocker's op. `wake_on_end` only flips the record, sends the start notice and returns the waiter
(enqueue-only, 0 spawns). The caller then queues the refire the EM runs by hand today, an
`engine-ask {"sizing": <rel>, "resume_run_id": <waiter run_id>}` payload, with
`ask_spool.record.spool_ask`; `ask_spool.drain.run` mints it through `submit_engine_ask.evaluate`,
whose resume branch (`_resume_run_id`, `halted_run_to_resume`) re-stages the run. No new op-catalog op.
`run_token._wake_stage_waiters` only spools; it starts no drain (a spawn on the blocker's op), so the
refire waits for the warm server's own drain (`warm/server.py :: _DrainTrigger.maybe_drain`).
Trap: `ask_spool.drain._live_token_run` treats a `run:<id>` entry whose run still holds a live token as
  already minted (coalesced), so the parked stager's token must be gone before its refire drains.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

BLOCKER_KINDS = ("run_end", "run_landed", "file_state", "oracle_check", "remote_run")
RESUME_ACTIONS = ("refire", "close_landed", "notify")
ON_BLOCKER_DEAD = ("surface", "proceed")

#: Seconds a parked stage may wait for its blockers; past it the baseline refusal applies.
STAGE_WAIT_DEADLINE_S = 7200.0

_END_KINDS = ("run_end", "run_landed")
_DIR = "coordinator-warp-stage-waits"
_STARTED_SUFFIX = ".started"
_LOCK_SUFFIX = ".lock"
_LOCK_STALE_S = 5.0
_LOCK_SPIN_S = 2.0
_lock = threading.Lock()


@contextlib.contextmanager
def _record_lock(path: Path):
    """Cross-process exclusion for one record's read-modify-write: an O_EXCL lock file beside it.

    A lock older than `_LOCK_STALE_S` is a dead holder's and is stolen; past `_LOCK_SPIN_S` the
    caller proceeds unlocked rather than deadlock."""
    lock_path = path.with_suffix(_LOCK_SUFFIX)
    held = False
    end = time.monotonic() + _LOCK_SPIN_S
    while True:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > _LOCK_STALE_S:
                    lock_path.unlink()
                    continue
            except OSError:
                continue
        except OSError:
            break
        if time.monotonic() >= end:
            break
        time.sleep(0.005)
    try:
        yield
    finally:
        if held:
            try:
                lock_path.unlink()
            except OSError:
                pass


def _wait_dir(root: Path) -> Optional[Path]:
    from coordinator_core.ops.dispatch_emit import run_token

    home = run_token._record_home(Path(root))
    return None if home is None else home / _DIR


def _path(root: Path, run_id: str) -> Optional[Path]:
    from coordinator_core.ops.dispatch_emit import run_token

    directory = _wait_dir(root)
    if directory is None or not run_token._RUN_ID_RE.match(run_id or ""):
        return None
    return directory / f"{run_id}.json"


def _write(path: Path, record: dict) -> None:
    from coordinator_core.atomic_replace import atomic_write_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(record, sort_keys=True).encode("utf-8"), preserve_mode=False)


def read(root: Path, run_id: str) -> Optional[dict]:
    """The run's wait record, or None when it is absent, malformed or unreadable."""
    path = _path(root, run_id)
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    record["typed_blockers"] = typed_blockers(record)
    return record


def iso_utc(epoch: float) -> str:
    """`epoch` seconds as an ISO-8601 UTC stamp, second resolution (`2026-11-13T12:21:04Z`)."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalise_blocker(blocker: object) -> Optional[dict]:
    """A string is a `run_end` blocker on that run id; a mapping keeps its extra keys. None when unusable."""
    if isinstance(blocker, str):
        return {"kind": "run_end", "ref": blocker} if blocker else None
    if isinstance(blocker, Mapping):
        ref = blocker.get("ref")
        if not isinstance(ref, str) or not ref:
            return None
        kind = blocker.get("kind", "run_end")
        if kind not in BLOCKER_KINDS:
            raise ValueError(f"stage_wait: unknown blocker kind {kind!r}; one of {BLOCKER_KINDS}")
        return {**blocker, "kind": kind, "ref": ref}
    return None


def typed_blockers(record: Mapping) -> list[dict]:
    """The record's blockers as `{kind, ref, ...}` objects; a legacy string-only record reads as `run_end`."""
    typed = record.get("typed_blockers")
    if isinstance(typed, list):
        return [b for b in typed if isinstance(b, dict) and b.get("ref")]
    out = [_normalise_blocker(b) for b in record.get("blockers") or ()]
    return [b for b in out if b is not None]


def _landed_cleared(root: Path, ref: str) -> bool:
    """True when `blocker_check` finds the `run_landed` blocker's landed marker sha; a failing check is not cleared."""
    # trap: blocker_check imports stage_wait at module level, so this import stays lazy.
    from coordinator_core.ops.dispatch_emit import blocker_check

    try:
        return blocker_check.evaluate(Path(root), {"kind": "run_landed", "ref": ref}).cleared
    except Exception:
        return False


def park(
    root: Path,
    run_id: str,
    blockers: Iterable[object],
    writes: Iterable[str],
    *,
    session_id: str = "",
    now: Optional[float] = None,
    last_activity: Optional[Mapping[str, float]] = None,
    sizing_rel: str = "",
    reason: str = "",
    resume_action: Optional[str] = None,
    on_blocker_dead: Optional[str] = None,
) -> Optional[dict]:
    """Write the run's one wait record naming `blockers`; None when no record home or run id resolves.

    A blocker is a run-id string (a `run_end` blocker) or a `{kind, ref, ...}` mapping with `kind` in
    `BLOCKER_KINDS`. The record keeps `typed_blockers` (the objects) and `blockers` (their refs, for
    readers that test run-id membership). `resume_action` (`RESUME_ACTIONS`, default `refire`) and
    `on_blocker_dead` (`ON_BLOCKER_DEAD`, default `surface`) are record-level; a repeat park that omits
    one keeps the prior value.

    A repeat park of a still-waiting run replaces the blocker set and keeps the first deadline, so
    re-attempts never extend the bound. `session_id` receives the start notice. `last_activity` maps a
    blocker ref to its last-activity epoch; the record carries it as ISO UTC under `last_activity`, so a
    reader tells a live blocker from a dead one without the stage stamp. `sizing_rel` is the refire's
    sizing when the waiter's own token record is gone (a halted run discards it); `reason` tags a park
    that is not a stage-write overlap (`co-tenant-wait`). Both are omitted from the record when empty."""
    path = _path(root, run_id)
    normalised = [n for n in (_normalise_blocker(b) for b in blockers) if n is not None]
    unique = {(b["kind"], b["ref"]): b for b in normalised}
    typed = [unique[key] for key in sorted(unique)]
    names = sorted({b["ref"] for b in typed})
    if path is None:
        return None
    if not typed:
        raise ValueError("stage_wait.park: blockers must be non-empty")
    for value, allowed, label in ((resume_action, RESUME_ACTIONS, "resume_action"), (on_blocker_dead, ON_BLOCKER_DEAD, "on_blocker_dead")):
        if value is not None and value not in allowed:
            raise ValueError(f"stage_wait.park: {label} {value!r} is not one of {allowed}")
    stamp = time.time() if now is None else now
    with _lock, _record_lock(path):
        prior = read(root, run_id)
        keep = prior is not None and not prior.get("started")
        record = {
            "run_id": run_id,
            "blockers": names,
            "typed_blockers": typed,
            "resume_action": resume_action or (prior or {}).get("resume_action") or RESUME_ACTIONS[0],
            "on_blocker_dead": on_blocker_dead or (prior or {}).get("on_blocker_dead") or ON_BLOCKER_DEAD[0],
            "writes": sorted({w for w in writes if isinstance(w, str)}),
            "session_id": session_id,
            "parked_at": prior["parked_at"] if keep else stamp,
            "deadline": prior["deadline"] if keep else stamp + STAGE_WAIT_DEADLINE_S,
            "started": False,
            "last_activity": {b: iso_utc(last_activity[b]) for b in names if b in (last_activity or {})},
        }
        sizing_rel = sizing_rel or (prior or {}).get("sizing_rel") or ""
        reason = reason or (prior or {}).get("reason") or ""
        if sizing_rel:
            record["sizing_rel"] = sizing_rel
        if reason:
            record["reason"] = reason
        _write(path, record)
    return record


def expired(root: Path, run_id: str, *, now: Optional[float] = None) -> bool:
    """True when the run's still-waiting record is past its deadline; no record or a started one is False."""
    record = read(root, run_id)
    if record is None or record.get("started"):
        return False
    stamp = time.time() if now is None else now
    return stamp >= float(record.get("deadline") or 0.0)


def clear(root: Path, run_id: str) -> None:
    """Remove the run's wait record and its start marker; a missing one is already clear."""
    path = _path(root, run_id)
    if path is None:
        return
    for target in (path, path.with_suffix(_STARTED_SUFFIX), path.with_suffix(_LOCK_SUFFIX)):
        try:
            target.unlink()
        except OSError:
            pass


def _notify_started(root: Path, record: dict) -> None:
    from coordinator_core.ask_spool import drain

    try:
        drain._notify(
            str(root),
            {"spool_id": f"stage-wait-{record['run_id']}", "session_id": record.get("session_id")},
            {"status": "started", "run_id": record["run_id"]},
        )
    except OSError:
        pass


def _claim_start(path: Path) -> bool:
    """True for exactly one caller: the marker is created O_EXCL."""
    try:
        fd = os.open(str(path.with_suffix(_STARTED_SUFFIX)), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    os.close(fd)
    return True


def wake_on_end(root: Path, ended_run_id: str) -> list[dict]:
    """Drop `ended_run_id` from every waiter's blockers; return the waiters left with none, each once.

    A returned waiter's record is flipped to started and its session got one start notice; a second
    end event finds it started and returns nothing. Never raises for a bad record: it is skipped."""
    directory = _wait_dir(root)
    if directory is None:
        return []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return []
    woken: list[dict] = []
    for name in names:
        path = directory / name
        with _lock, _record_lock(path):
            record = _wake_one(root, path, ended_run_id)
        if record is not None:
            woken.append(record)
    return woken


def _wake_one(root: Path, path: Path, ended_run_id: str) -> Optional[dict]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("started"):
        return None
    typed = typed_blockers(record)
    hit = [b for b in typed if b["ref"] == ended_run_id and b["kind"] in _END_KINDS]
    if not hit:
        return None
    cleared = [b for b in hit if b["kind"] == "run_end" or _landed_cleared(root, b["ref"])]
    if not cleared:
        return None
    left = [b for b in typed if not any(b is c for c in cleared)]
    record["typed_blockers"] = left
    record["blockers"] = sorted({b["ref"] for b in left})
    if left:
        _write_quiet(path, record)
        return None
    if not _claim_start(path):
        return None
    _notify_started(root, record)
    record["started"] = True
    record["started_at"] = time.time()
    _write_quiet(path, record)
    return record


def _write_quiet(path: Path, record: dict) -> None:
    try:
        _write(path, record)
    except OSError:
        pass
