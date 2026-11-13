"""The warm idle tick runs atlas catch-up per served repo, throttled, and never fails on it."""

from __future__ import annotations

import pytest

from coordinator_core.cartography import atlas_catchup
from coordinator_core.warm import idle, lifecycle, server


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kwargs: None)
    monkeypatch.setattr(atlas_catchup, "_next_tick", {})
    repo = tmp_path / "served"
    repo.mkdir()
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: [repo])
    yield server._ServerContext(
        name="pipe-catchup", sid="sid-catchup", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


def _ran(ran: bool) -> atlas_catchup.CatchUpResult:
    return atlas_catchup.CatchUpResult(ran=ran, spawns=0, facts=0, pending=0, reason="t")


def test_two_ticks_within_interval_run_catch_up_once(ctx, monkeypatch):
    calls: list = []
    monkeypatch.setattr(atlas_catchup, "catch_up", lambda repo: calls.append(repo) or _ran(True))
    ctx._idle_tick()
    ctx._idle_tick()
    assert len(calls) == 1


def test_raising_catch_up_leaves_tick_intact(ctx, monkeypatch):
    def boom(repo):
        raise RuntimeError("catch-up broke")

    monkeypatch.setattr(atlas_catchup, "catch_up", boom)
    ctx._idle_tick()


def test_import_failure_leaves_tick_intact(ctx, monkeypatch):
    def boom(repos):
        raise RuntimeError("tick broke")

    monkeypatch.setattr(atlas_catchup, "on_idle_tick", boom)
    ctx._idle_tick()
