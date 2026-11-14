"""The warm idle tick calls the git-maintenance trigger with the served repos, and a failure in it
never escapes the tick."""

from __future__ import annotations

import pytest

from coordinator_core.git import maintenance_tick
from coordinator_core.warm import breadcrumb, idle, lifecycle, server


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return self.is_skewed(client_token)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "served"
    path.mkdir()
    return path


@pytest.fixture
def ctx(tmp_path, repo, monkeypatch):
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kwargs: None)
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda root=None: tmp_path / "svc")
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: [repo])
    yield server._ServerContext(
        name="pipe-maint", sid="sid-maint", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


def test_idle_tick_hands_served_repos_to_the_maintenance_trigger(ctx, repo, monkeypatch):
    seen: list = []
    monkeypatch.setattr(maintenance_tick, "on_idle_tick", lambda repos: seen.append(list(repos)) or False)
    ctx._idle_tick()
    assert seen == [[repo]]


def test_a_launch_counts_as_engine_work(ctx, monkeypatch):
    marked: list = []
    monkeypatch.setattr(maintenance_tick, "on_idle_tick", lambda repos: True)
    monkeypatch.setattr(idle, "mark_invocation", lambda: marked.append(1))
    ctx._idle_tick()
    assert marked


def test_trigger_failure_never_escapes_the_tick(ctx, monkeypatch):
    def boom(repos):
        raise RuntimeError("maintenance broke")

    monkeypatch.setattr(maintenance_tick, "on_idle_tick", boom)
    ctx._idle_tick()
