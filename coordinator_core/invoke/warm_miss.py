"""
coordinator_core.invoke.warm_miss -- the one warm-miss policy for the op/CLI door.

Purpose: after `try_warm_dispatch` returns None, `settle_warm_miss` waits once,
bounded, for the respawned warm server, and otherwise announces the cold run on
stderr. Only the cold `_dispatch_argv_body` calls it; cc_invoke's stamped
in-process rung serves the op itself, so neither the wait nor the defect line
applies there.

Import safety: module-level imports are stdlib only; every coordinator_core
import is deferred into the function that needs it.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import sys
from typing import Iterator, Optional, Tuple

#: How long `_wait_for_warm_boot` may wait for a just-spawned warm server to
#: start answering, in seconds. `COORDINATOR_WARM_BOOT_WAIT_SECS` overrides it;
#: `0` disables the wait entirely (miss -> immediate refusal).
#:
#: SIZED FROM THE MEASURED BOOT, NOT A GUESS. `server-boot.jsonl`, 49 boots to
#: 2026-09-21: spawn-to-ready median 0.47s, p90 0.78s, max 1.04s. 2s is twice
#: the worst boot on record. A wait longer than that is not waiting on a boot:
#: `client-boot-wait.jsonl` showed the retired 15s bound serving 15 of 249 waits
#: inside 2s and nearly all the rest never or at ~30s -- a server already up
#: and not answering, which no boot wait can fix and a long one only hides.
#: If boots slow past this bound, that is the defect to find; do not widen it.
#:
#: NEVER REACHABLE FROM A HOOK. This wait is for the op/CLI door, where a
#: caller is already waiting on a result. A hook path must pass
#: `COORDINATOR_WARM_BOOT_WAIT_SECS=0` in the child it spawns: hooks fire on
#: the session and commit hot path where blocking is never acceptable.
#: -> state/bug-backlog/2026-08-26-sixteen-hundred-warm-misses-in-thirteen-seconds.yaml
WARM_BOOT_WAIT_SECS = 2.0

#: First poll interval, and the cap it backs off to. Fast at the start because
#: the case this exists for is a server that is nearly up; capped at a second
#: because a tighter tail buys nothing and every poll is a pipe open. Polling
#: cannot storm the spawn path: `warm.client._spawn_once` is one-per-process
#: (`_spawned_this_process`) and cross-process debounced by
#: `breadcrumb.should_spawn`, so every poll after the first re-uses the spawn
#: already in flight rather than triggering another.
#:
#: That claim is about SPAWNS and answers only the spawn question. It says
#: nothing about MISSES, which are recorded per poll and per process -- and a
#: burst of them is a real observed shape on this box, not a hypothetical
#: (see `WARM_BOOT_WAIT_SECS` above). Do not read it as covering both.
_BOOT_POLL_MIN_SECS = 0.1
_BOOT_POLL_MAX_SECS = 1.0
_BOOT_POLL_GROWTH = 1.6


def _warm_boot_wait_deadline() -> float:
    """The configured bound, in seconds. Unset -> `WARM_BOOT_WAIT_SECS`;
    unparseable or negative -> the same default, since a malformed knob must
    not silently turn the wait off -- that is what an explicit `0` is for."""
    raw = os.environ.get("COORDINATOR_WARM_BOOT_WAIT_SECS")
    if raw is None or raw.strip() == "":
        return WARM_BOOT_WAIT_SECS
    try:
        value = float(raw)
    except ValueError:
        return WARM_BOOT_WAIT_SECS
    if value < 0:
        return WARM_BOOT_WAIT_SECS
    return value


def _warm_miss_wait_secs() -> float:
    """The most a warm miss spends before the op's own read begins: the missed
    attempt's liveness read (a zero-byte close or a compute-only probe expiry
    inside it goes on to the boot wait), then the boot wait. 0 when the wait is
    off, since a miss then fails at once and the first read was the only one.
    Connect deadlines (0.25s each) ride the caller's start margin."""
    boot = _warm_boot_wait_deadline()
    if boot <= 0:
        return 0.0
    from coordinator_core.warm.client import READ_DEADLINE_SECS

    return READ_DEADLINE_SECS + boot


def _wait_for_warm_boot(msg: dict) -> Tuple[Optional[dict], float]:
    """Poll `try_warm_dispatch` until a warm server serves `msg` or the bound
    expires. Returns `(response, waited_secs)`; `response` is None when nothing
    served the call before the deadline.

    THIS IS THE OP/CLI DOOR, NOT THE HOOK FAST PATH. `warm/client.py`'s
    negative-spec forbids a poll loop inside that module by contract -- it is
    imported on hook paths where blocking is never acceptable, and the value of
    its cold-signal return is that it is bounded by a single round trip. This
    function is the other side of that line: a human or a script invoked one op
    and is already waiting on its result, so waiting a bounded moment for the
    server that op needs beats handing back a refusal for a fault that is
    already healing. Before this existed, the retry interval was supplied by an
    operator guessing, and the guess was unbounded (four sessions lost an
    evening's memo traffic to it, 2026-08-25/26).

    WHY THIS IS NOT BACKSTOP 2. What the PM retired (2026-08-21) was a SILENT
    degrade to a full cold spawn on every miss. This waits for the WARM server,
    announces itself on stderr before it waits, and waits once; when the bound
    expires the caller runs cold LOUDLY (2026-09-21 ruling: an unreachable
    engine passes loudly, never denies).

    Aborts early on a PERMANENT reason established mid-wait
    (`last_cold_reason`): those recur identically on every poll, so waiting the
    deadline out would burn the bound to reach a conclusion already in hand.

    THE BOUND COVERS EACH ATTEMPT'S READ, NOT ONLY WHETHER ONE STARTS. Every
    poll passes what is left of `deadline_secs` down as its read deadline, so
    the wait cannot outrun its own bound by an attempt's blocking read (a
    retired shape: the 15s bound served at a median 30.11s, 2026-09-20). A
    MUTATING op is never the poll: a delivered mutation's read cannot be cut
    short without minting an indeterminate for an op that may merely be slow
    (`try_warm_dispatch`'s negative-spec). It polls with compute-only `ping`,
    and the mutation is dispatched once, after the server answers, on its own
    transport deadline -- outside this bound and outside `waited`.
    -> state/bug-backlog/2026-09-20-the-warm-pool-re-enters-the-engine-by-cold-subprocess.yaml (d)

    Negative-spec:
        - Does NOT retry a served error envelope. Anything well-formed coming
          back is the server answering, which is the condition this waits for.
        - Does NOT print an ETA or a countdown. Boot time is load-dependent,
          and an interval an operator can satisfy is one they will draw a wrong
          conclusion from.
    """
    import time as _time

    from coordinator_core.warm.client import (
        _op_may_mutate,
        last_cold_reason,
        try_warm_dispatch,
    )

    deadline_secs = _warm_boot_wait_deadline()
    if deadline_secs <= 0:
        return None, 0.0

    started = _time.monotonic()
    print(
        "[warm-client] no warm server answered; a respawn was triggered and this call "
        f"is waiting up to {deadline_secs:g}s for it to start answering.",
        file=sys.stderr,
    )
    sys.stderr.flush()

    mutating = _op_may_mutate(msg.get("method"))
    probe = (
        {"jsonrpc": "2.0", "id": msg.get("id"), "method": "ping", "params": {}}
        if mutating
        else msg
    )
    interval = _BOOT_POLL_MIN_SECS
    response = None
    while True:
        _time.sleep(max(0.0, min(interval, deadline_secs - (_time.monotonic() - started))))
        remaining = deadline_secs - (_time.monotonic() - started)
        if remaining <= 0:
            break
        response = try_warm_dispatch(probe, read_deadline_secs=remaining)
        if response is not None:
            break
        if last_cold_reason():
            break
        interval = min(interval * _BOOT_POLL_GROWTH, _BOOT_POLL_MAX_SECS)

    waited = _time.monotonic() - started
    try:
        from coordinator_core.warm.telemetry import record_client_boot_wait

        record_client_boot_wait(
            waited_secs=waited,
            served=response is not None,
            deadline_secs=deadline_secs,
        )
    except Exception:  # noqa: BLE001 -- an instrument may not be why an op fails
        pass
    if mutating and response is not None:
        response = try_warm_dispatch(msg)
    return response, waited


#: True while the caller will serve a miss in its own interpreter (cc_invoke
#: rung 3). The miss is then not a cold spawn, so the "ENGINE UNREACHABLE ...
#: COLD ... defect" register would be false; a terse path=in-process line
#: replaces it. A context flag, not a parameter, so callers that patch
#: `settle_warm_miss` with a one-argument stand-in keep working.
_serving_in_process: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "warm_miss_serving_in_process", default=False
)


@contextlib.contextmanager
def serving_in_process() -> Iterator[None]:
    """Scope in which `settle_warm_miss` words a miss as an in-process serve."""
    token = _serving_in_process.set(True)
    try:
        yield
    finally:
        _serving_in_process.reset(token)


def settle_warm_miss(msg: dict) -> Optional[dict]:
    """Handle a warm miss for `msg`: wait once, bounded, then pass loudly.

    Call only after `try_warm_dispatch(msg)` returned None. Returns the warm
    server's response if one came up inside the bound, else None, meaning the
    caller runs the op cold. Returns None silently when
    `ipc.is_unstamped_dispatch_allowed()` (that caller asked for cold). A
    permanent `last_cold_reason` skips the wait. On None otherwise, prints the
    "ENGINE UNREACHABLE ... COLD" line to stderr once.

    `None` from `try_warm_dispatch` is never a delivered mutation (a delivered
    but unanswered mutation returns the -32004 indeterminate envelope), so
    running cold after this cannot execute an op twice.
    -> coordinator/docs/wiki/coordinator-tripwires/an-unreachable-engine-passes-loudly-never-denies.md (coordinator-content-repo)
    """
    from coordinator_core.ipc import is_unstamped_dispatch_allowed

    if is_unstamped_dispatch_allowed():
        return None

    from coordinator_core.warm.client import last_cold_reason

    reason = last_cold_reason()
    waited = 0.0
    response: Optional[dict] = None
    if not reason:
        response, waited = _wait_for_warm_boot(msg)
        reason = last_cold_reason()
    if response is None:
        why = reason or (
            f"no warm server answered within {waited:.1f}s of a respawn"
            if waited > 0
            else "no warm server answered, and the boot wait is off "
            "in this process"
        )
        if _serving_in_process.get():
            print(
                f"[warm-client] no warm server answered ({why}); "
                f"{msg.get('method')} path=in-process.",
                file=sys.stderr,
            )
        else:
            print(
                f"[warm-client] ENGINE UNREACHABLE -- running {msg.get('method')} "
                f"COLD: {why}. This is a defect (reaching the engine is "
                "budgeted in hundreds of milliseconds), not a queue; the "
                "op still runs, slower.",
                file=sys.stderr,
            )
        sys.stderr.flush()
    return response
