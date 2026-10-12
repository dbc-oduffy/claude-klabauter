"""Tests for coordinator_core._data_root (AC1-AC5, AC11)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from coordinator_core import _data_root, machine_resolver


@pytest.fixture(autouse=True)
def _cold(monkeypatch, tmp_path):
    monkeypatch.delenv(_data_root.ENV_OVERRIDE, raising=False)
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    _data_root.reset_cache()
    yield reg
    _data_root.reset_cache()


def _write_registry(reg: Path, value: str) -> Path:
    path = reg / "registry.local.toml"
    path.write_text(f'"{_data_root.REGISTRY_KEY}" = {value!r}\n'.replace("'", '"'), encoding="utf-8")
    return path


def test_ac1_surface_and_override_name():
    assert issubclass(_data_root.EngineDataRootUnresolved, OSError)
    assert _data_root.ENV_OVERRIDE == machine_resolver._env_override_key(_data_root.REGISTRY_KEY)


def test_quarantine_sets_override_above_real_home_return():
    src = (Path(__file__).resolve().parent.parent / "conftest.py").read_text(encoding="utf-8")
    body = src[src.index("def _quarantine_real_home") :]
    setenv = body.index('"MACHINE_LOCAL_PATHS_ENGINE_DATA_ROOT"')
    assert setenv < body.index('get_closest_marker("real_home")')


def test_env_override_wins_over_registry(_cold, tmp_path):
    _write_registry(_cold, str(tmp_path / "reg-root"))
    env = {_data_root.ENV_OVERRIDE: str(tmp_path / "env-root")}
    assert _data_root.engine_data_root(env) == tmp_path / "env-root" / "claude-klabauter"


def test_registry_wins_over_default(_cold, tmp_path):
    _write_registry(_cold, str(tmp_path / "reg-root"))
    assert _data_root.engine_data_root({}) == tmp_path / "reg-root" / "claude-klabauter"


def test_relative_and_empty_env_skip_to_registry(_cold, tmp_path):
    _write_registry(_cold, str(tmp_path / "reg-root"))
    for bad in ("", "relative/dir"):
        got = _data_root.engine_data_root({_data_root.ENV_OVERRIDE: bad})
        assert got == tmp_path / "reg-root" / "claude-klabauter"


def test_relative_registry_skips_to_default(_cold, tmp_path, monkeypatch):
    _write_registry(_cold, "relative/dir")
    monkeypatch.setattr(_data_root, "_platform", lambda: "linux")
    base = tmp_path / "xdg"
    got = _data_root.engine_data_root({"XDG_DATA_HOME": str(base)})
    assert got == base / "coordinator" / "claude-klabauter"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_default_win32(tmp_path, monkeypatch, fake_home):
    monkeypatch.setattr(_data_root, "_platform", lambda: "win32")
    base = tmp_path / "local"
    got = _data_root.engine_data_root({"LOCALAPPDATA": str(base)})
    assert got == base / "coordinator" / "claude-klabauter"
    got = _data_root.engine_data_root({})
    assert got == fake_home / "AppData" / "Local" / "coordinator" / "claude-klabauter"


def test_default_darwin(monkeypatch, fake_home):
    monkeypatch.setattr(_data_root, "_platform", lambda: "darwin")
    got = _data_root.engine_data_root({})
    assert got == fake_home / "Library" / "Application Support" / "coordinator" / "claude-klabauter"


def test_default_linux(tmp_path, monkeypatch, fake_home):
    monkeypatch.setattr(_data_root, "_platform", lambda: "linux")
    base = tmp_path / "xdg"
    assert _data_root.engine_data_root({"XDG_DATA_HOME": str(base)}) == (
        base / "coordinator" / "claude-klabauter"
    )
    assert _data_root.engine_data_root({"XDG_DATA_HOME": "relative"}) == (
        fake_home / ".local" / "share" / "coordinator" / "claude-klabauter"
    )


def test_unresolved_raises(monkeypatch):
    monkeypatch.setattr(_data_root, "_platform", lambda: "linux")
    monkeypatch.setattr(_data_root, "_home", lambda: None)
    with pytest.raises(_data_root.EngineDataRootUnresolved):
        _data_root.engine_data_root({})


def test_registry_memo_and_mtime_invalidation(_cold, tmp_path, monkeypatch):
    path = _write_registry(_cold, str(tmp_path / "one"))
    calls = []
    real = machine_resolver.registry_get

    def counting(key):
        calls.append(key)
        return real(key)

    monkeypatch.setattr(machine_resolver, "registry_get", counting)
    assert _data_root.engine_data_root({}) == tmp_path / "one" / "claude-klabauter"
    assert _data_root.engine_data_root({}) == tmp_path / "one" / "claude-klabauter"
    assert len(calls) == 1

    _write_registry(_cold, str(tmp_path / "two"))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert _data_root.engine_data_root({}) == tmp_path / "two" / "claude-klabauter"
    assert len(calls) == 2

    _data_root.reset_cache()
    _data_root.engine_data_root({})
    assert len(calls) == 3


def test_source_has_no_drive_or_user_literal():
    src = Path(_data_root.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b[A-Za-z]:[\\/]", src)
    assert "example-operator" not in src
