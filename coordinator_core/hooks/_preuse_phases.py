"""Shared wrapper for the preuse dispatches: per-phase wall+cpu spans and a cooperative deadline stop.

`run_phased` binds leg attribution and a hook deadline around a dispatch chain, then attaches
`slow_phase` (top leg by wall) and `spans` (top 3) to the request row via `op_row_fields`.
A guard loop calls `check_deadline(phase)` between guards; it raises `DeadlineStop`, which
`run_phased` turns into `no_advisory()` plus `deadline_stop=<phase>`.

Invariant: cooperative only. A phase already running is never interrupted; the worker is not
killed. Stdlib-only at import; the door's deadline constant is read lazily.
"""

from __future__ import annotations

import contextvars
import functools
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


def run_phased(op_name: str, params: dict, chain: Callable[[dict], Any]) -> Any:
    """Run `chain(params)` under leg attribution and the hook deadline; attach `slow_phase` and
    `spans` to the bound request row, and answer `no_advisory()` if the chain raises `DeadlineStop`."""
    attr_token = leg_attribution.begin()
    deadline_token = _HOOK_DEADLINE.set(time.monotonic() + _door_deadline_s())
    try:
        return chain(params)
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
