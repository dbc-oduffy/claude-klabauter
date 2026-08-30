"""
Tests for coordinator_core.engine_version — the engine self-version surface.

Covers: real-repo SHA resolution shape, graceful-degradation to None on git
failure (never raises), and the committed floor constant's shape.

Spec backlink: pln-claude-klabauter-engine-version-surface--c130a8
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.engine_version import (
    MIN_KNOWN_GOOD_SHA,
    resolve_engine_dirty,
    resolve_engine_sha,
)
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def test_resolve_engine_sha_returns_40_char_lowercase_hex_in_real_repo():
    sha = resolve_engine_sha()
    assert sha is not None
    assert len(sha) == 40
    # Review: code-reviewer (Finding 4) — literal lowercase-hex set states
    # intent directly instead of relying on `.lower()`'s redundant haystack.
    assert all(c in "0123456789abcdef" for c in sha)


def test_resolve_engine_sha_returns_none_when_git_missing(monkeypatch):
    def raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(
        "subprocess.run", raise_file_not_found
    )
    assert resolve_engine_sha() is None


def test_resolve_engine_sha_strips_trailing_newline_from_git_stdout(monkeypatch):
    # Review: code-reviewer (Finding 5) — explicitly exercises `.strip()` via
    # a mocked stdout so a regression (e.g. accidental `.rstrip("\n")` swap,
    # or `.strip()` removal) fails here rather than only incidentally via the
    # real-repo test.
    class FakeResult:
        returncode = 0
        stdout = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n"

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: FakeResult(),
    )
    assert resolve_engine_sha() == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_resolve_engine_sha_returns_none_when_git_returns_nonzero(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: FakeResult(),
    )
    assert resolve_engine_sha() is None


def test_resolve_engine_dirty_returns_bool_in_real_repo():
    dirty = resolve_engine_dirty()
    assert dirty is None or isinstance(dirty, bool)


def test_resolve_engine_dirty_false_when_porcelain_output_empty(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: FakeResult(),
    )
    assert resolve_engine_dirty() is False


def test_resolve_engine_dirty_true_when_porcelain_output_nonempty(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = " M engine_version.py\n"

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: FakeResult(),
    )
    assert resolve_engine_dirty() is True


def test_resolve_engine_dirty_scopes_status_to_engine_dir(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeResult()

    monkeypatch.setattr(
        "subprocess.run", fake_run
    )
    resolve_engine_dirty()
    assert captured["cmd"][:3] == ["git", "-C", str(Path(__file__).resolve().parent.parent)]
    assert captured["cmd"][-2:] == ["--", "."]


def test_resolve_engine_dirty_returns_none_when_git_missing(monkeypatch):
    def raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(
        "subprocess.run", raise_file_not_found
    )
    assert resolve_engine_dirty() is None


def test_resolve_engine_dirty_returns_none_when_git_returns_nonzero(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: FakeResult(),
    )
    assert resolve_engine_dirty() is None


def test_resolve_engine_dirty_returns_none_when_timeout(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=5)

    monkeypatch.setattr(
        "subprocess.run", raise_timeout
    )
    assert resolve_engine_dirty() is None


def test_min_known_good_sha_is_40_char_hex():
    assert len(MIN_KNOWN_GOOD_SHA) == 40
    # Review: code-reviewer (Finding 4) — literal lowercase-hex set states
    # intent directly instead of relying on `.lower()`'s redundant haystack.
    assert all(c in "0123456789abcdef" for c in MIN_KNOWN_GOOD_SHA)


def test_min_known_good_sha_is_a_real_commit_in_this_repo():
    # Review: code-reviewer (Finding 3) — a shape-only check lets a
    # typo'd/orphaned floor SHA pass silently and surfaces later as a
    # misclassified "indeterminate" drift result rather than a fast local
    # test failure. Tolerant of a missing git binary (skip, don't fail).
    engine_dir = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(engine_dir), "cat-file", "-e",
             f"{MIN_KNOWN_GOOD_SHA}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=5,
            **no_console_creationflags(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pytest.skip("git unavailable in this test environment")

    assert result.returncode == 0, (
        f"MIN_KNOWN_GOOD_SHA {MIN_KNOWN_GOOD_SHA!r} is not a resolvable "
        f"commit in this repo's history: {result.stderr}"
    )
