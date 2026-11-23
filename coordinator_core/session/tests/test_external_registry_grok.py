"""Tests for Grok external_registry + agent_role parity."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.session import agent_role, core
from coordinator_core.session import external_registry as registry
from coordinator_core.session import harness_registry

SID_GROK = "01a205b1-1111-4222-8333-444455556666"


def _proc(name: str, cmdline: list[str] | None = None, pid: int = 401, create_time: float = 1700000000.0):
    return SimpleNamespace(
        pid=pid,
        name=lambda: name,
        cmdline=lambda: cmdline if cmdline is not None else [name],
        create_time=lambda: create_time,
    )


def test_observed_grok_audience_distinguishes_interactive_vs_headless():
    assert agent_role.observed_grok_audience(["grok"]) == "em"
    assert agent_role.observed_grok_audience(["grok", "--model", "grok-4"]) == "em"
    assert agent_role.observed_grok_audience(["grok", "-p", "hello"]) == "unknown"
    assert agent_role.observed_grok_audience(["grok", "--print", "hello"]) == "unknown"
    assert agent_role.observed_grok_audience(["grok", "login"]) == "unknown"
    assert agent_role.observed_grok_audience([]) == "unknown"


def test_grok_register_lookup_snapshot_and_unregister(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINATOR_AGENT_WORKER", raising=False)
    monkeypatch.delenv("COORDINATOR_AGENT_ROLE", raising=False)
    monkeypatch.setattr(core, "stable_pid_alive", lambda *a, **k: True)
    monkeypatch.setattr(core, "ensure_session", lambda *a, **k: None)
    monkeypatch.setattr(harness_registry, "registry_dir", lambda: None)

    cwd = tmp_path / "claude-klabauter"
    cwd.mkdir()

    grok_parents = [_proc("zsh", pid=400), _proc("grok", ["grok"], pid=401)]
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(
        parents=lambda: grok_parents,
        create_time=lambda: 1700000000.0,
        is_running=lambda: True,
    ))
    grok_name = "grok-claude-klabauter-01a205b1-on-mac-mini"
    rec = registry.register_current({
        "session_id": SID_GROK,
        "cwd": str(cwd),
        "provider": "grok",
        "name": grok_name,
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    assert rec["provider"] == "grok"
    assert rec["role"] == "em"
    assert rec["name"] == grok_name
    assert rec["address_version"] == 1
    assert (tmp_path / "host-sessions" / "grok" / f"{SID_GROK}.json").is_file()

    found = registry.lookup(SID_GROK)
    assert found is not None and found["provider"] == "grok" and found["name"] == grok_name
    snap = registry.snapshot()
    assert SID_GROK in snap

    hsnap = harness_registry.snapshot()
    assert SID_GROK in hsnap
    assert hsnap[SID_GROK].provider == "grok"
    assert hsnap[SID_GROK].name == grok_name

    registry.unregister(SID_GROK)
    assert registry.is_ended(SID_GROK) is True
    assert registry.lookup(SID_GROK) is None
