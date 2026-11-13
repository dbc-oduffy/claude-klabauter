"""
coordinator_core.distill.autofire -- distill self-fires from the warm idle tick.

`on_idle_tick` is a call on the warm server's existing idle tick (never a thread): per served
repo it fires one detached headless distill run when harvest debt exceeds the threshold and no
autofired run is live there. Exactly-once rests on two guards: the live check over the fire
registry, and an O_EXCL claim file created before the launch. Every failure is recorded in
`last_decisions` and logged, never raised (advisory).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from coordinator_core.distill import harvest_debt
from coordinator_core.distill.autofire_contract import (
    AUTOFIRE_INTERVAL_SECS,
    AUTOFIRE_THRESHOLD,
    AutofireDecision,
    Launcher,
)

_log = logging.getLogger(__name__)

CLAIM_REL = Path(".coordinator-local") / "distill-autofire" / "claim.json"
SPECS_REL = Path("archive") / "specs"
LOG_REL = Path("state") / "distillation-log.md"

_LIVE_STATES = ("running", "pending")

_lock = threading.Lock()
_next_tick: dict[Path, float] = {}
# Stat stamp of the inputs at the last non-firing debt decision; unchanged => skip the recompute.
_stamps: dict[Path, tuple] = {}
# Latest decision per repo, for observability (benchmark, tests, status surfaces).
last_decisions: dict[Path, AutofireDecision] = {}


def _mtime(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _input_stamp(repo: Path) -> tuple:
    """mtimes of the log, archive/specs and its month subdirectories (a new spec in an
    existing month changes only that subdirectory's mtime)."""
    specs = repo / SPECS_REL
    subs = []
    try:
        with os.scandir(specs) as entries:
            for entry in entries:
                if entry.is_dir():
                    subs.append((entry.name, entry.stat().st_mtime_ns))
    except OSError:
        pass
    return (_mtime(repo / LOG_REL), _mtime(specs), tuple(sorted(subs)))


def _record_live(record: dict) -> bool:
    """Liveness through fire.py's refresh path: the stored state is valid only as of
    `status_checked_at` and is not rewritten when the child exits."""
    from coordinator_core.ops.workflow_fire import fire

    if record.get("state") not in _LIVE_STATES:
        return False
    return fire._refresh_record_state(record).get("state") in _LIVE_STATES


def _autofire_record_live(repo: Path) -> bool:
    """True when a registry record for this repo's autofire script is pending or running."""
    from coordinator_core.distill import autofire_launch
    from coordinator_core.ops.workflow_fire import fire

    script = str(autofire_launch.fire_script_path(repo))
    for record in fire._list_records(fire._registry_dir(str(repo))):
        if record.get("script_path") == script and _record_live(record):
            return True
    return False


def _claim_blocks(repo: Path, claim: Path) -> bool:
    """True when an existing claim still stands; a released claim is removed here.

    Released when its fire_id's record is terminal or missing, or when it names no fire_id and
    is older than AUTOFIRE_INTERVAL_SECS (crash between claim and launch)."""
    from coordinator_core.ops.workflow_fire import fire

    try:
        body = claim.read_text(encoding="utf-8")
        age = time.time() - claim.stat().st_mtime
    except OSError:
        return False
    try:
        fire_id = json.loads(body).get("fire_id") if body.strip() else None
    except (ValueError, AttributeError):
        fire_id = None
    if fire_id:
        record = fire._read_record(fire._record_path(fire._registry_dir(str(repo)), fire_id))
        if record is not None and _record_live(record):
            return True
    elif age <= AUTOFIRE_INTERVAL_SECS:
        return True
    try:
        claim.unlink()
    except FileNotFoundError:
        pass
    return False


def _launch_claimed(repo: Path, launcher: Launcher, debt: int) -> AutofireDecision:
    claim = repo / CLAIM_REL
    claim.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return AutofireDecision(repo, False, "claim-held", debt)
    os.close(fd)
    try:
        record = launcher(repo)
    except Exception:
        try:
            claim.unlink()
        except FileNotFoundError:
            pass
        raise
    claim.write_text(
        json.dumps({"fire_id": (record or {}).get("fire_id")}), encoding="utf-8", newline="\n"
    )
    return AutofireDecision(repo, True, "fired", debt)


def _evaluate(repo: Path, launcher: Launcher) -> AutofireDecision:
    stamp = _input_stamp(repo)
    if _stamps.get(repo) == stamp:
        previous = last_decisions.get(repo)
        if previous is not None and not previous.fire:
            return previous
    if _autofire_record_live(repo) or _claim_blocks(repo, repo / CLAIM_REL):
        return AutofireDecision(repo, False, "run-live", 0)
    try:
        result = harvest_debt.compute_harvest_debt(repo / SPECS_REL, repo / LOG_REL)
    except (harvest_debt.DistillationLogMissingError, harvest_debt.DistillationLogUnparseableError):
        _stamps[repo] = stamp
        return AutofireDecision(repo, False, "log-absent", 0)
    debt = len(result.harvest_debt)
    if debt <= AUTOFIRE_THRESHOLD:
        _stamps[repo] = stamp
        return AutofireDecision(repo, False, "below-threshold", debt)
    _stamps.pop(repo, None)
    return _launch_claimed(repo, launcher, debt)


def on_idle_tick(
    repos: Iterable, *, clock: Callable[[], float] = time.monotonic, launcher: Optional[Launcher] = None
) -> bool:
    """Evaluate each served repo at most once per AUTOFIRE_INTERVAL_SECS; return True when a
    distill run was launched (the tick then marks an invocation)."""
    if not _lock.acquire(blocking=False):
        return False
    fired = False
    try:
        now = clock()
        seen: set[Path] = set()
        for raw in repos:
            try:
                repo = Path(raw).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if repo in seen or not repo.is_dir():
                continue
            seen.add(repo)
            if now < _next_tick.get(repo, float("-inf")):
                continue
            _next_tick[repo] = now + AUTOFIRE_INTERVAL_SECS
            try:
                if launcher is None:
                    from coordinator_core.distill import autofire_launch

                    launcher = autofire_launch.launch
                decision = _evaluate(repo, launcher)
            except Exception as exc:  # noqa: BLE001 -- advisory: a failure is a recorded reason
                decision = AutofireDecision(repo, False, f"error:{type(exc).__name__}", 0)
                _log.warning("distill autofire failed for %s: %s", repo, exc)
            last_decisions[repo] = decision
            fired = fired or decision.fire
        return fired
    finally:
        _lock.release()
