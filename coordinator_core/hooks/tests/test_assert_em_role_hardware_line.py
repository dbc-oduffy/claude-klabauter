"""assert_em_role hardware line: present, formatted from the reader, spawn-free, advisory on failure."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import env_locality
from coordinator_core.hooks import assert_em_role as mod

GIB = 2**30


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path


def _run() -> str:
    result = mod._handler({"payload": {"session_id": "s-self"}})
    return result["hookSpecificOutput"]["additionalContext"]


def _hw(monkeypatch, value):
    monkeypatch.setattr(env_locality, "box_hardware", lambda *a, **k: value)


def test_line_names_brand_cores_and_ram(env, monkeypatch):
    _hw(monkeypatch, env_locality.BoxHardware("Test CPU X1", 16, 32, 64 * GIB))
    out = _run()
    assert out.count("assert-em-role: this box:") == 1
    assert "Test CPU X1, 16 physical / 32 logical cores, 64GB RAM" in out


def test_partial_fields_degrade_to_partial_line(env, monkeypatch):
    _hw(monkeypatch, env_locality.BoxHardware(None, None, 8, None))
    out = _run()
    assert "this box: ? physical / 8 logical cores" in out
    assert "RAM" not in out


def test_all_unknown_adds_no_line(env, monkeypatch):
    _hw(monkeypatch, env_locality.BoxHardware(None, None, None, None))
    assert "this box:" not in _run()


def test_reader_failure_never_blocks_session_start(env, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr(env_locality, "box_hardware", boom)
    assert "this box:" not in _run()


def test_building_the_line_spawns_nothing(env, monkeypatch):
    env_locality.box_hardware(force=True)

    def no_spawn(*_a, **_k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(subprocess, "run", no_spawn)
    line = mod._hardware_line()
    assert line == "" or line.startswith("assert-em-role: this box:")
