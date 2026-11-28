"""Idle-tick sweep that ends every un-started stage wait: each typed blocker is evaluated by `blocker_check`.

`stage_wait.wake_on_end` fires only from `run_token.consume`/`discard`; a blocker that halts does neither, so
its waiter's record stays `started: false`. The warm idle tick calls `on_idle_tick`; per un-started record:

- every blocker cleared: the record starts and `run_token._resume_stage_waiter` runs its `resume_action`
  (`refire`, `close_landed` at the landed-marker sha, `notify`), stamped `resumed_by: engine`, `session: null`.
- a halted owner (`live` with a sizing): the OWNER's `engine-ask` refire is spooled (`pending_oracle.spool_refire`,
  source `stranded-wait`), at most `MAX_STRANDED_REFIRES` per owner, once none is outstanding.
- a dead blocker: `on_blocker_dead: surface` holds the park (`status: attention`, one `pending_oracle` PM flag,
  never `proceeded_without`); the flag's removal ends it, `resumed_by: pm`. `proceed` refires a dead owner up to
  the cap, then records it under `proceeded_without` and drops it, so the wait always ends.

Stat/json reads only; no git, no spawn, no sleep. Only a `close_landed` resume spawns, and only on that event.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.ops.dispatch_emit import blocker_check, pending_oracle, run_token, stage_wait

SOURCE = "stranded-wait"

#: Owner refires per waiter before the waiter proceeds without the owner.
MAX_STRANDED_REFIRES = pending_oracle.MAX_AUTO_REFIRES

#: Seconds an owner refire may sit unconsumed (owner still halted, marker untouched) before the next attempt.
REFIRE_COOLDOWN_S = 900.0

STATUS_ATTENTION = "attention"

# invariant: an attention park never expires; `stage_wait.expired` reads this deadline.
_ATTENTION_DEADLINE_S = 10 * 365 * 24 * 3600.0

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


def _verdict(root: Path, blocker: dict, memo: dict, home: Optional[Path]) -> blocker_check.Verdict:
    """`blocker_check`'s verdict of one typed blocker, memoised per sweep; a failing check reads `pending`.

    A `run_landed` blocker on a halted owner reads as that owner's `live` verdict, so the owner is refired."""
    kind, ref = blocker["kind"], blocker["ref"]
    key = (kind, ref, blocker.get("row"), blocker.get("expect"), blocker.get("mtime_after"))
    held = memo.get(key)
    if held is not None:
        return held
    try:
        if kind == "run_end":
            found = blocker_check._run_end(root, ref, home)
        elif kind == "run_landed":
            found = blocker_check._run_landed(root, ref)
            if found.state == blocker_check.PENDING:
                owner = blocker_check._run_end(root, ref, home)
                if owner.state == blocker_check.LIVE and owner.payload.get("sizing_rel"):
                    found = owner
        else:
            found = blocker_check.evaluate(root, blocker)
    except Exception:  # noqa: BLE001 -- one blocker's failing check must not strand the other waiters
        _log.warning("stranded-wait check of %s %s failed", kind, ref, exc_info=True)
        found = blocker_check.Verdict(blocker_check.PENDING, {})
    memo[key] = found
    return found


def _drop_and_start(root: Path, path: Path, record: dict, cleared: set, proceeded: set) -> Optional[dict]:
    """With the record locked: drop the `(kind, ref)` pairs in `cleared` and `proceeded` (the latter recorded under
    `proceeded_without`) and start the record when no blocker is left. The started record, else None."""
    left = [b for b in stage_wait.typed_blockers(record) if (b["kind"], b["ref"]) not in (cleared | proceeded)]
    record["typed_blockers"] = left
    record["blockers"] = sorted({b["ref"] for b in left})
    if proceeded:
        record["proceeded_without"] = sorted({*(record.get("proceeded_without") or ()), *(ref for _, ref in proceeded)})
    if left:
        stage_wait._write_quiet(path, record)
        return None
    if not stage_wait._claim_start(path):
        return None
    stage_wait._notify_started(root, record)
    record["started"] = True
    record["started_at"] = time.time()
    stage_wait._write_quiet(path, record)
    return record


def _apply_drop(root: Path, path: Path, cleared: set, proceeded: set) -> Optional[dict]:
    """`_drop_and_start` on the freshly read record; None for a record that is gone, started or held for the PM."""
    with stage_wait._lock, stage_wait._record_lock(path):
        record = stage_wait.read(root, path.stem)
        if not record or record.get("started") or record.get("status") == STATUS_ATTENTION:
            return None
        return _drop_and_start(root, path, record, cleared, proceeded)


def _flag_row(kind: str, ref: str) -> str:
    return "blocker-" + re.sub(r"[^A-Za-z0-9._-]", "_", f"{kind}-{ref}")[:80]


def _surface(root: Path, path: Path, blocker: dict, now: float) -> Optional[dict]:
    """Hold the waiter for the PM: one `pending_oracle` flag naming the dead blocker and the wait time, record
    `status: attention`. A held record, or an existing flag for this wait and blocker, is left as is."""
    kind, ref = blocker["kind"], blocker["ref"]
    record = stage_wait.read(root, path.stem)
    if not record or record.get("started") or record.get("status") == STATUS_ATTENTION:
        return None
    waited = max(0.0, now - float(record.get("parked_at") or now))
    citation = f"stage wait of {path.stem} blocked on dead {kind} {ref} for {int(waited)}s; remove this flag to proceed without it"
    try:
        flag = pending_oracle.write_pm_flag(
            root, path.stem, _flag_row(kind, ref), {"owner": ref, "closing_check": {}, "requester_citation": citation}
        )
    except (OSError, ValueError):
        _log.warning("stranded-wait attention flag for %s on %s failed", path.stem, ref, exc_info=True)
        return None
    with stage_wait._lock, stage_wait._record_lock(path):
        fresh = stage_wait.read(root, path.stem)
        if not fresh or fresh.get("started") or fresh.get("status") == STATUS_ATTENTION:
            return None
        fresh.update(
            status=STATUS_ATTENTION,
            flag=str(flag),
            attention={"kind": kind, "ref": ref, "flagged_at": now, "waited_s": round(waited, 1)},
            deadline=now + _ATTENTION_DEADLINE_S,
        )
        stage_wait._write_quiet(path, fresh)
    return {"waiter": path.stem, "owner": ref, "action": STATUS_ATTENTION, "flag": str(flag)}


def hold_close_landed(root: Path, waiter: dict, sha: Optional[str], reason: str) -> None:
    """A `close_landed` resume the handler refused: reopen the start claim, flag the PM once, record
    `status: attention`; the flag's removal resumes the waiter by `refire`."""
    run_id = waiter["run_id"]
    flag = pending_oracle.write_pm_flag(
        root, run_id, "close-landed-refused",
        {"owner": run_id, "closing_check": {}, "requester_citation": f"close_landed of {run_id} at {sha} refused: {reason}"},
    )
    path = stage_wait._path(root, run_id)
    if path is not None:
        try:
            path.with_suffix(stage_wait._STARTED_SUFFIX).unlink()
        except OSError:
            pass
    run_token._stamp_resume(root, run_id, {
        "started": False,
        "status": STATUS_ATTENTION,
        "flag": str(flag),
        "attention": {"kind": "close_landed", "ref": run_id, "sha": sha, "reason": reason, "resume_action": "refire"},
        "deadline": time.time() + _ATTENTION_DEADLINE_S,
    })


def _attention_tick(root: Path, path: Path, record: dict, actions: list, resumes: list) -> None:
    """A held park ends when its flag file is gone: the blocker named by the flag is dropped, recorded under
    `proceeded_without`, and the record resumes `resumed_by: pm` once none is left."""
    flag = record.get("flag")
    if not isinstance(flag, str) or not flag or os.path.exists(flag):
        return
    with stage_wait._lock, stage_wait._record_lock(path):
        fresh = stage_wait.read(root, path.stem)
        if not fresh or fresh.get("started") or fresh.get("status") != STATUS_ATTENTION:
            return
        held = fresh.pop("attention", None) or {}
        fresh.pop("status", None)
        fresh.pop("flag", None)
        fresh["pm_resolved"] = True
        if held.get("resume_action") in stage_wait.RESUME_ACTIONS:
            fresh["resume_action"] = held["resume_action"]
        gone = {(held["kind"], held["ref"])} if held.get("ref") and held.get("kind") != "close_landed" else set()
        woken = _drop_and_start(root, path, fresh, set(), gone)
    if woken is not None:
        resumes.append((woken, None))
        actions.append({"waiter": path.stem, "owner": held.get("ref"), "action": "pm-resolved"})


def _owner_refire(
    root: Path, path: Path, owner: str, sizing_rel: str, marker_mtime: float, now: float,
) -> Optional[dict]:
    """Spool one refire of `owner` for the waiter record at `path`, or end its wait on the owner past the cap.

    Returns the action dict, plus `woken` when the record started; `action: cap` when the cap is spent under a
    surface policy (the caller holds the waiter for the PM); None when a refire is still outstanding."""
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
            if record.get("on_blocker_dead", stage_wait.ON_BLOCKER_DEAD[0]) != "proceed":
                return {"waiter": path.stem, "owner": owner, "action": "cap"}
            gone = {(b["kind"], b["ref"]) for b in stage_wait.typed_blockers(record) if b["ref"] == owner}
            woken = _drop_and_start(root, path, record, set(), gone)
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


def _tick_record(
    root: Path, path: Path, record: dict, memo: dict, home: Optional[Path], now: float, actions: list, resumes: list,
) -> None:
    """Evaluate one un-started record's typed blockers and act: drop the cleared, refire a halted owner, hold or
    proceed past a dead one."""
    cleared: set = set()
    shas: list = []
    dead: list = []
    refire: list = []
    for blocker in record.get("typed_blockers") or ():
        state, payload = _verdict(root, blocker, memo, home)
        key = (blocker["kind"], blocker["ref"])
        if state == blocker_check.CLEARED:
            cleared.add(key)
            if key[0] == "run_landed" and payload.get("sha"):
                shas.append(payload["sha"])
        elif state == blocker_check.DEAD:
            dead.append((blocker, payload))
        elif state == blocker_check.LIVE and payload.get("sizing_rel"):
            refire.append((blocker, payload))
    surface = record.get("on_blocker_dead", stage_wait.ON_BLOCKER_DEAD[0]) != "proceed"
    if dead and surface:
        if cleared:
            _apply_drop(root, path, cleared, set())
        acted = _surface(root, path, dead[0][0], now)
        if acted is not None:
            actions.append(acted)
        return
    proceeded: set = set()
    for blocker, payload in dead:
        if payload.get("sizing_rel"):
            refire.append((blocker, payload))
        else:
            proceeded.add((blocker["kind"], blocker["ref"]))
    for blocker, payload in refire:
        owner, mtime = blocker["ref"], payload.get("marker_mtime", 0.0)
        if _cooling(record, owner, mtime, now):
            continue
        acted = _owner_refire(root, path, owner, payload["sizing_rel"], mtime, now)
        if acted is None:
            continue
        woken = acted.pop("woken", None)
        if acted["action"] == "cap":
            acted = _surface(root, path, blocker, now)
            if acted is None:
                continue
        actions.append(acted)
        if woken:
            resumes.append((woken, None))
    if cleared or proceeded:
        woken = _apply_drop(root, path, cleared, proceeded)
        if woken is not None:
            ref = sorted(ref for _, ref in cleared | proceeded)[-1]
            actions.append({"waiter": path.stem, "owner": ref, "action": "wake" if cleared else "proceed-without-owner"})
            resumes.append((woken, shas[0] if shas else None))


def sweep(root: Path, now: Optional[float] = None) -> list[dict]:
    """One pass over `root`'s un-started stage waits; the actions taken (`owner-refire`, `proceed-without-owner`,
    `wake`, `attention`, `pm-resolved`), each with its `waiter` and `owner`. Idempotent: a second pass with
    nothing changed acts on nothing."""
    moment = time.time() if now is None else now
    directory = stage_wait._wait_dir(Path(root))
    if directory is None:
        return []
    home = run_token._record_home(Path(root))
    memo: dict = {}
    actions: list[dict] = []
    resumes: list = []
    for name, record in _cached_records(Path(root), directory):
        if record.get("started"):
            continue
        path = directory / name
        if record.get("status") == STATUS_ATTENTION:
            _attention_tick(root, path, record, actions, resumes)
        else:
            _tick_record(root, path, record, memo, home, moment, actions, resumes)
    for waiter, sha in resumes:
        try:
            run_token._resume_stage_waiter(root, waiter, sha=sha)
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
