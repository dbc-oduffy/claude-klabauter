"""
coordinator_core.distill.autofire -- distill self-fires from the warm idle tick.

`on_idle_tick` is a call on the warm server's existing idle tick (never a thread): per served
repo it fires one detached headless distill run when harvest debt exceeds the threshold and no
autofired run is live there. Exactly-once rests on two guards: the live check over the fire
registry, and an O_EXCL claim file created before the launch. A run that ended without a
recorded success backs the repo off (`backoff.json`) rather than refiring. A kill switch (env or
settings-home flag file) and a directory at the marker script path switch autofire off. Every
failure is recorded in `last_decisions` and logged, never raised (advisory).
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
    BACKOFF_MAX_SECS,
    KILL_SWITCH_ENV,
    KILL_SWITCH_FILE,
    KILL_SWITCH_VALUES,
    REASON_BACKOFF_PREFIX,
    REASON_KILL_SWITCH,
    REASON_SETTINGS_HOME_UNRESOLVED,
    REASON_STOPGAP_DIR,
    AutofireDecision,
    AutofireDisabled,
    Launcher,
)

_log = logging.getLogger(__name__)

CLAIM_REL = Path(".coordinator-local") / "distill-autofire" / "claim.json"
BACKOFF_REL = Path(".coordinator-local") / "distill-autofire" / "backoff.json"
SPECS_REL = Path("archive") / "specs"
LOG_REL = Path("state") / "distillation-log.md"

_LIVE_STATES = ("running", "pending")
# Decisions the stat gate may replay while the inputs are unchanged.
_STAT_GATED_REASONS = ("below-threshold", "log-absent")

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


def _log_size(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _input_stamp(repo: Path) -> tuple:
    """mtimes of the log, archive/specs and its month subdirectories (a new spec in an
    existing month changes only that subdirectory's mtime), plus the log's size: an append
    inside one mtime tick still changes the stamp."""
    specs = repo / SPECS_REL
    subs = []
    try:
        with os.scandir(specs) as entries:
            for entry in entries:
                if entry.is_dir():
                    subs.append((entry.name, entry.stat().st_mtime_ns))
    except OSError:
        pass
    return (_mtime(repo / LOG_REL), _log_size(repo / LOG_REL), _mtime(specs), tuple(sorted(subs)))


def _normalised_stamp(stamp: tuple) -> list:
    """`_input_stamp` as JSON round-trips it (nested lists): compare only this form on both
    sides, or every tick reads "changed" and the back-off never holds."""
    return json.loads(json.dumps(stamp))


def _kill_switch_set() -> Optional[str]:
    """The disabled reason when autofire is switched off right now, else None: the env var, or
    the flag file under the settings home. An unresolvable settings home disables too."""
    if os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in KILL_SWITCH_VALUES:
        return REASON_KILL_SWITCH
    try:
        from coordinator_core._settings_home import settings_home

        flag = settings_home() / KILL_SWITCH_FILE
    except Exception:  # noqa: BLE001 -- unresolved home is a typed reason, never a launch
        return REASON_SETTINGS_HOME_UNRESOLVED
    return REASON_KILL_SWITCH if flag.exists() else None


def _read_backoff(repo: Path) -> Optional[dict]:
    try:
        data = json.loads((repo / BACKOFF_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clear_backoff(repo: Path) -> None:
    try:
        (repo / BACKOFF_REL).unlink()
    except FileNotFoundError:
        pass


def _backoff_reason(state: dict) -> str:
    return f"{REASON_BACKOFF_PREFIX}{state.get('outcome')}:failures={state.get('failures')}"


def _backoff_blocks(repo: Path, stamp: tuple) -> Optional[str]:
    """The `backoff:<outcome>:failures=<n>` reason while a recorded back-off stands and the
    inputs are unchanged; an input change clears it, an expired one stops blocking."""
    state = _read_backoff(repo)
    if state is None:
        return None
    if state.get("stamp") != _normalised_stamp(stamp):
        _clear_backoff(repo)
        return None
    if time.time() < state.get("next_at", 0):
        return _backoff_reason(state)
    return None


def _record_failure(repo: Path, fire_id: object, outcome: str, stamp: tuple) -> str:
    """Record that the last autofired run ended without a recorded success; return its
    `backoff:` reason. next_at = now + INTERVAL * 2**failures, capped at BACKOFF_MAX_SECS."""
    prior = _read_backoff(repo) or {}
    failures = int(prior.get("failures", 0)) + 1
    delay = min(AUTOFIRE_INTERVAL_SECS * 2 ** min(failures, 20), BACKOFF_MAX_SECS)
    state = {
        "fire_id": fire_id,
        "failures": failures,
        "outcome": outcome,
        "next_at": time.time() + delay,
        "stamp": _normalised_stamp(stamp),
    }
    path = repo / BACKOFF_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8", newline="\n")
    return _backoff_reason(state)


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


def _claim_blocks(repo: Path, claim: Path, stamp: tuple) -> Optional[str]:
    """The reason an existing claim blocks this tick, else None; a released claim is removed here.

    A live claimed run blocks as `run-live`. A claim whose record is terminal or missing is
    released: `clean` with a changed input stamp since launch clears the back-off (a refire is
    due if debt remains); anything else records a failure and blocks with its `backoff:`
    reason, so a dead child is never refired on the same tick. A claim naming no fire_id older
    than AUTOFIRE_INTERVAL_SECS (crash between claim and launch) is released with no back-off."""
    from coordinator_core.ops.workflow_fire import fire

    try:
        body = claim.read_text(encoding="utf-8")
        age = time.time() - claim.stat().st_mtime
    except OSError:
        return None
    try:
        data = json.loads(body) if body.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    fire_id = data.get("fire_id")
    reason: Optional[str] = None
    if fire_id:
        record = fire._read_record(fire._record_path(fire._registry_dir(str(repo)), fire_id))
        refreshed = fire._refresh_record_state(record) if record is not None else None
        if refreshed is not None and refreshed.get("state") in _LIVE_STATES:
            return "run-live"
        outcome = (refreshed or {}).get("outcome") or "missing"
        if outcome == "clean" and data.get("stamp") != _normalised_stamp(stamp):
            _clear_backoff(repo)
        else:
            reason = _record_failure(repo, fire_id, outcome, stamp)
    elif age <= AUTOFIRE_INTERVAL_SECS:
        return "run-live"
    try:
        claim.unlink()
    except FileNotFoundError:
        pass
    return reason


def _launch_claimed(repo: Path, launcher: Launcher, debt: int, stamp: tuple) -> AutofireDecision:
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
        json.dumps({"fire_id": (record or {}).get("fire_id"), "stamp": _normalised_stamp(stamp)}),
        encoding="utf-8",
        newline="\n",
    )
    return AutofireDecision(repo, True, "fired", debt)


def _evaluate(repo: Path, launcher: Launcher) -> AutofireDecision:
    """Order: stopgap dir -> back-off -> stat gate -> live/claim -> debt."""
    from coordinator_core.distill import autofire_launch

    if (repo / autofire_launch.FIRE_SCRIPT_REL).is_dir():
        return AutofireDecision(repo, False, REASON_STOPGAP_DIR, 0)
    stamp = _input_stamp(repo)
    reason = _backoff_blocks(repo, stamp)
    if reason:
        return AutofireDecision(repo, False, reason, 0)
    if _stamps.get(repo) == stamp:
        previous = last_decisions.get(repo)
        if previous is not None and not previous.fire and previous.reason in _STAT_GATED_REASONS:
            return previous
    if _autofire_record_live(repo):
        return AutofireDecision(repo, False, "run-live", 0)
    reason = _claim_blocks(repo, repo / CLAIM_REL, stamp)
    if reason:
        return AutofireDecision(repo, False, reason, 0)
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
    return _launch_claimed(repo, launcher, debt, stamp)


def on_idle_tick(
    repos: Iterable, *, clock: Callable[[], float] = time.monotonic, launcher: Optional[Launcher] = None
) -> bool:
    """Evaluate each served repo at most once per AUTOFIRE_INTERVAL_SECS; return True when a
    distill run was launched (the tick then marks an invocation). The kill switch is read first,
    ahead of the interval gate: set, no repo is evaluated and the launcher is never called."""
    if not _lock.acquire(blocking=False):
        return False
    fired = False
    try:
        now = clock()
        disabled = _kill_switch_set()
        seen: set[Path] = set()
        for raw in repos:
            try:
                repo = Path(raw).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if repo in seen or not repo.is_dir():
                continue
            seen.add(repo)
            if disabled:
                last_decisions[repo] = AutofireDecision(repo, False, disabled, 0)
                continue
            if now < _next_tick.get(repo, float("-inf")):
                continue
            _next_tick[repo] = now + AUTOFIRE_INTERVAL_SECS
            try:
                if launcher is None:
                    from coordinator_core.distill import autofire_launch

                    launcher = autofire_launch.launch
                decision = _evaluate(repo, launcher)
            except AutofireDisabled:
                decision = AutofireDecision(repo, False, REASON_STOPGAP_DIR, 0)
            except Exception as exc:  # noqa: BLE001 -- advisory: a failure is a recorded reason
                decision = AutofireDecision(repo, False, f"error:{type(exc).__name__}", 0)
                _log.warning("distill autofire failed for %s: %s", repo, exc)
            last_decisions[repo] = decision
            fired = fired or decision.fire
        return fired
    finally:
        _lock.release()
