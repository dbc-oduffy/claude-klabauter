"""A superseded forwarder resolves itself: retired and respawned once identity is
confirmed (argv, or the record's pid owning the forwarder port AND answering
`/health` as the forwarder holder), else refused
with text naming `coordinator/bin/forwarder-restart.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import sessionstart_ensure_http_forwarder as mod

_PID = 39864


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    forwarder = tmp_path / "http_hook_forwarder.py"
    forwarder.write_text("# fixture\n", encoding="utf-8")
    calls = {"retired": [], "spawned": 0, "forwarder": forwarder}
    monkeypatch.setattr(mod, "_module_fingerprint_on_disk", lambda p: "ondisk0000000000")
    monkeypatch.setattr(
        mod, "_running_forwarder_record", lambda: {"pid": _PID, "module_fingerprint": "running000000000"}
    )
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_posix", lambda pid: None)
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_windows", lambda pid: None)
    monkeypatch.setattr(mod, "_pid_listens_on", lambda pid, port=mod._FIXED_PORT: False)  # never the live socket table
    monkeypatch.setattr(mod, "_retire_stale_forwarder", lambda pid: calls["retired"].append(pid) or True)
    monkeypatch.setattr(
        mod, "_spawn_forwarder_detached", lambda p: calls.__setitem__("spawned", calls["spawned"] + 1) or True
    )
    monkeypatch.setattr(mod, "_await_successor_bind", lambda: True)
    monkeypatch.setattr(mod, "_probe_bind_wins", lambda *a, **kw: False)
    monkeypatch.setattr(mod, "_forwarder_module_path", lambda: forwarder)
    return calls


def test_unreadable_argv_with_port_owner_match_retires_and_respawns(
    seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: _PID)
    monkeypatch.setattr(mod, "_port_answers_as_forwarder", lambda pid, port=0: True)
    assert mod._ensure_current_forwarder(seams["forwarder"]) is None
    assert seams["retired"] == [_PID]
    assert seams["spawned"] == 1


def test_non_forwarder_holding_the_port_is_never_retired(
    seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: _PID)
    monkeypatch.setattr(mod, "_port_answers_as_forwarder", lambda pid, port=0: False)
    reason = mod._ensure_current_forwarder(seams["forwarder"])
    assert reason is not None and "forwarder-restart.py" in reason
    assert seams["retired"] == []
    assert seams["spawned"] == 0


@pytest.mark.parametrize("owner", [None, 111])
def test_unconfirmed_identity_refuses_naming_the_restart_script(
    seams: dict, monkeypatch: pytest.MonkeyPatch, owner: "int | None"
) -> None:
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: owner)
    reason = mod._ensure_current_forwarder(seams["forwarder"])
    assert reason is not None
    assert "forwarder-restart.py" in reason
    assert "-- restart it" not in reason
    assert seams["retired"] == []
    assert seams["spawned"] == 0


def test_argv_proven_not_a_forwarder_never_falls_back_to_the_port_owner(
    seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_posix", lambda pid: False)
    monkeypatch.setattr(mod, "_pid_is_a_forwarder_windows", lambda pid: False)
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: _PID)
    reason = mod._ensure_current_forwarder(seams["forwarder"])
    assert reason is not None and "forwarder-restart.py" in reason
    assert seams["retired"] == []


def test_terminate_failure_also_names_the_restart_script(
    seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: _PID)
    monkeypatch.setattr(mod, "_port_answers_as_forwarder", lambda pid, port=0: True)
    monkeypatch.setattr(mod, "_retire_stale_forwarder", lambda pid: False)
    reason = mod._ensure_current_forwarder(seams["forwarder"])
    assert reason is not None and "forwarder-restart.py" in reason


def test_session_start_handler_resolves_the_superseded_forwarder(
    seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: _PID)
    monkeypatch.setattr(mod, "_port_answers_as_forwarder", lambda pid, port=0: True)
    out = mod._handler({})
    assert "HTTP hook forwarder not ensured" not in str(out)
    assert seams["retired"] == [_PID]


def test_session_start_handler_discloses_the_runnable_when_unconfirmed(
    seams: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod, "_port_owner_pid", lambda port=mod._FIXED_PORT: None)
    out = mod._handler({})
    assert "forwarder-restart.py" in str(out)
    assert seams["retired"] == []
