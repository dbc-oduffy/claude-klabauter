"""Pins the fleet Temp layout paths and the reserved ``_fleet`` repo name."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from coordinator_core import temp_layout


@pytest.fixture
def fake_temp(tmp_path, monkeypatch):
    base = tmp_path / "systemp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(base))
    return base


def test_coordinator_temp_root_is_two_segments_under_gettempdir(fake_temp):
    assert temp_layout.coordinator_temp_root(Path("/work/my-repo")) == fake_temp / "coordinator" / "my-repo"


def test_fleet_temp_root(fake_temp):
    assert temp_layout.fleet_temp_root() == fake_temp / "coordinator" / "_fleet"


def test_pytest_basetemp_root(fake_temp):
    assert temp_layout.pytest_basetemp_root("/work/my-repo") == fake_temp / "coordinator" / "my-repo" / "pytest"


def test_repo_named_fleet_is_refused(fake_temp):
    with pytest.raises(ValueError):
        temp_layout.coordinator_temp_root(Path("/work/_fleet"))
    with pytest.raises(ValueError):
        temp_layout.pytest_basetemp_root(Path("/work/_fleet"))


def test_resolver_reads_gettempdir_at_call_time(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(first))
    assert temp_layout.fleet_temp_root().parent.parent == first
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(second))
    assert temp_layout.fleet_temp_root().parent.parent == second
