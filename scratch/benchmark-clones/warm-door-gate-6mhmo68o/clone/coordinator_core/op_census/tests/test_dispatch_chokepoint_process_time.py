"""Guards for process-time recording at the `ipc.dispatch_message` chokepoint.

the-meter-02 AC-6: *every* op invocation records its own process time and spawn
count, so the gap X-01 names cannot reopen. Before this, recording lived at four
OUTER entry points and 17 of 64 observed ops -- eight of them live kill-ledger
CANDIDATES -- recorded no process time at all. These tests pin the three ways
that guarantee could rot: coverage falling back to per-entry-point, a parent
absorbing its children's CPU, and a contaminated span claiming to be per-op.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from coordinator_core import ipc


def _burn(ms: float) -> None:
    end = time.process_time() + ms / 1000.0
    while time.process_time() < end:
        pass


@pytest.fixture
def sink(monkeypatch):
    """Capture telemetry rows instead of writing them to the real sink."""
    import coordinator_core.telemetry.op_latency as ol

    rows = []
    monkeypatch.setattr(ol, "_write_entry", lambda entry, repo_root=None: rows.append(entry))
    try:
        ipc.allow_unstamped_dispatch()
    except Exception:
        pass
    return rows


def _process_rows(rows, op):
    return [r for r in rows if r.get("kind") == "process_time" and r.get("op") == op]


def _dispatch(op):
    return asyncio.run(
        ipc.dispatch_message(
            {"jsonrpc": "2.0", "id": 1, "method": op, "params": {}}, caller="test.harness"
        )
    )


def test_an_op_invoked_through_no_outer_entry_point_still_records(sink) -> None:
    """The coverage gap itself: a bare `dispatch_message` call records nothing
    but `started`/`complete` before this. It must now carry both brightline axes."""
    @ipc.register_op("test.chokepoint_bare")
    def _h(params, repo_root=None):
        _burn(120)
        return {"exit_code": 0}

    _dispatch("test.chokepoint_bare")
    rows = _process_rows(sink, "test.chokepoint_bare")
    assert len(rows) == 1
    assert rows[0]["process_ms"] > 0
    assert "spawns" in rows[0]
    assert rows[0]["source_path"] == "dispatch_chokepoint"


def test_a_parent_does_not_absorb_its_children_cpu(sink) -> None:
    """`process_time()` is process-wide, so a naive parent delta contains every
    child's CPU — an op composing three others would read as the cost of all
    four, and the brightline would convict the wrong one."""
    @ipc.register_op("test.chokepoint_kid")
    def _kid(params, repo_root=None):
        _burn(300)
        return {"exit_code": 0}

    @ipc.register_op("test.chokepoint_dad")
    def _dad(params, repo_root=None):
        _burn(500)
        _dispatch("test.chokepoint_kid")
        return {"exit_code": 0}

    _dispatch("test.chokepoint_dad")
    dad = _process_rows(sink, "test.chokepoint_dad")[0]["process_ms"]
    kid = _process_rows(sink, "test.chokepoint_kid")[0]["process_ms"]
    assert 300 <= kid < 500, kid
    # The parent's own 500ms, NOT 800ms. Generous upper bound: this asserts the
    # child was excluded, not the resolution of the platform's CPU clock.
    assert 400 <= dad < 700, dad


def test_an_awaiting_ancestor_is_not_contamination(sink) -> None:
    """A parent blocked awaiting its child burns no CPU during the child's span.
    Counting it as a concurrent sibling would label every composed op
    PROCESS_WIDE and pessimise exactly the ops most worth measuring."""
    @ipc.register_op("test.nest_kid")
    def _kid(params, repo_root=None):
        _burn(60)
        return {"exit_code": 0}

    @ipc.register_op("test.nest_dad")
    def _dad(params, repo_root=None):
        _dispatch("test.nest_kid")
        return {"exit_code": 0}

    _dispatch("test.nest_dad")
    for op in ("test.nest_dad", "test.nest_kid"):
        assert _process_rows(sink, op)[0]["measurement_scope"] == ipc.MEASUREMENT_SCOPE_PER_OP_HANDLER


def test_a_concurrent_sibling_downgrades_the_scope(sink) -> None:
    """The honest half: threads sharing one `process_time()` clock genuinely do
    contaminate each other, and the row must say so rather than overclaim."""
    @ipc.register_op("test.sibling")
    def _h(params, repo_root=None):
        _burn(150)
        return {"exit_code": 0}

    threads = [threading.Thread(target=lambda: _dispatch("test.sibling")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    scopes = {r["measurement_scope"] for r in _process_rows(sink, "test.sibling")}
    assert scopes == {ipc.MEASUREMENT_SCOPE_PROCESS_WIDE}, scopes


def test_the_in_flight_counter_survives_a_raising_handler(sink) -> None:
    """A leaked in-flight slot would permanently downgrade every later row on
    this process to PROCESS_WIDE — a silent, sticky loss of the per-op scope."""
    @ipc.register_op("test.chokepoint_boom")
    def _h(params, repo_root=None):
        raise RuntimeError("boom")

    before = ipc._IN_FLIGHT_DISPATCHES
    _dispatch("test.chokepoint_boom")
    assert ipc._IN_FLIGHT_DISPATCHES == before
    assert _process_rows(sink, "test.chokepoint_boom"), "a failed op still cost CPU"


def test_meter_never_blends_measurement_scopes() -> None:
    """Three scopes measure three different spans. Averaging across them yields
    a number in no unit at all — the hazard `measurement_scope` exists to stop."""
    from coordinator_core.op_census import meter

    assert meter.DEFAULT_SCOPE == ipc.MEASUREMENT_SCOPE_PER_OP_HANDLER
    with pytest.raises(ValueError, match="unknown measurement scope"):
        meter.measure(ipc.Path("."), scope="per-op-handler")
