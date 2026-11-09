"""`repo_nested_names` / `repo_root_names` drop a stalled directory scan and name the advisory."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core.roadmap import prep_gate


@pytest.fixture(autouse=True)
def _short_wall(monkeypatch):
    monkeypatch.setattr(prep_gate, "REPO_SCAN_DEADLINE_SECS", 0.3)


def _block_iterdir(monkeypatch, slow: Path, release: threading.Event) -> None:
    real = Path.iterdir

    def iterdir(self):
        if self == slow:
            release.wait(30)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)


def test_healthy_scan_unchanged(tmp_path: Path) -> None:
    (tmp_path / "state" / "cross-repo").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    names = prep_gate.repo_root_names(tmp_path)
    assert names == frozenset({"state", "docs"})
    assert prep_gate.repo_nested_names(tmp_path, names) == {"cross-repo": ("state",)}


def test_stalled_nested_scan_returns_empty_within_wall_and_names_advisory(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    (tmp_path / "state").mkdir()
    release = threading.Event()
    _block_iterdir(monkeypatch, tmp_path / "state", release)
    t0 = time.monotonic()
    try:
        out = prep_gate.repo_nested_names(tmp_path, frozenset({"state"}))
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()
    assert out == {}
    assert prep_gate.REPO_SCAN_STALLED_ADVISORY in capsys.readouterr().err


def test_stalled_root_listing_returns_empty_within_wall(monkeypatch, tmp_path: Path, capsys) -> None:
    release = threading.Event()
    _block_iterdir(monkeypatch, tmp_path, release)
    t0 = time.monotonic()
    try:
        out = prep_gate.repo_root_names(tmp_path)
        assert time.monotonic() - t0 < 2.0
    finally:
        release.set()
    assert out == frozenset()
    assert prep_gate.REPO_SCAN_STALLED_ADVISORY in capsys.readouterr().err


def test_unreadable_root_still_fails_open_without_advisory(tmp_path: Path, capsys) -> None:
    assert prep_gate.repo_root_names(tmp_path / "absent") == frozenset()
    assert prep_gate.REPO_SCAN_STALLED_ADVISORY not in capsys.readouterr().err
