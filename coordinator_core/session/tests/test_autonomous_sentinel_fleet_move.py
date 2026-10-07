"""Sentinel writes under _fleet/; readers dual-read _fleet/ then the legacy bare-Temp path."""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import pytest

from coordinator_core.session import autonomous_go, mode_resolution
from coordinator_core.session import autonomous_sentinel as sentinel

SID = "sid-c8"


@pytest.fixture
def tmp(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def test_write_path_is_under_fleet(tmp) -> None:
    assert sentinel.sentinel_path(SID) == tmp / "coordinator" / "_fleet" / f"autonomous-run-{SID}"


def test_neither_present_reads_absent(tmp) -> None:
    assert sentinel.sentinel_read_path(SID) is None
    assert mode_resolution._autonomous_session_value(SID) is False
    assert autonomous_go._autonomous_command_signal(SID) is None


def test_legacy_only_is_read_by_each_reader(tmp) -> None:
    (tmp / f"autonomous-run-{SID}").write_text("autonomous\n")
    assert sentinel.sentinel_read_path(SID) == sentinel.legacy_sentinel_path(SID)
    assert mode_resolution._autonomous_session_value(SID) is True
    assert autonomous_go._autonomous_command_signal(SID) is not None


def test_fleet_wins_over_legacy(tmp) -> None:
    (tmp / f"autonomous-run-{SID}").write_text("x")
    fleet = sentinel.sentinel_path(SID)
    fleet.parent.mkdir(parents=True)
    fleet.write_text("autonomous\n")
    assert sentinel.sentinel_read_path(SID) == fleet


def _cli():
    path = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "misc-session-and-guards.py"
    spec = importlib.util.spec_from_file_location("misc_cli_c8", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_enable_lands_under_fleet_and_disable_clears_both(tmp, monkeypatch) -> None:
    cli = _cli()
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda *a, **k: SID)
    assert cli._cmd_autonomous_sentinel(["enable", "--mode", "autonomous"]) == 0
    assert sentinel.sentinel_path(SID).read_text() == "autonomous\n"
    assert not sentinel.legacy_sentinel_path(SID).exists()
    sentinel.legacy_sentinel_path(SID).write_text("autonomous\n")
    assert cli._cmd_autonomous_sentinel(["disable"]) == 0
    assert sentinel.sentinel_read_path(SID) is None
