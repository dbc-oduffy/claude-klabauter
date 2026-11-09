"""coordinator_core.warm.engine_converge -- background convergence of this box's
engine clone to its upstream.

A box's engine clone only caught up when that box published or ran
copy-currency-refresh, so a peer's publish left the warm engine without the new
ops. `schedule_convergence` is called from `push_cadence.on_idle_tick` (the
existing watchdog tick); it starts a daemon thread and returns without running
git. The thread runs `run_convergence_pass`: resolve the clone as
`skew._default_engine_clone` does, call `percolate.dest_refresh.fast_forward_consumer`
(fetch, then ff-only when clean and strictly behind), and write the outcome to
`<machine-local>/engine-converge.json`. A fast-forward changes
`coordinator_core/_engine_stamp`, so the next client token mismatches and
`warm.skew` evicts the stale server; no handover code lives here.

INVARIANT: a dirty or diverged clone is recorded as refused and never touched.
The fetch is a remote round trip and is exempt from the process bar only because
it runs on this background thread.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "CONVERGE_INTERVAL_SECS",
    "RECORD_NAME",
    "record_path",
    "run_convergence_pass",
    "schedule_convergence",
    "reset_converge_for_test",
]

#: Matches `push_cadence.PUSH_CADENCE_INTERVAL_SECS`: how stale a box may sit
#: after a peer publish.
CONVERGE_INTERVAL_SECS = 600.0

RECORD_NAME = "engine-converge.json"

_lock = threading.Lock()
_last_scheduled: Optional[float] = None
_in_flight = False


def reset_converge_for_test() -> None:
    global _last_scheduled, _in_flight
    with _lock:
        _last_scheduled = None
        _in_flight = False


def record_path() -> Path:
    from coordinator_core._settings_home import machine_local_dir

    return machine_local_dir() / RECORD_NAME


def _default_fast_forward(clone: Path):
    lib = str(clone / "coordinator" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from percolate.dest_refresh import fast_forward_consumer

    return fast_forward_consumer(clone)


def _outcome(result) -> str:
    reason = getattr(result, "reason", "") or ""
    if getattr(result, "ok", False):
        return "fast_forwarded" if getattr(result, "fast_forwarded", False) else "level"
    if reason.startswith("could not fetch origin"):
        return "fetch_failed"
    if getattr(result, "dirty", False):
        return "refused_dirty"
    if getattr(result, "ahead", 0) and getattr(result, "behind", 0):
        return "refused_diverged"
    return "refused"


def _record(path: Path, payload: dict) -> None:
    from coordinator_core.atomic_replace import atomic_write_bytes

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))


def run_convergence_pass(
    *,
    clone: Optional[Path] = None,
    fast_forward: Optional[Callable[[Path], object]] = None,
    record_to: Optional[Path] = None,
    clock: Callable[[], float] = time.time,
) -> dict:
    """One bounded question: is the engine clone behind its upstream, and if
    clean, fast-forward it. Returns the recorded payload; never raises.
    """
    payload: dict = {"checked_at": clock(), "pid": os.getpid()}
    try:
        if clone is None:
            from coordinator_core.warm.skew import _default_engine_clone

            clone = _default_engine_clone()
        clone = Path(clone)
        payload["clone"] = str(clone)
        if not (clone / ".git").exists():
            payload.update(outcome="not_a_git_clone", ok=False, reason=f"{clone} has no .git")
        else:
            result = (fast_forward or _default_fast_forward)(clone)
            payload.update(
                outcome=_outcome(result),
                ok=bool(getattr(result, "ok", False)),
                reason=getattr(result, "reason", "") or "",
                branch=getattr(result, "branch", None),
                upstream=getattr(result, "upstream", None),
                ahead=int(getattr(result, "ahead", 0) or 0),
                behind=int(getattr(result, "behind", 0) or 0),
                fast_forwarded=bool(getattr(result, "fast_forwarded", False)),
                dirty=bool(getattr(result, "dirty", False)),
            )
    except Exception as exc:  # noqa: BLE001 -- a background pass must never raise
        payload.update(outcome="error", ok=False, reason=f"{type(exc).__name__}: {exc}")
    try:
        _record(record_to if record_to is not None else record_path(), payload)
    except Exception as exc:  # noqa: BLE001 -- an unwritable record must not kill the pass
        payload["record_error"] = f"{type(exc).__name__}: {exc}"
    return payload


def _pass_thread(pass_fn: Callable[[], object]) -> None:
    global _in_flight
    try:
        pass_fn()
    except Exception:  # noqa: BLE001 -- background thread must not raise
        pass
    finally:
        with _lock:
            _in_flight = False


def schedule_convergence(
    *,
    clock: Callable[[], float] = time.monotonic,
    interval_secs: float = CONVERGE_INTERVAL_SECS,
    pass_fn: Optional[Callable[[], object]] = None,
    start: Callable[[threading.Thread], None] = lambda t: t.start(),
) -> bool:
    """Start a background pass iff one is due and none is running; True iff
    started. Returns without running git: the first call after boot is due at
    once, later ones every `interval_secs`. The default pass is a no-op under
    pytest so an idle tick in a test never fetches the real clone.
    """
    global _last_scheduled, _in_flight
    if pass_fn is None:
        if "PYTEST_CURRENT_TEST" in os.environ:
            return False
        pass_fn = run_convergence_pass
    now = clock()
    with _lock:
        if _in_flight:
            return False
        if _last_scheduled is not None and now - _last_scheduled < interval_secs:
            return False
        _last_scheduled = now
        _in_flight = True
    thread = threading.Thread(
        target=_pass_thread, args=(pass_fn,), name="engine-converge", daemon=True
    )
    try:
        start(thread)
    except Exception:  # noqa: BLE001 -- a failed spawn must not wedge the in-flight flag
        with _lock:
            _in_flight = False
        return False
    return True
