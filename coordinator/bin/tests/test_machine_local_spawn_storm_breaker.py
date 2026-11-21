"""Pins the machine-local re-entry guard: a nested or saturated invocation is refused before any work.

In-process tests drive the breaker directly; subprocess tests drive the in-flight slot cap and
a normal call across a real process boundary.
"""
import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"


@pytest.fixture
def ml(monkeypatch):
    spec = importlib.util.spec_from_file_location("_machine_local_storm_breaker_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv(mod._DEPTH_ENV, "0")  # registers restore: the breaker sets it in os.environ
    return mod


@pytest.fixture
def slotted(ml, tmp_path, monkeypatch):
    monkeypatch.setenv(ml._SLOT_DIR_ENV, str(tmp_path / "slots"))
    return ml


def test_nested_invocation_is_refused(ml, monkeypatch, capsys):
    monkeypatch.setenv(ml._DEPTH_ENV, "1")
    assert ml._spawn_storm_breaker() == ml.EXIT_OPERATIONAL
    err = capsys.readouterr()
    assert "refused nested invocation" in err.err
    assert err.out == ""


def test_top_level_invocation_proceeds_and_marks_descendants(slotted, monkeypatch):
    monkeypatch.setattr(slotted.os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)
    assert slotted._spawn_storm_breaker() is None
    assert os.environ[slotted._DEPTH_ENV] == "1", "children inherit the depth var and refuse re-entry"
    os.close(slotted._held_slot)


def test_saturated_box_is_refused(slotted, monkeypatch, capsys):
    monkeypatch.setattr(slotted.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(slotted.os, "getloadavg", lambda: (17.0, 0.0, 0.0), raising=False)
    assert slotted._spawn_storm_breaker() == slotted.EXIT_OPERATIONAL
    assert "refused under load" in capsys.readouterr().err
    os.close(slotted._held_slot)


def test_load_at_limit_proceeds(slotted, monkeypatch):
    monkeypatch.setattr(slotted.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(slotted.os, "getloadavg", lambda: (16.0, 0.0, 0.0), raising=False)
    assert slotted._spawn_storm_breaker() is None
    os.close(slotted._held_slot)


def test_no_loadavg_proceeds(slotted, monkeypatch):
    def _absent():
        raise OSError("no load average")
    monkeypatch.setattr(slotted.os, "getloadavg", _absent, raising=False)
    assert slotted._spawn_storm_breaker() is None
    os.close(slotted._held_slot)


def test_default_cap_is_sixteen(ml, monkeypatch):
    monkeypatch.delenv(ml._MAX_INFLIGHT_ENV, raising=False)
    assert ml._max_inflight() == 16
