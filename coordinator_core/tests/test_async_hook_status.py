"""
coordinator_core.tests.test_async_hook_status — parity coverage for the
Python port of Port of: async-hook-status.sh (DoE c187f5b9, 2026-07-21)
(DOE-PORT R1, sourced-lib shape).

Mirrors Port of: async-hook-status.test.sh (DoE c187f5b9, 2026-07-21)'s
AC5 sub-properties:
  (a) happy path — record -> surface (operator line emitted) -> cleared
  (b) no-renag idempotency — second surface_and_clear with no intervening
      write emits nothing; a re-write-on-persist re-surfaces on the next call
  (c) exit-preservation / best-effort — record_failure and surface_and_clear
      never raise, even on malformed/unreadable markers
  (d) unwritable .cache fallback — degrades to a silent no-op (None / [])
      without crashing
"""

from __future__ import annotations

import json

import coordinator_core.async_hook_status as ahs


def test_status_dir_creates_canonical_path(tmp_path):
    result = ahs.status_dir(claude_home=tmp_path)
    assert result == tmp_path / ".claude" / ".cache" / "async-hook-status"
    assert result.is_dir()


def test_status_dir_unwritable_returns_none(tmp_path):
    # Place a regular FILE where .claude would need to be a directory, so
    # mkdir -p fails — mirrors the bash test's unwritable-.cache simulation.
    blocker = tmp_path / ".claude"
    blocker.write_text("not a directory", encoding="utf-8")

    result = ahs.status_dir(claude_home=tmp_path)
    assert result is None


def test_record_failure_writes_marker(tmp_path):
    ahs.record_failure("my-hook", 1, "boom", "/tmp/my-hook.log", claude_home=tmp_path)

    marker = tmp_path / ".claude" / ".cache" / "async-hook-status" / "my-hook.json"
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["hook"] == "my-hook"
    assert data["exit"] == 1
    assert data["detail"] == "boom"
    assert data["log"] == "/tmp/my-hook.log"
    assert data["remediation"] == ""
    assert data["failed_at"].endswith("Z")


def test_record_failure_latest_wins(tmp_path):
    ahs.record_failure("my-hook", 1, "first", claude_home=tmp_path)
    ahs.record_failure("my-hook", 2, "second", claude_home=tmp_path)

    marker = tmp_path / ".claude" / ".cache" / "async-hook-status" / "my-hook.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["exit"] == 2
    assert data["detail"] == "second"


def test_record_failure_never_raises_on_unwritable_cache(tmp_path):
    blocker = tmp_path / ".claude"
    blocker.write_text("not a directory", encoding="utf-8")

    # Must not raise — best-effort, matching the bash subshell + `return 0` contract.
    ahs.record_failure("my-hook", 1, "boom", claude_home=tmp_path)


def test_surface_and_clear_happy_path(tmp_path):
    ahs.record_failure("my-hook", 1, "boom", claude_home=tmp_path)

    lines = ahs.surface_and_clear(claude_home=tmp_path)

    assert len(lines) == 1
    assert "my-hook" in lines[0]
    assert "failed last boot" in lines[0]
    assert "exit 1" in lines[0]
    assert "boom" in lines[0]

    marker = tmp_path / ".claude" / ".cache" / "async-hook-status" / "my-hook.json"
    assert not marker.exists()


def test_surface_and_clear_no_renag_then_resurfaces_on_rewrite(tmp_path):
    ahs.record_failure("my-hook", 1, "boom", claude_home=tmp_path)

    first = ahs.surface_and_clear(claude_home=tmp_path)
    assert len(first) == 1

    # No intervening write -> second call emits nothing (marker was cleared).
    second = ahs.surface_and_clear(claude_home=tmp_path)
    assert second == []

    # Re-fail on the "next boot" -> re-surfaces on the call after that.
    ahs.record_failure("my-hook", 1, "boom again", claude_home=tmp_path)
    third = ahs.surface_and_clear(claude_home=tmp_path)
    assert len(third) == 1
    assert "boom again" in third[0]


def test_surface_and_clear_unwritable_cache_returns_empty(tmp_path):
    blocker = tmp_path / ".claude"
    blocker.write_text("not a directory", encoding="utf-8")

    lines = ahs.surface_and_clear(claude_home=tmp_path)
    assert lines == []


def test_surface_and_clear_no_markers_returns_empty(tmp_path):
    lines = ahs.surface_and_clear(claude_home=tmp_path)
    assert lines == []


def test_surface_and_clear_skips_malformed_marker_without_raising(tmp_path):
    status_dir = tmp_path / ".claude" / ".cache" / "async-hook-status"
    status_dir.mkdir(parents=True)
    (status_dir / "broken.json").write_text("not valid json{{{", encoding="utf-8")

    # Must not raise; malformed JSON still surfaces with unknown/default fields
    # (mirrors the bash sed-parse degrading to empty-match defaults, not a hard skip).
    lines = ahs.surface_and_clear(claude_home=tmp_path)
    assert len(lines) == 1
    assert "unknown" in lines[0]

    assert not (status_dir / "broken.json").exists()


def test_surface_and_clear_multiple_markers(tmp_path):
    ahs.record_failure("hook-a", 1, "a-detail", claude_home=tmp_path)
    ahs.record_failure("hook-b", 2, "b-detail", claude_home=tmp_path)

    lines = ahs.surface_and_clear(claude_home=tmp_path)

    assert len(lines) == 2
    joined = "\n".join(lines)
    assert "hook-a" in joined
    assert "hook-b" in joined
