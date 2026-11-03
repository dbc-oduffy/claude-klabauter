"""Boot gates refuse every non-holder boot under a held publish marker; the holder's own successor boots."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.session.core import _win_create_time_epoch
from coordinator_core.warm import breadcrumb, front_door, server, supervisor

PAST_GATE = "past the marker gate"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    monkeypatch.delenv(breadcrumb.PUBLISH_HOLDER_TOKEN_ENV, raising=False)
    eng = tmp_path / "engine"
    eng.mkdir()
    return eng


def _foreign_marker(root, token="tok-held"):
    """A live marker held by another process (this test's parent), as a peer's publish writes it."""
    pid = os.getppid()
    epoch = _win_create_time_epoch(pid)
    assert epoch, "parent process identity unavailable"
    path = breadcrumb.publish_marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "stable_pid_start_epoch": epoch,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "holder": "peer",
                "holder_token": token,
            }
        ),
        encoding="utf-8",
    )
    assert breadcrumb.publish_marker_held(breadcrumb.svc_dir(root))
    return path


def _arm_server(root, monkeypatch):
    monkeypatch.setattr(server, "_engine_clone_root", lambda: root)
    reached = []

    def stop_here(*a, **k):
        reached.append(1)
        raise RuntimeError(PAST_GATE)

    monkeypatch.setattr(server.skew, "compute_client_token", stop_here)
    return reached, server._run_guarded


def _arm_supervisor(root, monkeypatch):
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: root)
    monkeypatch.setattr(supervisor, "engine_root_divergence_message", lambda r: None)
    reached = []

    def stop_here(r):
        reached.append(1)
        raise RuntimeError(PAST_GATE)

    monkeypatch.setattr(supervisor, "_elect_supervisor_slot", stop_here)
    return reached, supervisor.main


def _arm_front_door(root, monkeypatch):
    monkeypatch.setattr(front_door, "current_engine_clone", lambda: root)
    reached = []

    def stop_here(*a, **k):
        reached.append(1)
        raise RuntimeError(PAST_GATE)

    monkeypatch.setattr(front_door, "elect_front_door", stop_here)
    return reached, front_door.main


GATES = pytest.mark.parametrize("arm", [_arm_server, _arm_supervisor, _arm_front_door], ids=["server", "supervisor", "front_door"])


@GATES
def test_boot_refuses_while_held_and_proceeds_after(arm, root, monkeypatch, capsys):
    reached, boot = arm(root, monkeypatch)
    path = _foreign_marker(root)
    assert boot() == 0
    assert reached == []
    assert "publish in progress" in capsys.readouterr().err
    path.unlink()
    with pytest.raises(RuntimeError, match=PAST_GATE):
        boot()
    assert reached == [1]


@GATES
def test_boot_refuses_a_wrong_or_missing_holder_token(arm, root, monkeypatch):
    reached, boot = arm(root, monkeypatch)
    _foreign_marker(root, token="tok-held")
    monkeypatch.setenv(breadcrumb.PUBLISH_HOLDER_TOKEN_ENV, "tok-of-an-earlier-hold")
    assert boot() == 0
    assert reached == []


@GATES
def test_boot_carrying_the_holders_token_proceeds_under_the_marker(arm, root, monkeypatch):
    reached, boot = arm(root, monkeypatch)
    _foreign_marker(root, token="tok-held")
    monkeypatch.setenv(breadcrumb.PUBLISH_HOLDER_TOKEN_ENV, "tok-held")
    with pytest.raises(RuntimeError, match=PAST_GATE):
        boot()
    assert reached == [1]


@GATES
def test_the_holder_proceeds_only_inside_its_successor_spawn_scope(arm, root, monkeypatch):
    reached, boot = arm(root, monkeypatch)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert boot() == 0
        assert reached == []
        with breadcrumb.holder_successor_spawn():
            with pytest.raises(RuntimeError, match=PAST_GATE):
                boot()
        assert boot() == 0
    assert reached == [1]


def test_holder_env_is_empty_for_a_non_holder_and_carries_the_token_for_the_holder(root):
    assert breadcrumb.publish_holder_env(root) == {}
    _foreign_marker(root)
    with breadcrumb.holder_successor_spawn():
        assert breadcrumb.publish_holder_env(root) == {}
    breadcrumb.publish_marker_path(root).unlink()
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert breadcrumb.publish_holder_env(root) == {}
        with breadcrumb.holder_successor_spawn():
            env = breadcrumb.publish_holder_env(root)
            assert list(env) == [breadcrumb.PUBLISH_HOLDER_TOKEN_ENV] and env[breadcrumb.PUBLISH_HOLDER_TOKEN_ENV]
    assert breadcrumb.publish_holder_env(root) == {}
