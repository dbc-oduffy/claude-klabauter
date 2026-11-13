"""The warm idle tick hands served repos to distill autofire and marks an invocation on a True return."""

from __future__ import annotations

import sys
import types

import pytest

from coordinator_core.warm import idle, lifecycle, server


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return self.is_skewed(client_token)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kwargs: None)
    yield server._ServerContext(
        name="pipe-autofire", sid="sid-autofire", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


def _install_fake(monkeypatch, fn):
    fake = types.ModuleType("coordinator_core.distill.autofire")
    fake.on_idle_tick = fn
    monkeypatch.setitem(sys.modules, "coordinator_core.distill.autofire", fake)


def _run_tick(ctx, monkeypatch):
    marks: list[int] = []
    monkeypatch.setattr(idle, "mark_invocation", lambda *a, **k: marks.append(1))
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: ["/r1", "/r2"])
    ctx._idle_tick()
    return marks


def test_true_return_marks_invocation_and_receives_served_repos(ctx, monkeypatch):
    seen: list = []
    _install_fake(monkeypatch, lambda repos: seen.append(repos) or True)
    marks = _run_tick(ctx, monkeypatch)
    assert seen == [["/r1", "/r2"]]
    assert marks


def test_false_return_marks_nothing_from_autofire(ctx, monkeypatch):
    seen: list = []
    _install_fake(monkeypatch, lambda repos: seen.append(repos) or False)
    assert _run_tick(ctx, monkeypatch) == []
    assert seen


def test_autofire_exception_does_not_escape_tick(ctx, monkeypatch):
    def boom(repos):
        raise RuntimeError("autofire broke")

    _install_fake(monkeypatch, boom)
    assert _run_tick(ctx, monkeypatch) == []
