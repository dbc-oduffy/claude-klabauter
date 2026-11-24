"""Idle-tick sweep that ends every un-started stage wait: a waiter whose blocker is dead is woken or its owner refired.

`stage_wait.wake_on_end` fires only from `run_token.consume`/`discard`; a blocker that halts does neither, so
its waiter's record stays `started: false`. The warm idle tick calls `on_idle_tick`: per un-started record it
classifies each blocker that holds no live run token, or a live one that is halted with no launcher escrow
(nothing will end it), by its `halted.json` marker.

- landed, closed, unmarked, or superseded by a landed successor: `stage_wait.wake_on_end` then
  `run_token._resume_stage_waiter` spool the waiter's own refire.
- halted (a launcher minted-not-started halt included) or superseded with no landed successor: the OWNER's
  `engine-ask` refire is spooled (`pending_oracle.spool_refire`, source `stranded-wait`), at most
  `MAX_STRANDED_REFIRES` per owner. Past it the owner is recorded under the waiter's `proceeded_without` and
  dropped from its blockers, so the wait always ends.

A fresh owner refire is spooled only when none is outstanding: the owner re-halted since the last one, or
`REFIRE_COOLDOWN_S` passed with the spool unconsumed. Stat/json reads only; no git, no spawn, no sleep.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.ops.dispatch_emit import co_tenant_owner, halted_run, pending_oracle, run_token, stage_wait
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

SOURCE = "stranded-wait"

#: Owner refires per waiter before the waiter proceeds without the owner.
MAX_STRANDED_REFIRES = pending_oracle.MAX_AUTO_REFIRES

#: Seconds an owner refire may sit unconsumed (owner still halted, marker untouched) before the next attempt.
REFIRE_COOLDOWN_S = 900.0

_LIVE, _ENDED, _REFIRE, _PENDING = "live", "ended", "refire", "pending"

_log = logging.getLogger(__name__)

# Parsed wait records keyed by path, valid while the scandir (mtime_ns, size) is unchanged. The warm
# engine is long-lived, so a steady idle tick opens no record file: an open costs ~1.4ms on a loaded
# Windows box, which over 50 records broke the 50ms tick budget.
_RECORDS: dict = {}


def _cooling(record: dict, owner: str, marker_mtime: float, now: float) -> bool:
    """True while `owner`'s last refire for this waiter is unconsumed and inside the cooldown."""
    last = (record.get("owner_refire_at") or {}).get(owner)
    return last is not None and marker_mtime <= float(last) and now - float(last) < REFIRE_COOLDOWN_S


def _cached_records(root: Path, directory: Path) -> list:
    """`(name, record)` for each `.json` wait record in `directory`, sorted by name; unreadable ones skipped."""
    try:
        entries = sorted((e for e in os.scandir(directory) if e.name.endswith(".json")), key=lambda e: e.name)
    except OSError:
        return []
    out = []
    seen = set()
    for entry in entries:
        key = entry.path
        seen.add(key)
        try:
            st = entry.stat()
        except OSError:
            continue
        sig = (st.st_mtime_ns, st.st_size)
        hit = _RECORDS.get(key)
        if hit is not None and hit[0] == sig:
            record = hit[1]
        else:
            record = stage_wait.read(root, entry.name[: -len(".json")])
            if record is None:
                _RECORDS.pop(key, None)
                continue
            _RECORDS[key] = (sig, record)
        out.append((entry.name, record))
    for stale in [k for k in _RECORDS if k not in seen and os.path.dirname(k) == str(directory)]:
        del _RECORDS[stale]
    return out


def _classify(root: Path, owner: str) -> tuple:
    """`(kind, sizing_rel, marker_mtime)` of a blocker: live (still running or unreadable, left alone), ended,
    refire (halted or superseded, with the sizing its refire needs) or pending (resumed: a refire is running)."""
    try:
        if run_token.is_live(root, owner) and not co_tenant_owner._launcherless_halted(root, owner):
            return _LIVE, None, 0.0
        rdir = record_dir(root, owner)
        marker = halted_run.read_strict(rdir)
    except (ValueError, OSError):
        return _LIVE, None, 0.0
    if marker is None:
        return _ENDED, None, 0.0
    status = marker.get("status")
    if status == halted_run.STATUS_RESUMED:
        return _PENDING, None, 0.0
    sizing_rel = marker.get("sizing_rel")
    if status == halted_run.STATUS_SUPERSEDED and co_tenant_owner._landed_successor(root, owner, sizing_rel):
        return _ENDED, None, 0.0
    if status not in (halted_run.STATUS_HALTED, halted_run.STATUS_SUPERSEDED):
        return _ENDED, None, 0.0
    if not (isinstance(sizing_rel, str) and sizing_rel):
        return _ENDED, None, 0.0
    try:
        mtime = (rdir / halted_run.HALTED_FILE).stat().st_mtime
    except OSError:
        mtime = 0.0
    return _REFIRE, sizing_rel, mtime


def _owner_refire(
    root: Path, path: Path, owner: str, sizing_rel: str, marker_mtime: float, now: float,
) -> Optional[dict]:
    """Spool one refire of `owner` for the waiter record at `path`, or end its wait on the owner past the cap.

    Returns the action dict, plus `waiter` when the record woke; None when a refire is still outstanding."""
    with stage_wait._lock, stage_wait._record_lock(path):
        record = stage_wait.read(root, path.stem)
        if not record or record.get("started") or owner not in (record.get("blockers") or ()):
            return None
        counts = dict(record.get("owner_refires") or {})
        stamps = dict(record.get("owner_refire_at") or {})
        done = int(counts.get(owner, 0))
        if _cooling(record, owner, marker_mtime, now):
            return None
        if done >= MAX_STRANDED_REFIRES:
            record["proceeded_without"] = sorted({*(record.get("proceeded_without") or ()), owner})
            stage_wait._write_quiet(path, record)
            woken = stage_wait._wake_one(root, path, owner)
            return {"waiter": path.stem, "owner": owner, "action": "proceed-without-owner", "woken": woken}
        attempt = done + 1
        pending_oracle.spool_refire(
            root, owner, sizing_rel, attempt, source=SOURCE, reason="stranded-wait: owner halted",
        )
        counts[owner] = attempt
        stamps[owner] = now
        record["owner_refires"], record["owner_refire_at"] = counts, stamps
        stage_wait._write_quiet(path, record)
        return {"waiter": path.stem, "owner": owner, "action": "owner-refire", "attempt": attempt}


def sweep(root: Path, now: Optional[float] = None) -> list[dict]:
    """One pass over `root`'s un-started stage waits; the actions taken (`owner-refire`, `proceed-without-owner`,
    `wake`), each with its `waiter` and `owner`. Idempotent: a second pass with nothing changed acts on nothing."""
    moment = time.time() if now is None else now
    directory = stage_wait._wait_dir(Path(root))
    if directory is None:
        return []
    verdicts: dict[str, tuple] = {}
    actions: list[dict] = []
    resume: list[dict] = []
    ended: list[str] = []
    for name, record in _cached_records(Path(root), directory):
        if record.get("started"):
            continue
        for owner in list(record.get("blockers") or ()):
            if not isinstance(owner, str):
                continue
            if owner not in verdicts:
                verdicts[owner] = _classify(root, owner)
            kind, sizing_rel, mtime = verdicts[owner]
            if kind == _ENDED:
                if owner not in ended:
                    ended.append(owner)
            elif kind == _REFIRE and not _cooling(record, owner, mtime, moment):
                acted = _owner_refire(root, directory / name, owner, sizing_rel, mtime, moment)
                if acted is not None:
                    woken = acted.pop("woken", None)
                    actions.append(acted)
                    if woken:
                        resume.append(woken)
    for owner in ended:
        for waiter in stage_wait.wake_on_end(root, owner):
            resume.append(waiter)
            actions.append({"waiter": waiter["run_id"], "owner": owner, "action": "wake"})
    for waiter in resume:
        try:
            run_token._resume_stage_waiter(root, waiter)
        except Exception:  # noqa: BLE001 -- one waiter's resume failure must not strand the others
            _log.warning("stranded-wait resume of %s failed", waiter.get("run_id"), exc_info=True)
    return actions


def on_idle_tick(repos: Iterable[object], now: Optional[float] = None) -> bool:
    """Run `sweep` over each served repo; True when any wait was acted on. Never raises."""
    any_acted = False
    for repo in repos:
        try:
            if sweep(Path(str(repo)), now):
                any_acted = True
        except Exception:  # noqa: BLE001 -- no stranded-wait failure may escape the idle tick
            _log.warning("stranded-wait sweep failed for %s", repo, exc_info=True)
    return any_acted
