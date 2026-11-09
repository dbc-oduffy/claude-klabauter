"""Shared wrapper for the preuse dispatches: per-phase wall+cpu spans and a cooperative deadline stop.

`run_phased` binds leg attribution and a hook deadline around a dispatch chain, then attaches
`slow_phase` (top leg by wall) and `spans` (top 3) to the request row via `op_row_fields`.
A guard loop calls `check_deadline(phase)` between guards; it raises `DeadlineStop`, which
`run_phased` turns into `no_advisory()` plus `deadline_stop=<phase>`.

Invariant: a phase already running is never interrupted and its thread is not killed, but the
reply does not wait for it: the chain runs in a daemon thread and `run_phased` answers
`no_advisory()` with `deadline_stop=in_flight` once the hook deadline's reply allowance is spent.
The abandoned chain stops at its next `check_deadline`. Stdlib-only at import; the door's deadline constant is read lazily.
"""

from __future__ import annotations

import contextvars
import functools
import threading
import time
from typing import Any, Callable, Optional

from coordinator_core import dispatch_abandon
from coordinator_core.bash_guards import leg_attribution
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.telemetry import op_row_fields

# time.monotonic() instant at which the door gives up on this hook; bound by run_phased only.
_HOOK_DEADLINE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "preuse_hook_deadline", default=None
)


# Seconds left on the hook deadline at which `run_phased` stops waiting for the chain thread.
_REPLY_ALLOWANCE_S = 0.1

# Set when `run_phased` has stopped waiting; the abandoned chain sees it in `past_deadline`.
_ABANDONED: contextvars.ContextVar[Optional[threading.Event]] = contextvars.ContextVar(
    "preuse_chain_abandoned", default=None
)


class DeadlineStop(Exception):
    """A guard loop saw `past_deadline()` before starting `phase`."""

    def __init__(self, phase: str) -> None:
        super().__init__(phase)
        self.phase = phase


@functools.lru_cache(maxsize=1)
def _door_deadline_s() -> float:
    from coordinator_core.hooks.submit_engine_ask import _DOOR_HOOK_READ_DEADLINE_S

    return float(_DOOR_HOOK_READ_DEADLINE_S)


def past_deadline() -> bool:
    """True when the dispatch is abandoned or the earlier of the dispatcher's deadline and the
    door's hook deadline is within `REPLY_MARGIN_SECS`; False when neither is bound."""
    if dispatch_abandon.is_abandoned():
        return True
    gone = _ABANDONED.get()
    if gone is not None and gone.is_set():
        return True
    left = dispatch_abandon.remaining()
    hook = _HOOK_DEADLINE.get()
    if hook is not None:
        hook_left = max(0.0, hook - time.monotonic())
        left = hook_left if left is None else min(left, hook_left)
    return left is not None and left <= dispatch_abandon.REPLY_MARGIN_SECS


def check_deadline(phase: str) -> None:
    """Raise `DeadlineStop(phase)` when `past_deadline()`; call before starting a guard or phase."""
    if past_deadline():
        raise DeadlineStop(phase)


def _wait_budget_s() -> float:
    """Seconds `run_phased` may wait for the chain: the earlier of the dispatcher's and the hook's
    deadline, less the reply allowance."""
    left = dispatch_abandon.remaining()
    hook = _HOOK_DEADLINE.get()
    if hook is not None:
        hook_left = hook - time.monotonic()
        left = hook_left if left is None else min(left, hook_left)
    return max(0.0, (left if left is not None else _door_deadline_s()) - _REPLY_ALLOWANCE_S)


def run_phased(op_name: str, params: dict, chain: Callable[[dict], Any]) -> Any:
    """Run `chain(params)` under leg attribution and the hook deadline; attach `slow_phase` and
    `spans` to the bound request row, and answer `no_advisory()` if the chain raises `DeadlineStop`
    or is still running when the hook deadline's reply allowance is spent."""
    attr_token = leg_attribution.begin()
    deadline_token = _HOOK_DEADLINE.set(time.monotonic() + _door_deadline_s())
    try:
        abandoned = threading.Event()
        box: dict[str, Any] = {}
        done = threading.Event()
        ctx = contextvars.copy_context()

        def work() -> None:
            _ABANDONED.set(abandoned)
            try:
                box["value"] = chain(params)
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller thread
                box["error"] = exc
            finally:
                done.set()

        threading.Thread(target=ctx.run, args=(work,), daemon=True).start()
        if not done.wait(_wait_budget_s()):
            abandoned.set()
            op_row_fields.attach(deadline_stop="in_flight")
            return no_advisory()
        err = box.get("error")
        if err is not None:
            raise err
        return box["value"]
    except DeadlineStop as stop:
        op_row_fields.attach(deadline_stop=stop.phase or op_name)
        return no_advisory()
    finally:
        try:
            spans = leg_attribution.summary(top=3)
            if spans:
                op_row_fields.attach(slow_phase=spans[0]["name"], spans=spans)
        finally:
            _HOOK_DEADLINE.reset(deadline_token)
            leg_attribution.end(attr_token)
