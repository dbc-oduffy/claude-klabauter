"""The prime exit criterion's own instrument: two callers, two homes, one warm
server -- driven through the REAL pool path, not a stubbed dispatch.

Spec backlink: docs/plans/2026-08-31-the-settings-home-crosses-the-warm-boundary.md
`prime_exit_criterion` and its own C3 dispatch brief.

WHY THIS IS NOT C2'S TEST (asked and settled -- do not re-raise). C2's own
`test_settings_home_served_per_request.py` and `test_settings_home_mismatch_
refusal.py` are UNIT legs: they prove the bind (`per_request_state`'s
`settings_home` axis) and the refusal move behave, each with a monkeypatched
`dispatch_message` or a fake `dispatch=` callable. Neither drives a live
worker PROCESS. This module is the plan's named ACCEPTANCE ORACLE -- the
instrument verification criterion 2 cites -- so it runs through
`_ServerContext._pool_dispatch` and a real `concurrent.futures.
ProcessPoolExecutor`, submitting the unmodified production
`_pool_dispatch_worker` to genuine worker processes. Collapsing this into
C2's unit tests is how a plan ends up certifying itself.

WHY THIS IS NOT THE SCRATCHPAD FALSIFIER, UNCHANGED. The plan's own
`prime_exit_criterion.falsifier` scratchpad script drove `_handle_connection`
with a dispatch STUB, exercising none of `_pool_dispatch`, no worker process,
and no `isolated=True` -- none of the machinery the fix actually consists of.
It is recorded there only as the BASELINE reproduction. This file is authored
fresh against the real pool path, per that block's own instruction.

HOW A DISPATCHED OP REPORTS ITS OWN `settings_home()`, without inventing a
production op or reaching into the frozen op registry. `_pool_dispatch`'s
submit target (`_pool_dispatch_worker`) is a fixed, unparameterized reference
-- the only thing a caller of `_pool_dispatch` controls is the JSON-RPC `msg`
and `caller` payloads, both plain data. So the one legitimate extension point
left is `ProcessPoolExecutor`'s own `initializer=` hook: `_worker_init` below
runs the REAL `server._worker_process_init()` (the same boot-time op-registry
preload every production worker pays) and then registers one additional,
write-free, params-ignoring op -- `test.echo_settings_home` -- that returns
`settings_home()` as resolved INSIDE that worker process at dispatch time.
Registering an op through the pool's own initializer is a customization
point the production code already exposes; it is not a second implementation
of `_pool_dispatch_worker`, `per_request_state`, or the settings-home axis
themselves, all of which run completely unmodified.

THE PINNED FAILING LEG (staff-eng Finding 5's own demand: the oracle must
discriminate, not merely run green). `test_the_check_discriminates_against_
the_pre_fix_shape` re-runs the exact same `_resolves_its_own_home` predicate
against the shape that predates BOTH C2 (the refusal) and C3 (per-request
isolation): a bare `dispatch_message(...)` call carrying no per-request
identity at all, exactly what every dispatch leg did before `per_request_
state`'s `settings_home` axis existed. That shape answers every claimed home
with the process's own ambient one, never the caller's -- the silent-wrong
answer the whole plan exists to close. Without this leg, a green real-pool
test is consistent with a harness that isn't actually discriminating.

Negative-spec: this module does NOT touch `coordinator_core/ops/`,
`coordinator_core/ops/_registry_map.py`, or any other file outside its own
declared `writes:` scope to make the echo op reachable -- the pool
initializer hook above is the only mechanism used.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from dataclasses import replace
from typing import Callable, Optional

from coordinator_core.warm import server
from coordinator_core.warm.caller_context import resolve_caller_context

_ECHO_METHOD = "test.echo_settings_home"


def _register_echo_op() -> None:
    """Register the write-free `test.echo_settings_home` op, idempotently.

    Ignores `params` entirely and touches nothing but `_settings_home.
    settings_home()` -- same write-free discipline `diagnostics_probes.py`
    documents for its own trio, for the same reason: this handler's only
    job is to be trustworthy by inspection.
    """
    from coordinator_core._settings_home import settings_home
    from coordinator_core.ipc import register_op

    def _echo(params: dict, repo_root=None) -> dict:
        return {"settings_home": str(settings_home())}

    register_op(_ECHO_METHOD, _echo)


def _worker_init() -> None:
    """`ProcessPoolExecutor(initializer=...)` target for this module's own
    pool -- runs the real production boot step, then layers on the one
    test-only op registration. Module-level (not a closure) so it survives
    pickling across the `spawn` start method Windows uses.

    Also re-arms `coordinator_core.conftest`'s own dispatch-axis stamp-gate
    opt-in (`ipc.allow_unstamped_dispatch()`) -- `pytest_configure` sets that
    flag once, in-process, in the pytest process; a `spawn`-started worker is
    a fresh interpreter that never ran it, so without this every dispatch
    through this suite's own unstamped working tree would refuse with -32005
    before ever reaching the settings-home axis this module exists to test.
    """
    server._worker_process_init()
    from coordinator_core.ipc import allow_unstamped_dispatch

    allow_unstamped_dispatch()
    _register_echo_op()


def _caller_for(home: Optional[str]) -> server.CallerContext:
    return replace(resolve_caller_context(), settings_home=home)


def _make_pool_ctx() -> "server._ServerContext":
    """A real `_ServerContext` carrying a real `ProcessPoolExecutor`, built
    exactly as `_ensure_dispatch_pool` builds the production one except for
    the `initializer=` swap documented above."""
    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._dispatch_pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=2,
        initializer=_worker_init,
    )
    ctx._dispatch_pool_lock = threading.Lock()
    return ctx


def _echo_msg(request_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": _ECHO_METHOD, "params": {}}


def _resolves_its_own_home(dispatch_call: Callable[[dict, str], dict], home: str, request_id: int) -> bool:
    """True iff a request claiming `home` is answered WARM (no -32008
    SETTINGS_HOME_MISMATCH refusal, no cold fall-through) and the dispatched
    op observes `settings_home() == home` from inside its own execution --
    the prime exit criterion's own predicate, shared by the real-pool leg
    below and the pinned pre-fix leg that proves it discriminates.
    """
    response = dispatch_call(_echo_msg(request_id), home)
    error = response.get("error") if isinstance(response, dict) else None
    if error is not None and error.get("code") == server.SETTINGS_HOME_MISMATCH_ERROR:
        return False
    result = response.get("result") if isinstance(response, dict) else None
    resolved = result.get("settings_home") if isinstance(result, dict) else None
    return resolved == home


def test_two_callers_are_served_warm_concurrently_through_the_real_pool(tmp_path):
    """The prime exit criterion itself: two callers naming different homes,
    served through `_ServerContext._pool_dispatch` and a live worker
    process, each resolving its own claimed home -- neither refused, neither
    observing the other's home.
    """
    ctx = _make_pool_ctx()
    try:
        home_a = str(tmp_path / "home-a")
        home_b = str(tmp_path / "home-b")

        def _pool_dispatch_call(msg: dict, home: str) -> dict:
            return ctx._pool_dispatch(msg, caller=_caller_for(home))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(_resolves_its_own_home, _pool_dispatch_call, home_a, 1)
            future_b = pool.submit(_resolves_its_own_home, _pool_dispatch_call, home_b, 2)
            resolved_a = future_a.result(timeout=60)
            resolved_b = future_b.result(timeout=60)

        assert resolved_a, "caller A was refused or did not resolve its own claimed home"
        assert resolved_b, "caller B was refused or did not resolve its own claimed home"

        # Direct re-check against each response, so a leaked value that
        # happened to equal `home` by coincidence (e.g. both resolving the
        # server's own ambient home) cannot pass the boolean predicate above.
        response_a = ctx._pool_dispatch(_echo_msg(3), caller=_caller_for(home_a))
        response_b = ctx._pool_dispatch(_echo_msg(4), caller=_caller_for(home_b))
        assert response_a["result"]["settings_home"] == home_a
        assert response_b["result"]["settings_home"] == home_b
        assert response_a["result"]["settings_home"] != response_b["result"]["settings_home"]
        assert "error" not in response_a
        assert "error" not in response_b
    finally:
        ctx._dispatch_pool.shutdown(wait=True, cancel_futures=True)


def test_the_check_discriminates_against_the_pre_fix_shape(tmp_path):
    """Pinned failing leg (staff-eng Finding 5). The SAME
    `_resolves_its_own_home` predicate, run against the shape that predates
    both C2's refusal and C3's per-request isolation -- a bare
    `dispatch_message` call carrying no per-request identity binding at all
    -- must go RED: that shape answers every claim with the process's own
    ambient home, never the caller's. Without this leg a green real-pool
    test proves only that the harness runs, not that it discriminates.
    """
    _register_echo_op()

    from coordinator_core.ipc import dispatch_message

    def _pre_fix_dispatch_call(msg: dict, home: str) -> dict:
        # No per_request_state, no settings_home threading at all -- exactly
        # the shape every dispatch leg had before this plan's C2/C3.
        return asyncio.run(dispatch_message(msg, caller="pre-fix-shape-test"))

    other_home = str(tmp_path / "genuinely-different-settings-home")

    assert not _resolves_its_own_home(_pre_fix_dispatch_call, other_home, 5), (
        "the two-homes predicate did not fail against the pre-fix (no per-request "
        "identity) shape -- it is not discriminating, and a green real-pool run "
        "above means nothing"
    )
