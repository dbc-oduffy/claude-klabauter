"""hooks.sweep_boot's index-resync drain leg: called once, exception-isolated,
env-gated, and spawn-free when no pending record exists."""

from __future__ import annotations

import asyncio

from coordinator_core.hooks import sweep_boot


def _neutralise_other_legs(monkeypatch, root):
    monkeypatch.setenv("COORDINATOR_FORWARDER_SELFHEAL_OFF", "1")
    monkeypatch.setenv("COORDINATOR_ORIENTATION_SELFHEAL_OFF", "1")
    monkeypatch.setenv("COORDINATOR_SESSION_REAP_OFF", "1")
    monkeypatch.delenv("COORDINATOR_INDEX_RESYNC_DRAIN_OFF", raising=False)
    monkeypatch.setattr(sweep_boot, "_resolve_this_repo_root", lambda: str(root))


def test_leg_called_once_per_handler_run(monkeypatch, tmp_path):
    _neutralise_other_legs(monkeypatch, tmp_path)
    calls = []

    async def fake(repo_root):
        calls.append(repo_root)

    monkeypatch.setattr(sweep_boot, "_drain_index_resyncs", fake)
    asyncio.run(sweep_boot._handler({}))
    assert calls == [str(tmp_path)]


def test_raising_drain_is_isolated_and_handler_returns_no_advisory(monkeypatch, tmp_path):
    _neutralise_other_legs(monkeypatch, tmp_path)
    import coordinator_core.ops.fleet._index_resync_drain as drain

    async def boom(*_a, **_k):
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(drain, "drain_pending_resyncs", boom)
    result = asyncio.run(sweep_boot._handler({}))
    assert result == sweep_boot.no_advisory()


def test_off_switch_skips_drain(monkeypatch, tmp_path):
    import coordinator_core.ops.fleet._index_resync_drain as drain

    called = []

    async def spy(*_a, **_k):
        called.append(1)

    monkeypatch.setattr(drain, "drain_pending_resyncs", spy)
    monkeypatch.setenv("COORDINATOR_INDEX_RESYNC_DRAIN_OFF", "1")
    asyncio.run(sweep_boot._drain_index_resyncs(str(tmp_path)))
    assert called == []


def test_no_repo_root_skips_drain(monkeypatch):
    import coordinator_core.ops.fleet._index_resync_drain as drain

    called = []

    async def spy(*_a, **_k):
        called.append(1)

    monkeypatch.setattr(drain, "drain_pending_resyncs", spy)
    monkeypatch.delenv("COORDINATOR_INDEX_RESYNC_DRAIN_OFF", raising=False)
    asyncio.run(sweep_boot._drain_index_resyncs(None))
    assert called == []


def test_no_records_spawns_nothing(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("COORDINATOR_INDEX_RESYNC_DRAIN_OFF", raising=False)
    spawns = []

    async def counting_spawn(*a, **k):
        spawns.append(a)
        raise AssertionError("spawn with no pending records")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_spawn)
    asyncio.run(sweep_boot._drain_index_resyncs(str(tmp_path)))
    assert spawns == []
