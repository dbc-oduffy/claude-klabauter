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
OUTCOME_HANDED_OFF = "handed_off"
#: `next_action.kind` of the land call an expired green entry leaves on its run's halt marker.
HANDOFF_KIND = "land"

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


def enqueue(
    root: Optional[Path], run_id: str, params: dict, leg: Optional[str], delay_secs: float = 0.0,
) -> bool:
    """Queue `run_id` for a later landing; True when a live entry now exists for it.

    A second call for a live run_id bumps `attempts` and sets `last_leg` on that entry. A positive
    `delay_secs` is a wait on a running child, not a failed attempt: it sets `not_before` (`next_due`
    skips the entry until then) and leaves `attempts` unbumped; `MAX_AGE_SECS` still bounds it. False
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
            if delay_secs > 0:
                entry["not_before"] = now + delay_secs
            else:
                entry["attempts"] = int(entry.get("attempts", 0)) + 1
                entry.pop("not_before", None)
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
        if delay_secs > 0:
            entry["not_before"] = now + delay_secs
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


def _waiting(entry: dict, now: float) -> bool:
    """True when `entry` carries a `not_before` still in the future."""
    try:
        return float(entry.get("not_before", 0.0)) > now
    except (TypeError, ValueError):
        return False


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

    An expired entry is finished as `expired` (or `handed_off`, see `_hand_off_green`) and skipped.
    The empty-queue cost is one failed `listdir`. A claim is a rename, so only one drainer wins an entry."""
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
            outcome, note = _hand_off_green(run_id, entry)
            _finish_claimed(root, run_id, dst, entry, outcome, note)
            continue
        if _waiting(entry, now):
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


def _land_call(entry: dict) -> str:
    """The one `dispatch.land_run` call that resumes the entry's run: its run id, mode and root."""
    raw = entry.get("params")
    params: dict = raw if isinstance(raw, dict) else {}
    args = {"run_id": entry["run_id"]}
    for key in ("mode", "root"):
        if isinstance(params.get(key), str) and params[key]:
            args[key] = params[key]
    return "coordinator-invoke dispatch.land_run '" + json.dumps(args, separators=(",", ":")) + "'"


def _hand_off_green(run_id: str, entry: dict) -> tuple:
    """`(outcome, log note)` for an expired entry: `handed_off` after stamping the run's halt marker
    with one land-call `next_action` when the run's own records are green, else `expired`.

    No hand-off is invented without the run's records: an empty `repo_root`, a missing run dir, an
    unreadable or non-halted marker, a red or signal-less run, or a failed write all stay `expired`.
    Pure file reads and one atomic write; no git."""
    repo_root = entry.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root:
        return OUTCOME_EXPIRED, "handoff=skipped:no-repo-root"
    try:
        from coordinator_core.atomic_replace import atomic_write_bytes
        from coordinator_core.ops.dispatch_emit.halt_strand_census import WITHHELD_PRINTS_FILE, _green
        from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE
        from coordinator_core.session import record_homes

        run_dir = Path(record_homes.home_dir(str(repo_root), "warp")) / run_id
        if not run_dir.is_dir():
            return OUTCOME_EXPIRED, "handoff=skipped:no-run-dir"
        marker_path = run_dir / HALTED_FILE
        try:
            marker = json.loads(marker_path.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            marker = None
        if not isinstance(marker, dict) or marker.get("status", "halted") != "halted":
            return OUTCOME_EXPIRED, "handoff=skipped:marker-unreadable"
        green, _signals = _green(run_dir)
        if not green:
            return OUTCOME_EXPIRED, "handoff=skipped:not-green"
        arts = marker.get("run_artifacts")
        withheld = arts.get("withheld") if isinstance(arts, dict) else None
        if not isinstance(withheld, dict) or not withheld:
            try:
                withheld = json.loads((run_dir / WITHHELD_PRINTS_FILE).read_bytes().decode("utf-8"))
            except (OSError, ValueError):
                withheld = {}
        paths = sorted(p for p in withheld if isinstance(p, str)) if isinstance(withheld, dict) else []
        marker["next_action"] = {
            "kind": HANDOFF_KIND,
            "op": "dispatch.land_run",
            "call": _land_call(entry),
            "run_id": run_id,
            "last_leg": entry.get("last_leg"),
            "withheld_paths": paths,
        }
        atomic_write_bytes(marker_path, json.dumps(marker, indent=2).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- the drain path never raises; today's `expired` stands
        return OUTCOME_EXPIRED, f"handoff=failed:{type(exc).__name__}"
    return OUTCOME_HANDED_OFF, f"handoff=land-call paths={len(paths)}"


def _finish_claimed(
    root: Optional[Path], run_id: str, claimed: Path, entry: dict, outcome: str, note: str = "",
) -> None:
    tail = f"outcome={outcome}" + (f" {note}" if note else "")
    if entry.get("requeued") and outcome not in (OUTCOME_EXPIRED, OUTCOME_HANDED_OFF):
        entry.pop("requeued", None)
        entry.pop("claimed_by", None)
        _write(claimed, entry)
        os.replace(claimed, claimed.with_name(run_id + PENDING_SUFFIX))
        _log(root, run_id, "requeued", entry, tail)
        return
    entry["outcome"] = outcome
    entry["finished_at"] = time.time()
    _write(claimed, entry)
    os.replace(claimed, claimed.with_name(run_id + DONE_SUFFIX))
    _log(root, run_id, "finished", entry, tail)


def finish(root: Optional[Path], run_id: str, outcome: str) -> bool:
    """Close the claimed entry for `run_id` with `outcome`; True when it changed state.

    `outcome` is `landed`, `expired` or `handed_off`. An entry re-enqueued during its own claim goes
    back to pending instead (unless `outcome` is `expired` or `handed_off`)."""
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
