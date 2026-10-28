"""A ladder copy whose lease is already gone at boot still yields the exit predicate."""

from __future__ import annotations

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
def leaked_copy(tmp_path, monkeypatch):
    """The observed shape: a copy with `.lifecycle.lock` beside it, no `.leases`."""
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    engines = tmp_path / "engines"
    engines.mkdir()
    (engines / ".lifecycle.lock").write_text("", encoding="utf-8")
    root = engines / "e-6598de473c50ab51"
    root.mkdir()
    return root


def test_lease_gone_with_only_lifecycle_lock_beside_copy_is_ladder_scoped(tmp_path):
    root = tmp_path / "e-6598de473c50ab51"
    root.mkdir()
    (tmp_path / ".lifecycle.lock").write_text("", encoding="utf-8")
    gone = idle.make_ladder_gone(root)
    assert gone is not None
    assert gone() is True


def test_ordinary_clone_still_has_no_predicate(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    (tmp_path / "unrelated.txt").write_text("x", encoding="utf-8")
    assert idle.make_ladder_gone(root) is None


def test_ordinary_clone_beside_sibling_leases_or_lock_is_not_retired(tmp_path):
    root = tmp_path / "myclone"
    root.mkdir()
    (tmp_path / "e-6598de473c50ab51.leases").mkdir()
    (tmp_path / ".lifecycle.lock").write_text("", encoding="utf-8")
    assert idle.make_ladder_gone(root) is None


def test_lifecycle_lock_matched_by_exact_name_only(tmp_path):
    root = tmp_path / "e-6598de473c50ab51"
    root.mkdir()
    (tmp_path / "x.lifecycle.lock").write_text("", encoding="utf-8")
    (tmp_path / ".lifecycle.lock.bak").write_text("", encoding="utf-8")
    assert idle.make_ladder_gone(root) is None


def test_server_boots_tombstones_and_demotes_leaked_copy(leaked_copy, monkeypatch):
    root = leaked_copy
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

    assert calls == [True]
    assert idle.ladder_gone_tombstoned(root)


def test_supervisor_stops_leaked_copy_and_is_not_respawned(leaked_copy, monkeypatch):
    root = leaked_copy
    ctx = supervisor._ServerContext(
        httpd=None, engine_root=root, version_state=_FakeVersionState()
    )
    stops = []
    monkeypatch.setattr(ctx, "stop", lambda: stops.append(1))
    monkeypatch.setattr(ctx, "_token_is_stale", lambda: False)

    ctx._skew_watchdog_tick()
    assert stops == [1]
    assert idle.ladder_gone_tombstoned(root)

    spawned = []
    monkeypatch.setattr(supervisor, "is_engine_root", lambda r: True)
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **k: spawned.append(a))
    assert supervisor.ensure_listener(root, now=2.0e9) is None
    assert spawned == []
