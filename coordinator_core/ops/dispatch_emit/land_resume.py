"""Resumable-landing queue: a finished run whose land_run hit a deadline waits here to be landed again.

One entry per run_id under `<queue_dir>`, states by rename (the `ask_spool.record` shape):
`<run_id>.json` pending -> `<run_id>.claimed` -> `<run_id>.done`. Files are 0600 and written
tmp + `os.replace`, so a reader never sees a partial entry. The directory is the warm runtime dir of
the engine clone (`breadcrumb.svc_dir`), never a path inside a repo.

Invariants: a run_id has at most one live entry; a second `enqueue` bumps `attempts` and `last_leg`
on it and never resets `enqueued_at`. An enqueue that lands while the entry is claimed (the resume
attempt itself hit a deadline) marks it `requeued`, and `finish` then returns it to pending instead
of closing it. A finished run is never re-queued. Every state change appends one line to
`<queue_dir>/land-resume.log`; logging failures are swallowed.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from coordinator_core.ask_spool import record

QUEUE_DIRNAME = "land-resume"
LOG_FILENAME = "land-resume.log"
PENDING_SUFFIX = ".json"
CLAIMED_SUFFIX = ".claimed"
DONE_SUFFIX = ".done"
TMP_SUFFIX = ".tmp"

#: An entry whose `attempts` exceeds this has used its resume budget.
MAX_ATTEMPTS = 5
#: Seconds after `enqueued_at` an entry stops being resumed.
MAX_AGE_SECS = 3600.0
#: A claim older than this whose claimer pid is provably dead is returned to pending.
CLAIM_STALE_SECS = 300.0

OUTCOME_LANDED = "landed"
OUTCOME_EXPIRED = "expired"

FILE_MODE = 0o600
DIR_MODE = 0o700


def queue_dir(root: Optional[Path] = None) -> Path:
    """The queue directory of the engine clone `root` (default: this module's clone)."""
    from coordinator_core.warm import breadcrumb

    return breadcrumb.svc_dir(root) / QUEUE_DIRNAME


def _safe(run_id: object) -> bool:
    return isinstance(run_id, str) and bool(run_id) and not any(c in run_id for c in "/\\") and run_id not in (".", "..")


def _path(root: Optional[Path], run_id: str, suffix: str) -> Path:
    return queue_dir(root) / (run_id + suffix)


def _log(root: Optional[Path], run_id: str, state: str, entry: Optional[dict] = None, note: str = "") -> None:
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tail = ""
        if entry:
            tail = f" attempts={entry.get('attempts')} leg={entry.get('last_leg')}"
        line = f"{stamp} run={run_id} state={state}{tail}{(' ' + note) if note else ''}\n"
        with open(queue_dir(root) / LOG_FILENAME, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except Exception:
        pass


def _write(path: Path, entry: dict) -> None:
    """Atomic 0600 write of `entry` at `path`."""
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{secrets.token_hex(4)}{TMP_SUFFIX}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(entry, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_entry(path: object) -> Optional[dict]:
    """The entry at `path`, or None when it is absent or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and _safe(data.get("run_id")) else None


def load(root: Optional[Path], run_id: str) -> Optional[dict]:
    """The live (pending or claimed) entry for `run_id`, or None."""
    if not _safe(run_id):
        return None
    for suffix in (PENDING_SUFFIX, CLAIMED_SUFFIX):
        entry = read_entry(_path(root, run_id, suffix))
        if entry is not None:
            return entry
    return None


def _fold_into_claim(root: Optional[Path], run_id: str, pending: Path, bumped: dict) -> None:
    """Undo a pending bump that raced a claim: the drainer renamed the entry to `.claimed` between
    the bump's read and write, so the write recreated a second live entry. Drop it and carry the
    bump onto the claim (marked `requeued`)."""
    claimed_path = _path(root, run_id, CLAIMED_SUFFIX)
    claimed = read_entry(claimed_path)
    if claimed is None:
        return
    try:
        os.unlink(pending)
    except OSError:
        return
    claimed["attempts"] = max(int(claimed.get("attempts", 0)), int(bumped.get("attempts", 0)))
    claimed["last_leg"] = bumped.get("last_leg")
    claimed["requeued"] = True
    _write(claimed_path, claimed)


def enqueue(root: Optional[Path], run_id: str, params: dict, leg: Optional[str]) -> bool:
    """Queue `run_id` for a later landing; True when a live entry now exists for it.

    A second call for a live run_id bumps `attempts` and sets `last_leg` on that entry. False
    when the run already finished, `run_id` is unsafe, or the queue could not be written. The caller
    reads `load` + `expired` afterwards: an enqueue past its bounds is still recorded."""
    if not _safe(run_id):
        return False
    try:
        if _path(root, run_id, DONE_SUFFIX).exists():
            return False
        now = time.time()
        for suffix in (PENDING_SUFFIX, CLAIMED_SUFFIX):
            path = _path(root, run_id, suffix)
            entry = read_entry(path)
            if entry is None:
                continue
            entry["attempts"] = int(entry.get("attempts", 0)) + 1
            entry["last_leg"] = leg
            if suffix == CLAIMED_SUFFIX:
                entry["requeued"] = True
            _write(path, entry)
            if suffix == PENDING_SUFFIX:
                _fold_into_claim(root, run_id, path, entry)
            _log(root, run_id, "bumped", entry)
            return True
        entry = {
            "run_id": run_id,
            "repo_root": str(params.get("root") or params.get("repo_root") or "") if isinstance(params, dict) else "",
            "params": params,
            "enqueued_at": now,
            "attempts": 1,
            "last_leg": leg,
        }
        _write(_path(root, run_id, PENDING_SUFFIX), entry)
        _log(root, run_id, "enqueued", entry)
        return True
    except Exception:
        return False


def expired(entry: dict, now: float) -> bool:
    """True when `entry` has more than `MAX_ATTEMPTS` attempts or is older than `MAX_AGE_SECS`."""
    try:
        if int(entry.get("attempts", 0)) > MAX_ATTEMPTS:
            return True
        return now - float(entry.get("enqueued_at", now)) > MAX_AGE_SECS
    except (TypeError, ValueError):
        return True


def _reclaim_stale(root: Optional[Path], directory: Path, now: float) -> None:
    """Return a dead claimer's `.claimed` entry to pending (stale mtime and provably dead pid)."""
    for name in os.listdir(directory):
        if not name.endswith(CLAIMED_SUFFIX):
            continue
        path = directory / name
        try:
            if now - os.stat(path).st_mtime <= CLAIM_STALE_SECS:
                continue
        except OSError:
            continue
        entry = read_entry(path)
        pid = entry.get("claimed_by") if entry else None
        if entry is not None and isinstance(pid, int) and record.pid_state(pid) is not False:
            continue
        run_id = name[: -len(CLAIMED_SUFFIX)]
        try:
            os.rename(path, directory / (run_id + PENDING_SUFFIX))
        except OSError:
            continue
        _log(root, run_id, "reclaimed", entry)


def next_due(root: Optional[Path], now: float) -> Optional[Path]:
    """Claim the oldest pending entry and return its claimed path; None when none is due.

    An expired entry is finished as `expired` and skipped. The empty-queue cost is one failed
    `listdir`. A claim is a rename, so only one drainer wins an entry."""
    directory = queue_dir(root)
    try:
        _reclaim_stale(root, directory, now)
        names = sorted(n for n in os.listdir(directory) if n.endswith(PENDING_SUFFIX))
    except OSError:
        return None
    for name in names:
        run_id = name[: -len(PENDING_SUFFIX)]
        src = directory / name
        dst = directory / (run_id + CLAIMED_SUFFIX)
        entry = read_entry(src)
        if entry is None:
            continue
        if expired(entry, now):
            try:
                os.rename(src, dst)
            except OSError:
                continue
            entry["claimed_by"] = os.getpid()
            _finish_claimed(root, run_id, dst, entry, OUTCOME_EXPIRED)
            continue
        try:
            os.rename(src, dst)
        except OSError:
            continue
        entry["claimed_by"] = os.getpid()
        entry["claimed_at"] = now
        try:
            _write(dst, entry)
        except OSError:
            pass
        _log(root, run_id, "claimed", entry)
        return dst
    return None


def _finish_claimed(root: Optional[Path], run_id: str, claimed: Path, entry: dict, outcome: str) -> None:
    if entry.get("requeued") and outcome != OUTCOME_EXPIRED:
        entry.pop("requeued", None)
        entry.pop("claimed_by", None)
        _write(claimed, entry)
        os.replace(claimed, claimed.with_name(run_id + PENDING_SUFFIX))
        _log(root, run_id, "requeued", entry, f"outcome={outcome}")
        return
    entry["outcome"] = outcome
    entry["finished_at"] = time.time()
    _write(claimed, entry)
    os.replace(claimed, claimed.with_name(run_id + DONE_SUFFIX))
    _log(root, run_id, "finished", entry, f"outcome={outcome}")


def finish(root: Optional[Path], run_id: str, outcome: str) -> bool:
    """Close the claimed entry for `run_id` with `outcome`; True when it changed state.

    An entry re-enqueued during its own claim goes back to pending instead (unless `outcome` is
    `expired`)."""
    if not _safe(run_id):
        return False
    claimed = _path(root, run_id, CLAIMED_SUFFIX)
    entry = read_entry(claimed)
    if entry is None:
        return False
    try:
        _finish_claimed(root, run_id, claimed, entry, outcome)
    except OSError:
        return False
    return True
