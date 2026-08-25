"""Regression tests for cross-platform home sandboxing.

Guards the 2026-07-20 incident in which this suite wrote pytest tmpdirs into
the real `~/.claude/.doe-root` on Windows because `monkeypatch.setenv("HOME")`
does not influence `os.path.expanduser("~")` there.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.testing.home_sandbox import sandbox_home


def test_sandbox_home_redirects_expanduser(tmp_path, monkeypatch):
    home = sandbox_home(monkeypatch, tmp_path / "home")

    # The load-bearing assertion: expanduser — not just os.environ["HOME"] —
    # must resolve into the sandbox. This is what fails on Windows under a
    # bare setenv("HOME", ...).
    assert Path(os.path.expanduser("~")) == home


def test_sandbox_home_creates_the_directory(tmp_path, monkeypatch):
    home = sandbox_home(monkeypatch, tmp_path / "not-yet-there")

    assert home.is_dir()


def test_sandbox_home_clears_windows_second_tier(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEDRIVE", "C:")
    monkeypatch.setenv("HOMEPATH", r"\Users\someone-else")

    sandbox_home(monkeypatch, tmp_path / "home")

    # HOMEDRIVE+HOMEPATH is expanduser's fallback tier on Windows; leaving it
    # populated would readmit the real profile if USERPROFILE were unset.
    assert "HOMEDRIVE" not in os.environ
    assert "HOMEPATH" not in os.environ


def test_suite_conftest_quarantines_real_home_by_default():
    """Every test — including ones that never sandbox anything — resolves `~`
    into a pytest-owned tmpdir, courtesy of the suite-root autouse fixture."""
    resolved = Path(os.path.expanduser("~")).resolve()

    # tmp_path_factory.mktemp suffixes a counter, hence startswith not ==.
    assert resolved.name.startswith("home-quarantine"), resolved
