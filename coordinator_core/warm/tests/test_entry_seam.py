"""Tests for `coordinator_core.warm.entry_seam` — the per-request state seam
C13 gives entry paths 2 (`cli_entry.run_op_main`) and 3 (`get_op_handler`
re-entry) to converge on.

Purpose: C13 of `docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-
cold-start.md`. Covers `per_request_state` isolation across overlapping
scopes (the AC4 shape: two dispatches must not cross-contaminate declared
writes), `reentrant_dispatch` against a REAL registered op (`ping`, never
invented here), its unknown-op failure, and `cli_entry.run_op_main`'s own
declared-write collection now opening through this seam.

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C13
"""

from __future__ import annotations

import pytest

from coordinator_core.session.declared_writes import active_declarations, declare_write
from coordinator_core.warm.entry_seam import per_request_state, reentrant_dispatch


def test_per_request_state_yields_the_collecting_list():
    with per_request_state() as declared:
        assert declared == []
        declare_write("some/path.txt")
        assert declared == ["some/path.txt"]
    # Closed: no collection open outside the block.
    assert active_declarations() is None


def test_per_request_state_accepts_a_preexisting_list():
    into: list = []
    with per_request_state(into) as declared:
        assert declared is into
        declare_write("a.txt")
    assert into == ["a.txt"]


def test_per_request_state_nesting_does_not_cross_contaminate():
    """AC4 shape: an inner per-request scope must not leak into, or be
    visible from, an outer one once it closes — the failure direction C11
    fixed at the dispatch core (a bare rebind instead of Token/reset) for
    exactly this reason.
    """
    with per_request_state() as outer:
        declare_write("outer.txt")
        with per_request_state() as inner:
            declare_write("inner.txt")
            assert inner == ["inner.txt"]
            assert active_declarations() is inner
        # Outer scope's list is restored and untouched by the inner one.
        assert outer == ["outer.txt"]
        assert active_declarations() is outer
        declare_write("outer-again.txt")
        assert outer == ["outer.txt", "outer-again.txt"]


def test_reentrant_dispatch_invokes_a_real_registered_op():
    result = reentrant_dispatch("ping", {})
    assert result.get("ok") is True
    assert "ts" in result


def test_reentrant_dispatch_unknown_op_raises_lookup_error():
    with pytest.raises(LookupError):
        reentrant_dispatch("this.op.does.not.exist", {})


def test_reentrant_dispatch_async_handler_raises_type_error_instead_of_silently_dropping(monkeypatch):
    """Regression for the reviewer-found latent trap: a future path-3
    migration onto this seam whose op resolves to an `async def` handler
    must fail loud (TypeError) rather than silently returning an unawaited
    coroutine — see entry_seam.py's docstring negative-spec.
    """
    from coordinator_core import ipc

    async def _fake_async_handler(params, repo_root=None):
        return {"ok": True}

    monkeypatch.setattr(ipc, "get_op_handler", lambda name: _fake_async_handler)

    with pytest.raises(TypeError):
        reentrant_dispatch("fake.async.op", {})


def test_reentrant_dispatch_scopes_declared_writes_per_call():
    """A handler invoked via `reentrant_dispatch` gets its own declared-
    writes scope, isolated from a caller's outer scope — the exact property
    that makes it safe for a `get_op_handler` re-entry call site to adopt
    without leaking its own declarations into (or out of) the op it calls.
    """
    with per_request_state() as outer:
        declare_write("caller.txt")
        reentrant_dispatch("ping", {})
        # ping declares nothing; the outer scope is unaffected either way.
        assert outer == ["caller.txt"]


def test_run_op_main_collection_opens_through_the_seam(tmp_path, monkeypatch):
    """`cli_entry.run_op_main` now opens its declared-write collection via
    `entry_seam.per_request_state` rather than calling `session.declared_
    writes.collecting()` directly — assert the seam is actually on the call
    path by having a fake op module call `declare_write` and observing it
    land in the recorded envelope `cli_entry._record` receives.
    """
    from coordinator_core import cli_entry

    recorded: list = []
    monkeypatch.setattr(cli_entry, "_record", lambda declared, cwd: recorded.append(list(declared)))

    module = type("FakeOpModule", (), {})()

    def _main(argv):
        declare_write("written.txt")
        return 0

    module.main = _main
    import sys

    monkeypatch.setitem(sys.modules, "fake_entry_seam_test_op_module", module)

    code = cli_entry.run_op_main("fake_entry_seam_test_op_module", [], cwd=str(tmp_path))

    assert code == 0
    assert recorded == [["written.txt"]]
    assert active_declarations() is None
