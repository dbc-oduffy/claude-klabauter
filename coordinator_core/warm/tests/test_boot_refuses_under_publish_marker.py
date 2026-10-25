"""server/supervisor boot returns 0 before election while the publish marker is held."""

from __future__ import annotations

import pytest

from coordinator_core.warm import breadcrumb, front_door, server, supervisor


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    eng = tmp_path / "engine"
    eng.mkdir()
    return eng


def test_server_boot_refuses_while_held_and_proceeds_after(root, monkeypatch, capsys):
    monkeypatch.setattr(server, "_engine_clone_root", lambda: root)
    reached = []

    def stop_here(*a, **k):
        reached.append(1)
        raise RuntimeError("past the marker gate")

    monkeypatch.setattr(server.skew, "compute_client_token", stop_here)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert server._run_guarded() == 0
    assert reached == []
    assert "publish in progress" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="past the marker gate"):
        server._run_guarded()
    assert reached == [1]


def test_supervisor_boot_refuses_while_held_and_proceeds_after(root, monkeypatch, capsys):
    monkeypatch.setattr(supervisor, "_default_engine_clone", lambda: root)
    monkeypatch.setattr(supervisor, "engine_root_divergence_message", lambda r: None)
    reached = []

    def stop_here(r):
        reached.append(1)
        raise RuntimeError("past the marker gate")

    monkeypatch.setattr(supervisor, "_elect_supervisor_slot", stop_here)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert supervisor.main() == 0
    assert reached == []
    assert "publish in progress" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="past the marker gate"):
        supervisor.main()
    assert reached == [1]


def test_front_door_boot_refuses_while_held_and_proceeds_after(root, monkeypatch, capsys):
    monkeypatch.setattr(front_door, "current_engine_clone", lambda: root)
    reached = []

    def stop_here(*a, **k):
        reached.append(1)
        raise RuntimeError("past the marker gate")

    monkeypatch.setattr(front_door, "elect_front_door", stop_here)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert front_door.main() == 0
    assert reached == []
    assert "publish in progress" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="past the marker gate"):
        front_door.main()
    assert reached == [1]
