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
