"""Spawn budget for the write-bump anchor read: resolving the data-root hub and reading
a session's launch-cwd record spawns zero processes (AC6)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core import _data_root
from coordinator_core.bash_guards import _write_bump_session_start as session_start


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.delenv(_data_root.ENV_OVERRIDE, raising=False)
    _data_root.reset_cache()
    root = tmp_path / "data-root"
    record = root / "claude-klabauter" / "write-bump-anchor" / "sid-1" / "write_bump_launch_cwd"
    record.parent.mkdir(parents=True)
    record.write_text("/some/launch/cwd\n", encoding="utf-8")
    yield {_data_root.ENV_OVERRIDE: str(root)}
    _data_root.reset_cache()


@pytest.fixture
def popen_calls(monkeypatch):
    calls: list = []

    def counting(*a, **k):
        calls.append(a)
        raise OSError("spawn blocked by counter")

    monkeypatch.setattr(subprocess, "Popen", counting)
    monkeypatch.setattr(os, "system", counting)
    return calls


def test_positive_control_counter_sees_a_spawn(popen_calls):
    with pytest.raises(OSError):
        subprocess.run(["git", "--version"], check=False)
    assert len(popen_calls) == 1


def test_data_root_and_anchor_read_spawn_nothing(seeded, popen_calls):
    hub = _data_root.engine_data_root(seeded)
    value = session_start.read_session_start_record("sid-1", cwd=str(Path(hub)), env=seeded)

    assert value == "/some/launch/cwd"
    assert popen_calls == []


def test_registry_rung_resolves_without_spawning(tmp_path, monkeypatch, popen_calls):
    reg = tmp_path / "registry"
    reg.mkdir()
    root = tmp_path / "reg-root"
    (reg / "registry.local.toml").write_text(
        f'"{_data_root.REGISTRY_KEY}" = "{root.as_posix()}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.delenv(_data_root.ENV_OVERRIDE, raising=False)
    _data_root.reset_cache()
    try:
        assert _data_root.engine_data_root() == root / "claude-klabauter"
    finally:
        _data_root.reset_cache()
    assert popen_calls == []


def test_os_default_rung_resolves_without_spawning(tmp_path, monkeypatch, popen_calls):
    reg = tmp_path / "empty-registry"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.delenv(_data_root.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(_data_root, "_platform", lambda: "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    _data_root.reset_cache()
    try:
        assert _data_root.engine_data_root() == tmp_path / "xdg" / "coordinator" / "claude-klabauter"
    finally:
        _data_root.reset_cache()
    assert popen_calls == []
