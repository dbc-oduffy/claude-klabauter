"""Newer-index SessionStart notice renders from the marker alone."""
from __future__ import annotations

import socket
import subprocess
import time

import pytest

from coordinator_core.hooks import project_orientation as po
from coordinator_core.index_distribution import newer_index_marker as nim


@pytest.fixture(autouse=True)
def _no_spawn_no_net(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn or network attempted")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket, "socket", boom)


def _render(root):
    out: list[str] = []
    po._newer_index_banner(out, {}, str(root))
    return "".join(out)


def test_newer_marker_prints_notice_naming_fetch_command(tmp_path):
    nim.write_marker(tmp_path, {"kind": "structural", "checked_at": time.time(), "newer_available": True})
    text = _render(tmp_path)
    assert "Newer structural index available" in text
    assert "structural-index-refresh" in text


def test_not_newer_marker_prints_nothing(tmp_path):
    nim.write_marker(tmp_path, {"kind": "structural", "checked_at": time.time(), "newer_available": False})
    assert _render(tmp_path) == ""


def test_missing_marker_prints_stale_unknown_naming_workday_start(tmp_path):
    text = _render(tmp_path)
    assert "stale-unknown" in text and "/workday-start" in text
    assert text.count("\n") == 1


def test_stale_marker_prints_stale_unknown_even_if_newer(tmp_path):
    nim.write_marker(
        tmp_path,
        {"kind": "structural", "checked_at": time.time() - nim.TTL_SECONDS - 10, "newer_available": True},
    )
    text = _render(tmp_path)
    assert "stale-unknown" in text and "Newer structural" not in text


def test_no_repo_root_renders_nothing():
    out: list[str] = []
    po._newer_index_banner(out, {}, None)
    assert out == []
