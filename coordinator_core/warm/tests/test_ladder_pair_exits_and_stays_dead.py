"""A ladder-scoped warm pair exits once its ladder lease is gone and is not restarted."""

from __future__ import annotations

import json
import os

import pytest

from coordinator_core.warm import breadcrumb, idle, lifecycle, server, supervisor


class _FakeVersionState:
    server_sha = "sha-test"


@pytest.fixture(autouse=True)
def _reset():
    idle.reset_idle_clock_for_test()
    lifecycle.reset_shutdown_guard_for_test()
    yield
    idle.reset_idle_clock_for_test()
    lifecycle.reset_shutdown_guard_for_test()


@pytest.fixture
def ladder(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    root = tmp_path / "e-abc"
    root.mkdir()
    leases = tmp_path / "e-abc.leases"
    leases.mkdir()
    (leases / f"{os.getpid()}-deadbeef0000.json").write_text(
        json.dumps({"pid": os.getpid(), "run_id": "r"}), encoding="utf-8"
    )
    return root, leases


def _remove(leases):
    for child in leases.iterdir():
        child.unlink()
    leases.rmdir()


def test_unscoped_root_has_no_ladder_predicate(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    assert idle.make_ladder_gone(root) is None


def test_boot_with_lease_already_gone_still_yields_predicate(tmp_path):
    root = tmp_path / "e-old"
    root.mkdir()
    (tmp_path / "e-other.leases").mkdir()
    gone = idle.make_ladder_gone(root)
    assert gone is not None
    assert gone() is True


def test_boot_with_sandbox_already_gone_yields_predicate(tmp_path):
    root = tmp_path / "e-vanished"
    gone = idle.make_ladder_gone(root)
    assert gone is not None
    assert gone() is True


def test_ladder_predicate_true_only_after_lease_gone(ladder):
    root, leases = ladder
    gone = idle.make_ladder_gone(root)
    assert gone is not None
    assert gone() is False
    _remove(leases)
    assert gone() is True


def test_lease_of_dead_holder_counts_as_gone(ladder):
    root, leases = ladder
    gone = idle.make_ladder_gone(root)
    for child in leases.iterdir():
        child.unlink()
    (leases / "999999999-aaaa.json").write_text("{}", encoding="utf-8")
    assert gone() is True


def test_should_demote_fires_on_ladder_gone_without_any_deadline():
    assert idle.should_demote(
        served_count=lambda: 5, ladder_gone=lambda: True, idle_deadline_secs=10**9
    )
    assert not idle.should_demote(
        served_count=lambda: 5, ladder_gone=lambda: False, idle_deadline_secs=10**9
    )


def test_server_idle_tick_demotes_and_tombstones_when_ladder_gone(ladder, monkeypatch):
    root, leases = ladder
    calls = []
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kw: calls.append(kw["ladder_gone"]()))
    monkeypatch.setattr(server.push_cadence, "on_idle_tick", lambda **kw: None)
    monkeypatch.setattr(server.telemetry, "record_worker_pool_depth", lambda **kw: None)
    ctx = server._ServerContext(
        name="/run/u/coordinator/warm/hash/tok.sock",
        sid="501",
        version_state=_FakeVersionState(),
        engine_root=root,
    )

    ctx._idle_tick()
    assert calls == [False]
    assert not idle.ladder_gone_tombstoned(root)

    _remove(leases)
    ctx._idle_tick()
    assert calls == [False, True]
    assert idle.ladder_gone_tombstoned(root)


def test_supervisor_tick_stops_and_tombstones_when_ladder_gone(ladder, monkeypatch):
    root, leases = ladder
    ctx = supervisor._ServerContext(
        httpd=None, engine_root=root, version_state=_FakeVersionState()
    )
    stops = []
    monkeypatch.setattr(ctx, "stop", lambda: stops.append(1))
    monkeypatch.setattr(ctx, "_token_is_stale", lambda: False)

    ctx._skew_watchdog_tick()
    assert stops == []

    _remove(leases)
    ctx._skew_watchdog_tick()
    assert stops == [1]
    assert idle.ladder_gone_tombstoned(root)


def test_ensure_listener_does_not_spawn_for_tombstoned_root(ladder, monkeypatch):
    root, _ = ladder
    spawned = []
    monkeypatch.setattr(supervisor, "is_engine_root", lambda r: True)
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **k: spawned.append(a))

    supervisor.ensure_listener(root, now=1.0e9)
    assert len(spawned) == 1

    idle.write_ladder_gone_tombstone(root)
    spawned.clear()
    assert supervisor.ensure_listener(root, now=2.0e9) is None
    assert spawned == []


def test_server_and_supervisor_boot_refuse_for_tombstoned_root(ladder, monkeypatch, capsys):
    root, _ = ladder
    idle.write_ladder_gone_tombstone(root)
    monkeypatch.setattr(server, "_engine_clone_root", lambda: root)
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: root)
    monkeypatch.setattr(supervisor, "engine_root_divergence_message", lambda r: None)
    reached = []

    def boom(*a, **k):
        reached.append(1)
        raise RuntimeError("past the ladder gate")

    monkeypatch.setattr(server.skew, "compute_client_token", boom)
    monkeypatch.setattr(supervisor, "_elect_supervisor_slot", boom)

    assert server._run_guarded() == 0
    assert supervisor.main() == 0
    assert reached == []
    assert capsys.readouterr().err.count("ladder gone") == 2


def test_client_spawn_once_does_not_respawn_for_tombstoned_root(ladder, monkeypatch):
    from coordinator_core.warm import client

    root, _ = ladder
    spawned = []
    monkeypatch.setattr(client, "_engine_clone_root", lambda: root)
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(breadcrumb, "should_spawn", lambda *a, **k: True)
    monkeypatch.setattr(client, "spawn_detached", lambda *a, **k: spawned.append(a))

    idle.write_ladder_gone_tombstone(root)
    client._spawn_once()
    assert spawned == []
