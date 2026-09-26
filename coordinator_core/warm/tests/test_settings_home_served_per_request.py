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


async def _echo_settings_home(msg, *, caller: str | None = None, corr_id: str | None = None) -> dict:
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
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    from coordinator_core._settings_home import settings_home as _resolve_settings_home

    ambient_home = str(_resolve_settings_home())
    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _echo_settings_home)

    result = server._pool_dispatch_worker(
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}, None
    )

    assert result["result"] == ambient_home


# VERIFY-AT-ENTRY (plan § C1's own contract, landing in C2's file because the
# OUTLIVES the task boundary, nor a restore that never runs at all. A pooled


def test_a_leaked_home_is_repaired_before_the_next_request_binds(monkeypatch, tmp_path):
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
    monkeypatch.setattr("coordinator_core.ipc.dispatch_message", _echo_settings_home)
    monkeypatch.setattr(server, "_worker_pristine_settings_home", None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "leaked-home"))

    server._repair_settings_home_to_pristine()

    assert "COORDINATOR_SETTINGS_HOME" not in os.environ


def test_repair_is_inert_in_a_process_that_never_captured(monkeypatch, tmp_path):
    ambient = str(tmp_path / "ambient-home")
    monkeypatch.setattr(
        server, "_worker_pristine_settings_home", server._PRISTINE_HOME_UNCAPTURED
    )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", ambient)

    server._repair_settings_home_to_pristine()

    assert os.environ["COORDINATOR_SETTINGS_HOME"] == ambient


def test_worker_process_init_captures_the_pristine_disposition(monkeypatch, tmp_path):
    captured = str(tmp_path / "spawner-home")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", captured)
    monkeypatch.setattr(server, "_bind_null_std_streams", lambda: None)
    monkeypatch.setattr(server, "_preload_op_registry", lambda: None)
    monkeypatch.setattr(server.threading, "Thread", lambda **kw: _NoopThread())

    before = server._worker_pristine_settings_home
    try:
        server._worker_process_init()
        assert server._worker_pristine_settings_home == captured
    finally:
        server._worker_pristine_settings_home = before


class _NoopThread:
    def start(self) -> None:
        pass
