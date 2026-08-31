"""Two callers, two homes, one warm server -- each op resolves ITS OWN home.

Spec backlink: docs/plans/2026-08-31-the-settings-home-crosses-the-warm-boundary.md § C2

WHAT THIS PROVES, and what it does not. `test_settings_home_mismatch_refusal.py`
covers the REFUSAL half -- an unisolated dispatch that cannot honour a mismatched
claim says so instead of answering wrong. This file covers the other half the
exit criterion actually asks for: the process-ISOLATED leg
(`_pool_dispatch_worker`, `isolated=True`) never needs to refuse at all, because
`entry_seam.per_request_state`'s `settings_home` axis mirrors the caller's claim
into `os.environ` for the life of that one call -- so `coordinator_core.
_settings_home.settings_home()`, read from INSIDE the dispatched op, resolves
the CALLER's home, not the home whoever spawned this server happened to have.

Two requests, served in SEQUENCE (this module's own transport model is one
thread/one call per connection; sequencing here stands in for that without
needing a live pipe) through the same code path a pool worker process would run,
each carrying a different claimed home. Proves both that each sees its own home
and that neither leaks into the other -- the second assertion is the one a naive
"just set `os.environ` and never unset it" fix would fail.
"""

from __future__ import annotations

import os

from dataclasses import replace

from coordinator_core.warm import server
from coordinator_core.warm.caller_context import resolve_caller_context


async def _echo_settings_home(msg, *, caller: str | None = None) -> dict:
    from coordinator_core._settings_home import settings_home

    return {"jsonrpc": "2.0", "id": msg.get("id"), "result": str(settings_home())}


def _caller_for(home) -> "server.CallerContext":
    return replace(resolve_caller_context(), settings_home=home)


def test_two_requests_each_resolve_their_own_claimed_home(monkeypatch, tmp_path):
    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _echo_settings_home)

    home_a = str(tmp_path / "home-a")
    home_b = str(tmp_path / "home-b")

    result_a = server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}, _caller_for(home_a)
    )
    result_b = server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}, _caller_for(home_b)
    )

    assert result_a["result"] == home_a
    assert result_b["result"] == home_b


def test_a_later_request_cannot_observe_an_earlier_ones_home(monkeypatch, tmp_path):
    """Leakage leg. A caller who names NO home at all -- the ordinary case --
    must resolve the server's own ambient home, never a prior request's
    borrowed one left lingering in `os.environ` after that request's scope
    should already have closed."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    from coordinator_core._settings_home import settings_home as _resolve_settings_home

    ambient_home = str(_resolve_settings_home())

    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _echo_settings_home)

    home_a = str(tmp_path / "home-a")
    server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}, _caller_for(home_a)
    )

    result_none = server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}, _caller_for(None)
    )

    assert result_none["result"] == ambient_home


def test_no_caller_at_all_is_unaffected(monkeypatch, tmp_path):
    """The pre-C2 no-identity path (`caller=None`, e.g. an older in-process
    caller of `_pool_dispatch_worker`) must still resolve the server's own
    ambient home -- adding the sixth axis must not force every caller to
    supply one."""
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    from coordinator_core._settings_home import settings_home as _resolve_settings_home

    ambient_home = str(_resolve_settings_home())
    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _echo_settings_home)

    result = server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}, None
    )

    assert result["result"] == ambient_home


# ---------------------------------------------------------------------------
# VERIFY-AT-ENTRY (plan § C1's own contract, landing in C2's file because the
# machinery is `server.py`'s). The borrow's `finally` restore covers every
# reader that lives and dies inside the task; it does not cover one that
# OUTLIVES the task boundary, nor a restore that never runs at all. A pooled
# worker is REUSED, so a leaked home is inherited by whichever caller lands on
# it next -- silently, in the direction that disarms guards.
# ---------------------------------------------------------------------------


def test_a_leaked_home_is_repaired_before_the_next_request_binds(monkeypatch, tmp_path):
    """A worker whose previous task leaked a borrowed home -- a skipped
    restore, or a background reader that outlived the block -- serves the NEXT
    no-claim caller against its own pristine home, not the leaked one."""
    pristine = str(tmp_path / "pristine-home")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", pristine)
    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _echo_settings_home)

    monkeypatch.setattr(server, "_worker_pristine_settings_home", pristine)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "leaked-home"))

    result = server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 10, "method": "ping", "params": {}}, _caller_for(None)
    )

    assert result["result"] == pristine


def test_a_worker_whose_spawner_set_no_home_repairs_by_unsetting(monkeypatch, tmp_path):
    """`None` captured at process start means "the spawner had none set", and
    repair must POP rather than write an empty string -- an empty value is not
    the same disposition as an absent one to `settings_home()`'s own ladder."""
    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _echo_settings_home)
    monkeypatch.setattr(server, "_worker_pristine_settings_home", None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "leaked-home"))

    server._repair_settings_home_to_pristine()

    assert "COORDINATOR_SETTINGS_HOME" not in os.environ


def test_repair_is_inert_in_a_process_that_never_captured(monkeypatch, tmp_path):
    """The accept process and every direct importer never run
    `_worker_process_init`, so no pristine disposition is known and repair must
    not fire -- an uncaptured sentinel is not "the spawner had none set"."""
    ambient = str(tmp_path / "ambient-home")
    monkeypatch.setattr(
        server, "_worker_pristine_settings_home", server._PRISTINE_HOME_UNCAPTURED
    )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", ambient)

    server._repair_settings_home_to_pristine()

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == ambient


def test_worker_process_init_captures_the_pristine_disposition(monkeypatch, tmp_path):
    """The capture leg itself: `_worker_process_init` records the disposition
    it found, which is what every later repair is measured against."""
    captured = str(tmp_path / "spawner-home")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", captured)
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: None)
    monkeypatch.setattr(server, "_preload_op_registry", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", lambda **kw: _NoopThread())

    # `_worker_process_init` assigns the module global directly, so
    # `monkeypatch.setattr` cannot unwind it -- restore it by hand or every
    # later test in this process inherits this one's captured disposition.
    before = server._worker_pristine_settings_home
    try:
        server._worker_process_init()
        assert server._worker_pristine_settings_home == captured
    finally:
        server._worker_pristine_settings_home = before


class _NoopThread:
    def start(self) -> None:
        pass
