"""Standing gate: `ipc.dispatch_message` on a zero-work handler costs under 5 ms of process time.

C3's attribution (docs/research/2026-10-14-engine-per-op-overhead-attribution.md) measured the
dispatch segment at 2.5 to 3.3 ms per call, below the 5 ms cut threshold, so no dispatch edit is
owed; this pins the floor so a later per-call addition (registry or suspension resolution,
telemetry sink path resolution) that crosses the threshold fails here.

Negative spec: does not assert the warm-engine per-op overhead (that is the engine_op_overhead
gate) and does not measure the handler, which does no work. The measured callable includes
`asyncio.run` loop setup/teardown, and the suite's autouse `_op_latency_never_appends_to_live_hub`
drops the live-hub telemetry append, so the sink write itself is not in this figure.
"""
from __future__ import annotations

import asyncio

from coordinator_core import ipc
from coordinator_core.benchmarks import process_time

DISPATCH_CEILING_MS = 5.0
_OP = "tests.dispatch_message_overhead_noop"


def _burn_handler(_params, repo_root=None):
    import time

    end = time.process_time() + 0.05
    while time.process_time() < end:
        pass
    return {"ok": True}


def test_ceiling_refuses_a_handler_that_burns_cpu():
    saved = ipc._REGISTRY.get(_OP)
    ipc._REGISTRY[_OP] = _burn_handler
    msg = {"jsonrpc": "2.0", "id": 1, "method": _OP, "params": {}}
    try:
        got = process_time.in_process_time_ms(
            lambda: asyncio.run(ipc.dispatch_message(dict(msg)))
        )
    finally:
        if saved is None:
            ipc._REGISTRY.pop(_OP, None)
        else:
            ipc._REGISTRY[_OP] = saved
    assert got["process_time_ms"] >= DISPATCH_CEILING_MS, got


def test_zero_work_dispatch_stays_under_cut_threshold():
    def _noop(_params, repo_root=None):
        return {"ok": True}

    saved = ipc._REGISTRY.get(_OP)
    ipc._REGISTRY[_OP] = _noop
    msg = {"jsonrpc": "2.0", "id": 1, "method": _OP, "params": {}}
    try:
        probe = asyncio.run(ipc.dispatch_message(dict(msg)))
        assert "result" in probe, probe

        def _call():
            asyncio.run(ipc.dispatch_message(dict(msg)))

        got = process_time.in_process_time_ms(_call)
    finally:
        if saved is None:
            ipc._REGISTRY.pop(_OP, None)
        else:
            ipc._REGISTRY[_OP] = saved
    assert got["process_time_ms"] < DISPATCH_CEILING_MS, got
