"""Agy external_registry, harness_registry, and agent_role parity pins."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.session import agent_role, core, external_registry as registry, harness_registry

SID_AGY = "5f30ae34-9887-4080-8d72-e61aaac0cedb"
SID_CODEX = "00000000-0000-4000-8000-0000000000bb"


def proc(name: str, cmdline=(), pid: int = 234) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, name=lambda: name, cmdline=lambda: list(cmdline), create_time=lambda: 789)


@pytest.mark.parametrize("name", ["agy", "agy.exe", "agy-daemon", "agy_daemon", "antigravity-ls"])
def test_agy_host_shapes_qualify(name: str) -> None:
    assert registry._is_agy_host(proc(name))


@pytest.mark.parametrize("name", ["codex", "node", "python3", "zsh", "ragy"])
def test_non_agy_processes_are_refused(name: str) -> None:
    assert not registry._is_agy_host(proc(name))


def test_find_agy_host_prefers_cli_over_daemon() -> None:
    daemon = proc("antigravity-ls", ["antigravity-ls"], pid=10)
    cli = proc("agy", ["agy"], pid=20)
    assert registry._find_agy_host([proc("zsh"), daemon, cli]) is cli
    assert registry._find_agy_host([proc("zsh"), daemon]) is daemon


@pytest.mark.parametrize("argv,expected", [
    (["agy"], "em"),
    (["agy", "chat"], "em"),
    (["agy-daemon"], "em"),
    (["agy", "-p", "do work"], "unknown"),
    (["agy", "--print", "do work"], "unknown"),
    (["agy", "--print=json", "do work"], "unknown"),
    (["agy", "plugin", "list"], "unknown"),
    (["agy", "--help"], "unknown"),
    ([], "unknown"),
])
def test_observed_agy_audience_classifies_interactive_vs_headless(argv, expected) -> None:
    assert agent_role.observed_agy_audience(argv) == expected
    payload = {"hook_event_name": "SessionStart", "source": "startup", "session_id": SID_AGY}
    assert agent_role.classify(payload, {}, **agent_role.observed_agy_launch(argv)) == expected


def test_agy_worker_env_and_subagent_override_interactive_em() -> None:
    payload = {"hook_event_name": "SessionStart", "source": "startup", "session_id": SID_AGY}
    launch = agent_role.observed_agy_launch(["agy"])
    assert agent_role.classify(payload, {"COORDINATOR_AGENT_WORKER": "1"}, **launch) == "worker"
    assert agent_role.classify(payload, {"COORDINATOR_AGENT_ROLE": "worker"}, **launch) == "worker"
    assert agent_role.classify({**payload, "agent_id": "sub-1"}, {}, **launch) == "worker"


def test_agy_register_lookup_snapshot_harness_and_unregister(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINATOR_AGENT_WORKER", raising=False)
    monkeypatch.delenv("COORDINATOR_AGENT_ROLE", raising=False)
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(core, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(harness_registry, "registry_dir", lambda: None)

    cwd = tmp_path / "coordinator-content-repo"
    cwd.mkdir()

    # Register Agy with router-provided name
    agy_parents = [proc("zsh"), proc("agy", ["agy"], pid=301)]
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(parents=lambda: agy_parents))
    agy_name = "agy-coordinator-content-repo-f75cc21303-on-machine-b"
    agy_rec = registry.register_current({
        "session_id": SID_AGY,
        "cwd": str(cwd),
        "provider": "agy",
        "name": agy_name,
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    assert agy_rec["provider"] == "agy"
    assert agy_rec["role"] == "em"
    assert agy_rec["name"] == agy_name
    assert agy_rec["address_version"] == 1
    assert (tmp_path / "host-sessions" / "agy" / f"{SID_AGY}.json").is_file()

    # Register Codex beside it
    codex_parents = [proc("zsh"), proc("codex", ["codex"], pid=302)]
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(parents=lambda: codex_parents))
    codex_rec = registry.register_current({
        "session_id": SID_CODEX,
        "cwd": str(cwd),
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    assert codex_rec["provider"] == "codex"

    # Multi-provider lookup + snapshot + harness_registry + role_for_caller
    assert registry.lookup(SID_AGY)["name"] == agy_name
    assert registry.lookup(SID_CODEX)["provider"] == "codex"
    snap = registry.snapshot()
    assert set(snap) == {SID_AGY, SID_CODEX}
    assert agent_role.role_for_caller({}, SID_AGY) == "em"

    hsnap = harness_registry.snapshot()
    assert hsnap[SID_AGY].provider == "agy"
    assert hsnap[SID_AGY].name == agy_name
    assert hsnap[SID_CODEX].provider == "codex"

    # Unregister marks Agy ended without touching Codex
    registry.unregister(SID_AGY)
    assert registry.is_ended(SID_AGY) is True
    assert registry.lookup(SID_AGY) is None
    assert registry.lookup(SID_CODEX) is not None


def test_agy_without_preferred_name_allocates_sequence_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(core, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(parents=lambda: [proc("agy", ["agy"], pid=401)]))
    cwd = tmp_path / "claude-klabauter"
    cwd.mkdir()
    rec = registry.register_current({
        "conversationId": SID_AGY,
        "cwd": str(cwd),
        "provider": "agy",
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    assert re.fullmatch(r"claude-klabauter-agy-[a-z0-9-]+-[1-9][0-9]*", rec["name"])


def test_agy_register_without_agy_ancestor_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(parents=lambda: [proc("zsh"), proc("codex")]))
    cwd = tmp_path / "repo"
    cwd.mkdir()
    with pytest.raises(ValueError, match="no observed Agy ancestor"):
        registry.register_current({"session_id": SID_AGY, "cwd": str(cwd), "provider": "agy"}, tmp_path)
