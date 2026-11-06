"""Pin: a test run never lets `hooks.sweep_boot` self-heal the operator's real settings home."""

from __future__ import annotations

import asyncio
import os

import pytest

from coordinator_core import _settings_home
from coordinator_core.hooks import sweep_boot
from coordinator_core.install import forwarder_self_heal


def _spy_self_heal(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(forwarder_self_heal, "self_heal_forwarders", lambda: calls.append(1))
    return calls


def _arm_self_heal(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("COORDINATOR_FORWARDER_SELFHEAL_OFF", raising=False)
    monkeypatch.setenv("COORDINATOR_ORIENTATION_SELFHEAL_OFF", "1")
    monkeypatch.setenv("COORDINATOR_SESSION_REAP_OFF", "1")
    monkeypatch.setenv("COORDINATOR_INDEX_RESYNC_DRAIN_OFF", "1")
    monkeypatch.setattr(sweep_boot, "_resolve_this_repo_root", lambda: str(tmp_path))


def test_handler_refuses_loudly_when_settings_home_is_the_forbidden_real_one(monkeypatch, tmp_path):
    real = tmp_path / "real-settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(real))
    monkeypatch.setenv(_settings_home.FORBID_REAL_SETTINGS_HOME_ENV, str(real))
    _arm_self_heal(monkeypatch, tmp_path)
    calls = _spy_self_heal(monkeypatch)
    with pytest.raises(_settings_home.RealSettingsHomeLeakError):
        asyncio.run(sweep_boot._handler({}))
    assert calls == []


def test_handler_self_heals_an_isolated_settings_home(monkeypatch, tmp_path):
    isolated = tmp_path / "isolated-settings-home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(isolated))
    monkeypatch.setenv(_settings_home.FORBID_REAL_SETTINGS_HOME_ENV, str(tmp_path / "elsewhere"))
    _arm_self_heal(monkeypatch, tmp_path)
    calls = _spy_self_heal(monkeypatch)
    assert asyncio.run(sweep_boot._handler({})) == sweep_boot.no_advisory()
    assert calls == [1]


def test_default_test_environment_never_self_heals(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_ORIENTATION_SELFHEAL_OFF", "1")
    monkeypatch.setenv("COORDINATOR_SESSION_REAP_OFF", "1")
    monkeypatch.setenv("COORDINATOR_INDEX_RESYNC_DRAIN_OFF", "1")
    monkeypatch.setattr(sweep_boot, "_resolve_this_repo_root", lambda: str(tmp_path))
    calls = _spy_self_heal(monkeypatch)
    asyncio.run(sweep_boot._handler({}))
    assert calls == []


def test_default_settings_home_is_not_the_real_one():
    forbidden = os.environ.get(_settings_home.FORBID_REAL_SETTINGS_HOME_ENV)
    if not forbidden:
        pytest.skip("real settings home unresolvable on this box; guard inert")
    assert str(_settings_home.settings_home()) != forbidden
