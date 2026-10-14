"""Tests for scan_floor: started_at parsing and the floored scandir walk."""

from __future__ import annotations

import builtins
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.session import scan_floor
from coordinator_core.session.core import session_dir
from coordinator_core.session.scan_floor import (
    FLOOR_SLACK_S,
    iter_files_at_or_above,
    session_start_floor,
)

SID = "sid-abc"


def _write_started(root: Path, text: str) -> None:
    (root / ".git").mkdir(exist_ok=True)  # session_dir needs a repo root
    sdir = Path(session_dir(SID, str(root)))
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "started_at").write_text(text, encoding="utf-8")


EPOCH = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-10-14T12:00:00Z\n", EPOCH),
        ("2026-10-14T12:00:00", EPOCH),
        ("2026-10-14T14:00:00+02:00", EPOCH),
    ],
)
def test_floor_parses_forms(tmp_path, text, expected):
    _write_started(tmp_path, text)
    assert session_start_floor(tmp_path, SID) == expected - FLOOR_SLACK_S


def test_floor_absent_file_and_dir(tmp_path):
    assert session_start_floor(tmp_path, SID) is None
    (tmp_path / ".git").mkdir()
    Path(session_dir(SID, str(tmp_path))).mkdir(parents=True)
    assert session_start_floor(tmp_path, SID) is None


@pytest.mark.parametrize("text", ["garbage", "", "   \n"])
def test_floor_unparseable(tmp_path, text):
    _write_started(tmp_path, text)
    assert session_start_floor(tmp_path, SID) is None


def _write_archived(root: Path, text: str) -> None:
    (root / ".git").mkdir(exist_ok=True)
    live = Path(session_dir(SID, str(root)))
    adir = live.parent / ".archive" / f"{SID}-2026-10-13"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "started_at").write_text(text, encoding="utf-8")


def test_slack_pinned():
    assert FLOOR_SLACK_S == 300.0


def test_archived_older_than_live_wins(tmp_path):
    _write_started(tmp_path, "2026-10-14T12:00:00Z")
    _write_archived(tmp_path, "2026-10-14T11:00:00Z")
    assert session_start_floor(tmp_path, SID) == EPOCH - 3600 - FLOOR_SLACK_S


def test_archived_newer_than_live_live_wins(tmp_path):
    _write_started(tmp_path, "2026-10-14T12:00:00Z")
    _write_archived(tmp_path, "2026-10-14T13:00:00Z")
    assert session_start_floor(tmp_path, SID) == EPOCH - FLOOR_SLACK_S


def test_live_missing_archive_present(tmp_path):
    _write_archived(tmp_path, "2026-10-14T12:00:00Z")
    assert session_start_floor(tmp_path, SID) == EPOCH - FLOOR_SLACK_S


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_floor_boundary(tmp_path):
    floor = 1_000_000.0
    at = _touch(tmp_path / "at", floor)
    above = _touch(tmp_path / "above", floor + 5)
    _touch(tmp_path / "below", floor - 5)
    got = set(iter_files_at_or_above(tmp_path, floor, frozenset(), recursive=False))
    assert got == {at, above}


def test_always_name_yielded_below_floor(tmp_path):
    floor = 1_000_000.0
    ledger = _touch(tmp_path / "ledger.md", floor - 100)
    _touch(tmp_path / "other.md", floor - 100)
    got = set(iter_files_at_or_above(tmp_path, floor, frozenset({"ledger.md"}), recursive=False))
    assert got == {ledger}


def test_none_floor_yields_all(tmp_path):
    a = _touch(tmp_path / "a", 1.0)
    b = _touch(tmp_path / "b", 2.0)
    assert set(iter_files_at_or_above(tmp_path, None, frozenset(), recursive=False)) == {a, b}


def test_recursive_vs_flat(tmp_path):
    top = _touch(tmp_path / "top", 500.0)
    deep = _touch(tmp_path / "d1" / "d2" / "deep", 500.0)
    assert set(iter_files_at_or_above(tmp_path, 100.0, frozenset(), recursive=False)) == {top}
    assert set(iter_files_at_or_above(tmp_path, 100.0, frozenset(), recursive=True)) == {top, deep}


def test_unreadable_and_missing_directory_skipped(tmp_path, monkeypatch):
    good = _touch(tmp_path / "good", 500.0)
    bad = tmp_path / "bad"
    bad.mkdir()
    real = os.scandir

    def fake(path):
        if Path(path) == bad:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(scan_floor.os, "scandir", fake)
    assert list(iter_files_at_or_above(tmp_path, 100.0, frozenset(), recursive=True)) == [good]
    assert list(iter_files_at_or_above(tmp_path / "nope", 100.0, frozenset(), recursive=True)) == []


def test_no_spawn_and_no_open(tmp_path, monkeypatch):
    _touch(tmp_path / "f", 500.0)
    _touch(tmp_path / "d" / "g", 500.0)

    def boom(*a, **k):
        raise AssertionError("forbidden call")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(Path, "open", boom)
    got = list(iter_files_at_or_above(tmp_path, 100.0, frozenset(), recursive=True))
    assert len(got) == 2
