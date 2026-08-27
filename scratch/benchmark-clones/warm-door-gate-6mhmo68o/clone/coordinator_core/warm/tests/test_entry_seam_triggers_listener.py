"""C3 of docs/plans/2026-08-25-the-http-listener-gets-something-keeping-it-up.md.

C2 gives the pipe server's own boot a call to `supervisor.ensure_listener()`. That
covers the box where the pipe server boots; it does not cover the box where NEITHER
process is running, which -- given idle demotion -- is the ordinary state after any
quiet period. This pins the second call site: `entry_seam.try_warm_guard_dispatch`,
the cold guard path every Bash-call hook already reaches, now best-effort-nudges a
listener boot via `entry_seam._trigger_listener_boot()`.

AC4 -- the trigger fires on the cold path, from a STAMPED engine root. AC5, the
load-bearing one -- with discovery unreadable (an unstamped root, so the trigger is
a pure no-op) and with `ensure_listener` itself raising, `try_warm_guard_dispatch`
returns its existing "not reached" `WarmGuardOutcome` unchanged: no exception, no
wait, no new failure mode.

Why the trigger is gated on `is_engine_root`, and why that gate is exactly what makes
this suite safe to run un-mocked: `ensure_listener` itself does not consult the stamp
(verified live against this very dev clone -- an unstamped tree still reaches
`spawn_detached`), so without this seam's own gate, EVERY test in
`test_entry_seam.py` that calls `try_warm_guard_dispatch` -- none of which mock
`supervisor.ensure_listener`, because no such call existed before this chunk -- would
trigger a REAL detached process spawn against the operator's own machine on each
test run. This repo's dev clone is unstamped by design (production hook traffic runs
through the klabauter publish clone instead, per this repo's own CLAUDE.md), so the
gate makes `test_entry_seam.py`'s existing, unmocked suite a safe no-op automatically.
This suite exercises the FIRES case explicitly, using a `tmp_path` root stamped via
`skew.write_engine_stamp` -- the same harness convention `test_server_starts_http_
listener.py` (C2) and `test_supervisor.py` already use for exactly this reason.

Spec backlink: docs/plans/2026-08-25-the-http-listener-gets-something-keeping-it-up.md § C3
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.warm import engine_root, skew, supervisor
from coordinator_core.warm.entry_seam import (
    WarmGuardOutcome,
    _trigger_listener_boot,
    try_warm_guard_dispatch,
)


def _stamp(tmp_path: Path) -> None:
    skew.write_engine_stamp(tmp_path, "sha-entry-seam-trigger")


def _patch_try_warm_dispatch(monkeypatch, fn):
    from coordinator_core.warm import client

    monkeypatch.setattr(client, "try_warm_dispatch", fn)


# ---------------------------------------------------------------------------
# AC4 -- the trigger fires, from a stamped root.
# ---------------------------------------------------------------------------


def test_trigger_listener_boot_calls_ensure_listener_from_a_stamped_root(tmp_path, monkeypatch):
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_listener",
        lambda root=None, **kwargs: calls.append(root) or "http://127.0.0.1:1",
    )

    _trigger_listener_boot()

    assert calls == [tmp_path]


def test_try_warm_guard_dispatch_fires_the_trigger_on_the_cold_path(tmp_path, monkeypatch):
    """AC4, exercised through the real caller: the cold guard path every Bash-call
    hook reaches already runs `try_warm_guard_dispatch` -- this asserts the listener
    nudge rides along, regardless of what the warm dispatch itself returns."""
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_listener",
        lambda root=None, **kwargs: calls.append(root) or None,
    )
    _patch_try_warm_dispatch(monkeypatch, lambda msg: None)

    try_warm_guard_dispatch("some.guard.op", {})

    assert calls == [tmp_path]


def test_trigger_listener_boot_is_a_no_op_from_an_unstamped_root(tmp_path, monkeypatch):
    """The dev clone's own protection: an unstamped root (this repo's own state,
    and every `tmp_path` this suite does not explicitly stamp) never reaches
    `ensure_listener` at all -- the gate this module's docstring names."""
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    calls = []
    monkeypatch.setattr(
        supervisor,
        "ensure_listener",
        lambda root=None, **kwargs: calls.append(root) or None,
    )

    _trigger_listener_boot()

    assert calls == []


# ---------------------------------------------------------------------------
# AC5 -- fail-open, unchanged outcome, no exception, no wait.
# ---------------------------------------------------------------------------


def test_try_warm_guard_dispatch_unchanged_when_ensure_listener_raises(tmp_path, monkeypatch):
    """The load-bearing assertion: discovery unreadable AND `ensure_listener` itself
    raising must not change `try_warm_guard_dispatch`'s own result, and must not
    raise past this call -- the existing "not reached" `WarmGuardOutcome` a warm-miss
    already produces is untouched by the trigger riding beside it."""
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    def _boom(root=None, **kwargs):
        raise OSError("discovery file unreadable")

    monkeypatch.setattr(supervisor, "ensure_listener", _boom)
    _patch_try_warm_dispatch(monkeypatch, lambda msg: None)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=False, response=None)


def test_try_warm_guard_dispatch_unchanged_when_ensure_listener_raises_on_a_real_hit(tmp_path, monkeypatch):
    """Same fail-open shape, but on the OTHER branch: a genuine warm hit must reach
    the caller exactly as before even though the trigger beside it blew up."""
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    def _boom(root=None, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(supervisor, "ensure_listener", _boom)

    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"decision": "deny"}}
    _patch_try_warm_dispatch(monkeypatch, lambda msg: envelope)

    outcome = try_warm_guard_dispatch("some.guard.op", {})

    assert outcome == WarmGuardOutcome(hit=True, response=envelope)


def test_trigger_listener_boot_swallows_an_unresolvable_engine_root(monkeypatch):
    """`current_engine_clone` itself raising (an unresolvable clone) is absorbed
    the same as every other failure mode -- never raised past this seam."""

    def _boom():
        raise RuntimeError("cannot resolve engine root")

    monkeypatch.setattr(engine_root, "current_engine_clone", _boom)

    _trigger_listener_boot()  # must not raise


def test_trigger_listener_boot_swallows_an_unimportable_supervisor_module(tmp_path, monkeypatch):
    """`supervisor` itself failing to import is treated identically to any other
    failure mode -- this trigger degrades to a no-op, never a crash."""
    _stamp(tmp_path)
    monkeypatch.setattr(engine_root, "current_engine_clone", lambda: tmp_path)

    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "coordinator_core.warm.supervisor":
            raise ImportError("simulated import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    _trigger_listener_boot()  # must not raise
